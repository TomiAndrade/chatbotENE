"""FastAPI: endpoints /webhook y /health.

Flujo de /webhook (ver spec-etapa1.md y spec-etapa2.md). El endpoint hace
solo lo mínimo para poder contestar 200 enseguida:

1. Verificar firma.
2. Si el evento no es un mensaje entrante, ignorar.
3. Extraer los datos del payload y encolar el procesamiento.

Todo lo que toca la base de datos o la red corre después, en la background
task `procesar_mensaje_entrante`:

4. Deduplicar por wa_message_id.
5. Buscar o crear la conversación y guardar el mensaje entrante.
6. Si modo_humano, cortar acá.
7. Si el número superó el límite de mensajes por hora, cortar (avisando una
   sola vez).
8. Armar el historial, generar la respuesta y, según lo que haya devuelto el
   modelo, enviarla, escalar a humano, o ambas cosas.

Starlette corre las background tasks en un threadpool y recién después de
haber mandado la respuesta HTTP, así que las llamadas bloqueantes de
SQLAlchemy y httpx no frenan el event loop. La contracara es que dos mensajes
seguidos del mismo número se procesan en paralelo: el chequeo de modo_humano
del paso 6 puede quedar viejo mientras el modelo piensa, así que se vuelve a
leer de la base justo antes de enviar (ver `esta_en_modo_humano`).
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app import mensajes
from app.config import config
from app.db import SessionLocal, init_db
from app.historial import construir_historial
from app.kapso import KapsoClient, verificar_firma_webhook
from app.limite import mensajes_ultima_hora
from app.models import Conversacion, Mensaje, RolMensaje
from app.respuesta import ErrorTransitorioProveedor, generar_respuesta

logging.basicConfig(
    level=logging.DEBUG if config.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

app = FastAPI(title="Bot WhatsApp ENE IA LAB")
kapso_client = KapsoClient()

EVENTO_MENSAJE_RECIBIDO = "whatsapp.message.received"


@app.on_event("startup")
def al_iniciar() -> None:
    init_db()


@app.on_event("shutdown")
def al_apagar() -> None:
    kapso_client.cerrar()


def enmascarar_telefono(telefono: str) -> str:
    """Muestra los primeros 4 y los últimos 2 dígitos, el resto tapado."""
    if len(telefono) <= 6:
        return "*" * len(telefono)
    return telefono[:4] + "*" * (len(telefono) - 6) + telefono[-2:]


def buscar_o_crear_conversacion(db, telefono: str) -> Conversacion:
    conversacion = db.query(Conversacion).filter_by(telefono=telefono).first()
    if conversacion is not None:
        return conversacion

    try:
        conversacion = Conversacion(telefono=telefono)
        db.add(conversacion)
        db.commit()
        db.refresh(conversacion)
        return conversacion
    except IntegrityError:
        # Otro mensaje del mismo número creó la conversación entre el SELECT
        # de arriba y este INSERT. La constraint única de telefono frenó al
        # nuestro: nos quedamos con la que ya existe.
        db.rollback()
        return db.query(Conversacion).filter_by(telefono=telefono).one()


def esta_en_modo_humano(db, conversacion: Conversacion) -> bool:
    """Re-lee modo_humano de la base, sin confiar en lo que tenga cargado la
    sesión.

    Hace falta porque entre el chequeo de modo_humano de
    `procesar_mensaje_entrante` y el momento de enviar puede pasar bastante
    tiempo: la llamada al modelo tiene un presupuesto de 20 segundos, y en esa
    ventana otra entrega concurrente del mismo número puede haber escalado, o
    alguien del equipo puede haber marcado la conversación a mano. Si no se
    vuelve a mirar, el bot escribe encima de un humano — que es exactamente lo
    que el criterio de aceptación 4 del spec-etapa2.md prohíbe.
    """
    db.refresh(conversacion)
    return conversacion.modo_humano


def enviar_y_guardar(
    db,
    conversacion: Conversacion,
    texto: str,
    aunque_este_en_modo_humano: bool = False,
) -> bool:
    """Envía un texto por Kapso y, si se pudo mandar, lo guarda como mensaje
    del bot. Se usa tanto para la respuesta del modelo como para los avisos
    de escalamiento, límite y error: todos son mensajes "del bot" a efectos
    del historial.

    Antes de enviar vuelve a mirar modo_humano (ver `esta_en_modo_humano`) y
    descarta el mensaje si la conversación ya pasó a una persona. La única
    excepción es el aviso de escalamiento, que se manda justo después de
    prender modo_humano y por eso llega con
    `aunque_este_en_modo_humano=True`.

    Devuelve True si el mensaje salió; False si se descartó por modo_humano o
    si Kapso lo rechazó.
    """
    telefono = conversacion.telefono

    if not aunque_este_en_modo_humano and esta_en_modo_humano(db, conversacion):
        logger.info(
            "La conversación con %s pasó a modo humano mientras se generaba la "
            "respuesta: no se envía nada para no escribir encima de la persona",
            enmascarar_telefono(telefono),
        )
        return False

    try:
        kapso_client.enviar_mensaje_texto(telefono, texto)
    except Exception:
        logger.exception("No se pudo enviar un mensaje a %s", enmascarar_telefono(telefono))
        return False

    mensaje_bot = Mensaje(
        conversacion_id=conversacion.id,
        rol=RolMensaje.BOT,
        contenido=texto,
    )
    db.add(mensaje_bot)
    conversacion.ultimo_mensaje_en = datetime.now(timezone.utc)
    db.commit()

    logger.info("Mensaje enviado a %s: %s", enmascarar_telefono(telefono), texto)
    return True


def escalar_a_humano(db, conversacion: Conversacion, resumen: str | None) -> None:
    """Marca la conversación en modo humano, guarda el resumen y avisa al
    usuario con el texto que corresponda según el horario. A partir de acá
    el bot no vuelve a responder en esta conversación.

    Si mientras se generaba la respuesta otra entrega ya escaló, no se vuelve
    a escalar: pisar el resumen del primer escalamiento con el del segundo le
    saca contexto a quien vaya a atender.
    """
    if esta_en_modo_humano(db, conversacion):
        logger.info(
            "La conversación con %s ya estaba escalada, no se escala de nuevo",
            enmascarar_telefono(conversacion.telefono),
        )
        return

    conversacion.modo_humano = True
    conversacion.resumen_escalamiento = resumen
    conversacion.escalada_en = datetime.now(timezone.utc)
    db.commit()

    # Va acá, pegado al commit, y no al final: si el envío del aviso falla, el
    # escalamiento ya ocurrió igual y tiene que quedar en el log sí o sí.
    logger.warning(
        "ESCALADO A HUMANO — %s | resumen: %s",
        enmascarar_telefono(conversacion.telefono), resumen,
    )

    ahora_local = datetime.now(config.timezone)
    aviso = mensajes.mensaje_escalamiento(ahora_local)
    if not enviar_y_guardar(db, conversacion, aviso, aunque_este_en_modo_humano=True):
        logger.error(
            "La conversación con %s quedó escalada pero NO se pudo enviar el aviso de "
            "escalamiento: la persona del equipo tiene que responder sin que el usuario "
            "sepa todavía que su consulta pasó a un humano.",
            enmascarar_telefono(conversacion.telefono),
        )


def responder(db, conversacion: Conversacion, mensaje_usuario: Mensaje) -> None:
    """Arma el historial, genera la respuesta y actúa según lo que haya
    devuelto el modelo: enviar texto, escalar a humano, o ambas cosas. Si la
    llamada al modelo falla o vuelve vacía sin escalar, se avisa el error y
    se escala de todos modos: una conversación en manos de una persona es
    mejor que una conversación muerta.

    Excepción: un ErrorTransitorioProveedor (saturación, 429, 5xx, o un error
    de upstream que llegó dentro de un HTTP 200 — ver spec-etapa2.md) en
    desarrollo no escala, solo pide que se reintente. Con un proveedor
    gratuito la tasa de estos errores es alta, y escalar en cada uno dejaría
    casi toda conversación de prueba en modo_humano sin que haya pasado nada
    malo con el bot. En producción, con un proveedor pago, la regla no
    cambia: escala igual que cualquier otro fallo.

    Ninguna de las ramas de acá abajo vuelve a chequear modo_humano a mano:
    de eso se encargan `enviar_y_guardar` y `escalar_a_humano`, que lo releen
    de la base justo antes de actuar. La lectura que hizo
    `procesar_mensaje_entrante` ya quedó vieja para cuando el modelo
    contesta."""
    historial = construir_historial(db, conversacion, mensaje_usuario)

    try:
        resultado = generar_respuesta(historial=historial, mensaje_nuevo=mensaje_usuario.contenido)
    except ErrorTransitorioProveedor as error:
        logger.warning(
            "Error transitorio del proveedor de IA para %s: %s",
            enmascarar_telefono(conversacion.telefono), error,
        )
        if config.debug:
            enviar_y_guardar(db, conversacion, mensajes.MENSAJE_ERROR_TRANSITORIO)
        else:
            enviar_y_guardar(db, conversacion, mensajes.MENSAJE_ERROR_GENERICO)
            escalar_a_humano(
                db, conversacion,
                resumen="Error automático: error transitorio del proveedor de IA.",
            )
        return
    except Exception:
        logger.exception(
            "Falló la llamada al modelo para %s", enmascarar_telefono(conversacion.telefono),
        )
        enviar_y_guardar(db, conversacion, mensajes.MENSAJE_ERROR_GENERICO)
        escalar_a_humano(db, conversacion, resumen="Error automático: no se pudo generar una respuesta.")
        return

    if (not resultado.texto or not resultado.texto.strip()) and not resultado.escalar:
        logger.error(
            "El modelo devolvió una respuesta vacía sin escalar para %s",
            enmascarar_telefono(conversacion.telefono),
        )
        enviar_y_guardar(db, conversacion, mensajes.MENSAJE_ERROR_GENERICO)
        escalar_a_humano(db, conversacion, resumen="Error automático: el modelo no generó una respuesta.")
        return

    if resultado.texto:
        enviar_y_guardar(db, conversacion, resultado.texto)

    if resultado.escalar:
        escalar_a_humano(db, conversacion, resultado.resumen)


def procesar_mensaje_entrante(telefono: str, wa_message_id: str, contenido: str) -> None:
    """Pasos 4 a 7 del flujo. Corre en background: la request del webhook ya
    devolvió 200 antes de que esto empiece."""
    db = SessionLocal()
    try:
        ya_existe = db.query(Mensaje).filter_by(wa_message_id=wa_message_id).first()
        if ya_existe is not None:
            logger.info(
                "Mensaje duplicado de %s (wa_message_id=%s), se descarta",
                enmascarar_telefono(telefono), wa_message_id,
            )
            return

        conversacion = buscar_o_crear_conversacion(db, telefono)

        mensaje_usuario = Mensaje(
            conversacion_id=conversacion.id,
            rol=RolMensaje.USUARIO,
            contenido=contenido,
            wa_message_id=wa_message_id,
        )
        db.add(mensaje_usuario)
        conversacion.ultimo_mensaje_en = datetime.now(timezone.utc)
        try:
            db.commit()
        except IntegrityError:
            # Dos entregas del mismo mensaje llegaron a la vez y las dos
            # pasaron el chequeo de duplicado de arriba. La constraint única
            # de wa_message_id frena a la segunda, que es esta.
            db.rollback()
            logger.info(
                "Mensaje duplicado de %s (wa_message_id=%s) detectado al guardar, se descarta",
                enmascarar_telefono(telefono), wa_message_id,
            )
            return

        logger.info(
            "Mensaje de %s guardado (modo_humano=%s): %s",
            enmascarar_telefono(telefono), conversacion.modo_humano, contenido,
        )

        if conversacion.modo_humano:
            return

        conteo_ultima_hora = mensajes_ultima_hora(db, telefono)
        if conteo_ultima_hora > config.limite_mensajes_hora:
            # Solo el mensaje que recién cruza el límite dispara el aviso.
            # Los siguientes, mientras siga por encima, quedan en silencio.
            if conteo_ultima_hora == config.limite_mensajes_hora + 1:
                logger.warning(
                    "Límite de mensajes por hora superado por %s (%s en la última hora)",
                    enmascarar_telefono(telefono), conteo_ultima_hora,
                )
                enviar_y_guardar(db, conversacion, mensajes.MENSAJE_LIMITE_ALCANZADO)
            return

        responder(db, conversacion, mensaje_usuario)
    finally:
        db.close()


@app.post("/webhook")
async def recibir_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_webhook_signature: str | None = Header(default=None),
    x_webhook_event: str | None = Header(default=None),
):
    # La firma se calcula sobre el body crudo, por eso hay que leerlo antes
    # de que algo lo parsee como JSON. Es el único punto async del archivo.
    cuerpo_crudo = await request.body()

    if not verificar_firma_webhook(cuerpo_crudo, x_webhook_signature):
        logger.warning("Firma de webhook inválida, se descarta la request")
        raise HTTPException(status_code=401, detail="Firma inválida")

    if x_webhook_event != EVENTO_MENSAJE_RECIBIDO:
        logger.debug("Evento de webhook ignorado: %s", x_webhook_event)
        return {"status": "evento ignorado"}

    payload = json.loads(cuerpo_crudo)
    mensaje = payload.get("message", {})
    conversacion_payload = payload.get("conversation", {})

    wa_message_id = mensaje.get("id")
    telefono = mensaje.get("from") or conversacion_payload.get("phone_number")
    tipo = mensaje.get("type")
    if tipo == "text":
        contenido = mensaje.get("text", {}).get("body", "")
    else:
        contenido = f"[mensaje de tipo '{tipo}' no soportado en esta etapa]"

    if not telefono or not wa_message_id:
        logger.warning("Payload de webhook incompleto, se descarta: %s", payload)
        return {"status": "payload incompleto"}

    background_tasks.add_task(procesar_mensaje_entrante, telefono, wa_message_id, contenido)
    return {"status": "ok"}


@app.get("/health")
def salud():
    return {"status": "ok"}
