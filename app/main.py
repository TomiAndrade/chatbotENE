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

El webhook también atiende `whatsapp.message.sent` (ver
spec-pausa-por-intervencion-humana.md): el número está en modo coexistencia,
la secretaría responde desde la app de WhatsApp Business sobre el mismo
número que usa el bot, y hay que enterarse cuando eso pasa para no escribir
encima. `procesar_mensaje_saliente` filtra ese evento a los mensajes
`direction=outbound, origin=business_app` — la combinación que solo puede
mandar una persona desde la app, nunca el bot por API — y prende o reinicia
la pausa por intervención manual. `motivo_pausa` (`MotivoPausa` en
app/models.py) distingue esa pausa de un escalamiento del modelo: solo la
manual expira, pasados `PAUSA_HUMANA_MINUTOS` desde `modo_humano_desde`; un
escalamiento (`escalar_a_humano`) usa el mismo `modo_humano` pero no expira
nunca, se desmarca a mano.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app import mensajes
from app.config import config
from app.db import SessionLocal, init_db
from app.historial import construir_historial
from app.kapso import KapsoClient, verificar_firma_webhook
from app.limite import mensajes_ultima_hora
from app.models import CANAL_WHATSAPP, Conversacion, Mensaje, MotivoPausa, RolMensaje
from app.respuesta import ErrorTransitorioProveedor, generar_respuesta

logging.basicConfig(
    level=logging.DEBUG if config.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

app = FastAPI(title="Bot WhatsApp ENE IA LAB")
kapso_client = KapsoClient()

EVENTO_MENSAJE_RECIBIDO = "whatsapp.message.received"
EVENTO_MENSAJE_ENVIADO = "whatsapp.message.sent"


@app.on_event("startup")
def al_iniciar() -> None:
    init_db()


@app.on_event("shutdown")
def al_apagar() -> None:
    kapso_client.cerrar()


def enmascarar_identificador(identificador: str) -> str:
    """Muestra los primeros 4 y los últimos 2 caracteres, el resto tapado.

    Pensada originalmente para números de WhatsApp, pero no asume ese formato:
    no valida dígitos ni longitud de país, solo tapa el medio de la cadena.
    Sirve igual para un id de sesión de otro canal.
    """
    if len(identificador) <= 6:
        return "*" * len(identificador)
    return identificador[:4] + "*" * (len(identificador) - 6) + identificador[-2:]


def buscar_o_crear_conversacion(db, canal: str, identificador_externo: str) -> Conversacion:
    conversacion = (
        db.query(Conversacion).filter_by(canal=canal, identificador_externo=identificador_externo).first()
    )
    if conversacion is not None:
        return conversacion

    try:
        conversacion = Conversacion(canal=canal, identificador_externo=identificador_externo)
        db.add(conversacion)
        db.commit()
        db.refresh(conversacion)
        return conversacion
    except IntegrityError:
        # Otro mensaje del mismo identificador creó la conversación entre el
        # SELECT de arriba y este INSERT. La constraint única de (canal,
        # identificador_externo) frenó al nuestro: nos quedamos con la que ya
        # existe.
        db.rollback()
        return db.query(Conversacion).filter_by(canal=canal, identificador_externo=identificador_externo).one()


def _pausa_vigente(conversacion: Conversacion, ahora: datetime) -> bool:
    """Si `conversacion` está en modo_humano *ahora mismo*, contemplando que
    la pausa por intervención manual expira.

    Quién decide si expira es `motivo_pausa`, no si `modo_humano_desde` tiene
    valor: solo INTERVENCION_MANUAL expira, a los `PAUSA_HUMANA_MINUTOS` de
    la última vez que la secretaría respondió (se reinicia con cada mensaje
    nuevo, ver `registrar_intervencion_humana`). ESCALAMIENTO no expira
    nunca, se desmarca a mano. Un `motivo_pausa` en None con `modo_humano`
    prendido no debería pasar (dato viejo, o alguien puso `modo_humano = 1`
    a mano por SQL sin especificar el motivo) — se trata como si no
    expirara: errar hacia "sigue pausado" es más seguro que arriesgarse a
    que el bot le escriba encima a alguien.

    SQLite devuelve los DateTime(timezone=True) sin tzinfo aunque se hayan
    guardado en UTC (se probó a mano: el round-trip pierde el offset). Todo lo
    que este proyecto guarda en esas columnas es `datetime.now(timezone.utc)`
    o equivalente, así que un valor naive acá se interpreta como UTC.
    """
    if not conversacion.modo_humano:
        return False
    if conversacion.motivo_pausa != MotivoPausa.INTERVENCION_MANUAL:
        return True

    desde = conversacion.modo_humano_desde
    if desde is None:
        return True
    if desde.tzinfo is None:
        desde = desde.replace(tzinfo=timezone.utc)
    return ahora - desde <= timedelta(minutes=config.pausa_humana_minutos)


def _ya_escalada(conversacion: Conversacion) -> bool:
    """True si la conversación ya está escalada por el modelo.

    Distinto de `esta_en_modo_humano`/`_pausa_vigente`: es el chequeo propio
    de `escalar_a_humano` para no pisar un escalamiento con otro. Una pausa
    manual vigente (la secretaría ya está respondiendo) NO cuenta acá — si
    contara, un escalamiento del modelo que cae justo en esa ventana se
    perdería entero (sin resumen, sin escalada_en, sin aviso) y la
    conversación volvería al bot cuando la pausa manual expire, sin que nadie
    se haya enterado de que hacía falta un humano. Eso pasaba antes de que
    existiera `motivo_pausa`."""
    return conversacion.modo_humano and conversacion.motivo_pausa == MotivoPausa.ESCALAMIENTO


def esta_en_modo_humano(db, conversacion: Conversacion) -> bool:
    """Re-lee modo_humano de la base, sin confiar en lo que tenga cargado la
    sesión, y aplica la expiración por tiempo de la pausa manual (ver
    `_pausa_vigente`).

    Hace falta porque entre el chequeo de modo_humano de
    `procesar_mensaje_entrante` y el momento de enviar puede pasar bastante
    tiempo: la llamada al modelo tiene un presupuesto de 20 segundos, y en esa
    ventana otra entrega concurrente del mismo número puede haber escalado, o
    alguien del equipo puede haber marcado la conversación a mano. Si no se
    vuelve a mirar, el bot escribe encima de un humano — que es exactamente lo
    que el criterio de aceptación 4 del spec-etapa2.md prohíbe.
    """
    db.refresh(conversacion)
    return _pausa_vigente(conversacion, datetime.now(timezone.utc))


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
    identificador_externo = conversacion.identificador_externo

    if not aunque_este_en_modo_humano and esta_en_modo_humano(db, conversacion):
        logger.info(
            "La conversación con %s pasó a modo humano mientras se generaba la "
            "respuesta: no se envía nada para no escribir encima de la persona",
            enmascarar_identificador(identificador_externo),
        )
        return False

    try:
        kapso_client.enviar_mensaje_texto(identificador_externo, texto)
    except Exception:
        logger.exception("No se pudo enviar un mensaje a %s", enmascarar_identificador(identificador_externo))
        return False

    mensaje_bot = Mensaje(
        conversacion_id=conversacion.id,
        rol=RolMensaje.BOT,
        contenido=texto,
    )
    db.add(mensaje_bot)
    conversacion.ultimo_mensaje_en = datetime.now(timezone.utc)
    db.commit()

    logger.info("Mensaje enviado a %s: %s", enmascarar_identificador(identificador_externo), texto)
    return True


def escalar_a_humano(db, conversacion: Conversacion, resumen: str | None) -> None:
    """Marca la conversación en modo humano, guarda el resumen y avisa al
    usuario con el texto que corresponda según el horario. A partir de acá
    el bot no vuelve a responder en esta conversación.

    Si mientras se generaba la respuesta otra entrega ya escaló, no se vuelve
    a escalar: pisar el resumen del primer escalamiento con el del segundo le
    saca contexto a quien vaya a atender. El chequeo (`_ya_escalada`) es
    sobre un escalamiento previo puntualmente, no sobre modo_humano en
    general: una pausa manual vigente (la secretaría ya está respondiendo)
    no tiene que frenar esto. Si lo frenara, el escalamiento se perdería
    entero — sin resumen, sin escalada_en, sin aviso — y la conversación
    volvería sola al bot cuando la pausa manual expire.
    """
    db.refresh(conversacion)
    if _ya_escalada(conversacion):
        logger.info(
            "La conversación con %s ya estaba escalada, no se escala de nuevo",
            enmascarar_identificador(conversacion.identificador_externo),
        )
        return

    ahora = datetime.now(timezone.utc)
    conversacion.modo_humano = True
    conversacion.motivo_pausa = MotivoPausa.ESCALAMIENTO
    conversacion.modo_humano_desde = ahora
    conversacion.resumen_escalamiento = resumen
    conversacion.escalada_en = ahora
    db.commit()

    # Va acá, pegado al commit, y no al final: si el envío del aviso falla, el
    # escalamiento ya ocurrió igual y tiene que quedar en el log sí o sí.
    logger.warning(
        "ESCALADO A HUMANO — %s | resumen: %s",
        enmascarar_identificador(conversacion.identificador_externo), resumen,
    )

    ahora_local = datetime.now(config.timezone)
    aviso = mensajes.mensaje_escalamiento(ahora_local)
    if not enviar_y_guardar(db, conversacion, aviso, aunque_este_en_modo_humano=True):
        logger.error(
            "La conversación con %s quedó escalada pero NO se pudo enviar el aviso de "
            "escalamiento: la persona del equipo tiene que responder sin que el usuario "
            "sepa todavía que su consulta pasó a un humano.",
            enmascarar_identificador(conversacion.identificador_externo),
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
            enmascarar_identificador(conversacion.identificador_externo), error,
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
            "Falló la llamada al modelo para %s", enmascarar_identificador(conversacion.identificador_externo),
        )
        enviar_y_guardar(db, conversacion, mensajes.MENSAJE_ERROR_GENERICO)
        escalar_a_humano(db, conversacion, resumen="Error automático: no se pudo generar una respuesta.")
        return

    if (not resultado.texto or not resultado.texto.strip()) and not resultado.escalar:
        logger.error(
            "El modelo devolvió una respuesta vacía sin escalar para %s",
            enmascarar_identificador(conversacion.identificador_externo),
        )
        enviar_y_guardar(db, conversacion, mensajes.MENSAJE_ERROR_GENERICO)
        escalar_a_humano(db, conversacion, resumen="Error automático: el modelo no generó una respuesta.")
        return

    if resultado.texto:
        enviar_y_guardar(db, conversacion, resultado.texto)

    if resultado.escalar:
        escalar_a_humano(db, conversacion, resultado.resumen)


def procesar_mensaje_entrante(identificador_externo: str, wa_message_id: str, contenido: str) -> None:
    """Pasos 4 a 7 del flujo. Corre en background: la request del webhook ya
    devolvió 200 antes de que esto empiece.

    El webhook solo atiende WhatsApp por ahora, así que el canal queda
    hardcodeado acá; cuando exista otro canal, esta función pasa a recibirlo
    como parámetro en vez de asumirlo."""
    db = SessionLocal()
    try:
        ya_existe = db.query(Mensaje).filter_by(wa_message_id=wa_message_id).first()
        if ya_existe is not None:
            logger.info(
                "Mensaje duplicado de %s (wa_message_id=%s), se descarta",
                enmascarar_identificador(identificador_externo), wa_message_id,
            )
            return

        conversacion = buscar_o_crear_conversacion(db, CANAL_WHATSAPP, identificador_externo)

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
                enmascarar_identificador(identificador_externo), wa_message_id,
            )
            return

        pausado = _pausa_vigente(conversacion, datetime.now(timezone.utc))
        logger.info(
            "Mensaje de %s guardado (pausado=%s): %s",
            enmascarar_identificador(identificador_externo), pausado, contenido,
        )

        if pausado:
            return

        conteo_ultima_hora = mensajes_ultima_hora(db, CANAL_WHATSAPP, identificador_externo)
        if conteo_ultima_hora > config.limite_mensajes_hora:
            # Solo el mensaje que recién cruza el límite dispara el aviso.
            # Los siguientes, mientras siga por encima, quedan en silencio.
            if conteo_ultima_hora == config.limite_mensajes_hora + 1:
                logger.warning(
                    "Límite de mensajes por hora superado por %s (%s en la última hora)",
                    enmascarar_identificador(identificador_externo), conteo_ultima_hora,
                )
                enviar_y_guardar(db, conversacion, mensajes.MENSAJE_LIMITE_ALCANZADO)
            return

        responder(db, conversacion, mensaje_usuario)
    finally:
        db.close()


def registrar_intervencion_humana(db, conversacion: Conversacion, contenido: str, wa_message_id: str) -> None:
    """Guarda el mensaje de la secretaría (rol HUMANO, sin commit) y prende o
    reinicia la ventana de pausa por intervención manual.

    No commitea: lo hace quien llama (`procesar_mensaje_saliente`), para
    poder atrapar un IntegrityError de `wa_message_id` duplicado sin dejar a
    mitad de camino el cambio de modo_humano — un reintento del mismo evento
    no tiene que reiniciar la ventana (spec-pausa-por-intervencion-humana.md,
    sección 6).

    Si la conversación ya estaba escalada por el modelo (`_ya_escalada`), no
    se le cambia el motivo ni la fecha: el escalamiento no expira, y que la
    secretaría responda sobre esa conversación no cambia eso (sección 8 del
    spec). En cualquier otro caso -sin pausa previa, o con una pausa manual
    ya en curso, vencida o no- se prende o reinicia la ventana.
    """
    db.add(
        Mensaje(
            conversacion_id=conversacion.id,
            rol=RolMensaje.HUMANO,
            contenido=contenido,
            wa_message_id=wa_message_id,
        )
    )

    ahora = datetime.now(timezone.utc)
    if not _ya_escalada(conversacion):
        conversacion.modo_humano = True
        conversacion.motivo_pausa = MotivoPausa.INTERVENCION_MANUAL
        conversacion.modo_humano_desde = ahora
    conversacion.ultimo_mensaje_en = ahora


def procesar_mensaje_saliente(identificador_externo: str, wa_message_id: str, contenido: str) -> None:
    """Procesa un evento `whatsapp.message.sent` ya filtrado en
    `recibir_webhook` como `direction=outbound, origin=business_app` — la
    combinación que solo puede mandar una persona respondiendo desde la app
    de WhatsApp Business, nunca el bot (ver spec-pausa-por-intervencion-
    humana.md, sección 3). Corre en background, igual que
    `procesar_mensaje_entrante`.
    """
    db = SessionLocal()
    try:
        ya_existe = db.query(Mensaje).filter_by(wa_message_id=wa_message_id).first()
        if ya_existe is not None:
            logger.info(
                "Mensaje saliente de la secretaría a %s duplicado (wa_message_id=%s), se descarta sin tocar la pausa",
                enmascarar_identificador(identificador_externo), wa_message_id,
            )
            return

        conversacion = buscar_o_crear_conversacion(db, CANAL_WHATSAPP, identificador_externo)
        registrar_intervencion_humana(db, conversacion, contenido, wa_message_id)
        try:
            db.commit()
        except IntegrityError:
            # Mismo caso que en procesar_mensaje_entrante: dos entregas del
            # mismo evento llegaron a la vez y las dos pasaron el chequeo de
            # duplicado de arriba.
            db.rollback()
            logger.info(
                "Mensaje saliente de la secretaría a %s duplicado (wa_message_id=%s) detectado al guardar, "
                "se descarta sin tocar la pausa",
                enmascarar_identificador(identificador_externo), wa_message_id,
            )
            return

        logger.warning(
            "PAUSA POR INTERVENCIÓN MANUAL — %s (modo_humano_desde=%s)",
            enmascarar_identificador(identificador_externo), conversacion.modo_humano_desde,
        )
    finally:
        db.close()


def _extraer_contenido(mensaje: dict) -> str:
    """Traduce `message.type`/`message.text.body` del payload de Kapso al
    texto a guardar. Comparte esta lógica el mensaje entrante y el saliente
    de la secretaría: los dos pueden venir con tipos no soportados (una
    imagen, un audio)."""
    tipo = mensaje.get("type")
    if tipo == "text":
        return mensaje.get("text", {}).get("body", "")
    return f"[mensaje de tipo '{tipo}' no soportado en esta etapa]"


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

    if x_webhook_event == EVENTO_MENSAJE_RECIBIDO:
        payload = json.loads(cuerpo_crudo)
        mensaje = payload.get("message", {})
        conversacion_payload = payload.get("conversation", {})

        wa_message_id = mensaje.get("id")
        identificador_externo = mensaje.get("from") or conversacion_payload.get("phone_number")
        contenido = _extraer_contenido(mensaje)

        if not identificador_externo or not wa_message_id:
            logger.warning("Payload de webhook incompleto, se descarta: %s", payload)
            return {"status": "payload incompleto"}

        background_tasks.add_task(procesar_mensaje_entrante, identificador_externo, wa_message_id, contenido)
        return {"status": "ok"}

    if x_webhook_event == EVENTO_MENSAJE_ENVIADO:
        payload = json.loads(cuerpo_crudo)
        mensaje = payload.get("message", {})
        conversacion_payload = payload.get("conversation", {})
        kapso_info = mensaje.get("kapso") or {}

        # Requisito crítico (spec-pausa-por-intervencion-humana.md, sección
        # 3): chequeo explícito y positivo de los dos valores exactos, nunca
        # "si no es del bot, es humano". Si el bot manda un mensaje y ese
        # evento vuelve por acá como outbound+cloud_api, o si origin viene
        # ausente o con cualquier valor que no sea justo "business_app", no
        # se pausa — el bot no puede pausarse a sí mismo.
        if kapso_info.get("direction") != "outbound" or kapso_info.get("origin") != "business_app":
            logger.debug(
                "Evento %s ignorado (direction=%s, origin=%s)",
                x_webhook_event, kapso_info.get("direction"), kapso_info.get("origin"),
            )
            return {"status": "evento ignorado"}

        wa_message_id = mensaje.get("id")
        identificador_externo = mensaje.get("to") or conversacion_payload.get("phone_number")
        contenido = _extraer_contenido(mensaje)

        if not identificador_externo or not wa_message_id:
            logger.warning("Payload de whatsapp.message.sent incompleto, se descarta: %s", payload)
            return {"status": "payload incompleto"}

        background_tasks.add_task(procesar_mensaje_saliente, identificador_externo, wa_message_id, contenido)
        return {"status": "ok"}

    logger.debug("Evento de webhook ignorado: %s", x_webhook_event)
    return {"status": "evento ignorado"}


@app.get("/health")
def salud():
    return {"status": "ok"}
