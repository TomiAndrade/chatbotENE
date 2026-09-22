"""FastAPI: endpoints /webhook y /health.

Flujo de POST /webhook (ver spec-etapa1.md, spec-etapa2.md,
spec-meta-cloud-api.md y spec-agrupamiento-mensajes.md). El endpoint hace
solo lo mínimo para poder contestar 200 enseguida:

1. Verificar firma (X-Hub-Signature-256, Meta).
2. Si el evento no trae mensajes (p. ej. statuses[] de entrega), ignorar.
3. Extraer los datos del payload y encolar el procesamiento, uno por cada
   mensaje — un solo POST puede traer varios, de personas distintas.

Todo lo que toca la base de datos o la red corre después, en la background
task `procesar_mensaje_entrante`:

4. Deduplicar por wa_message_id.
5. Buscar o crear la conversación y guardar el mensaje entrante.
6. Si modo_humano, cortar acá.
7. Si el número superó el límite de mensajes por hora, cortar (avisando una
   sola vez).
8. Según el `type` real del mensaje (ver specs/spec-adjuntos-no-soportados.md):
   si es texto, sumarse al agrupamiento de la conversación
   (`agrupar_y_responder`, ver specs/spec-agrupamiento-mensajes.md) — espera
   una ventana breve por si llegan más mensajes seguidos, arma un solo lote,
   arma el historial y genera la respuesta, y según lo que haya devuelto el
   modelo la envía, escala a humano, o ambas cosas; si es cualquier otro tipo
   (imagen, documento, audio, etc.), responder con un texto fijo pidiendo la
   consulta por escrito, de inmediato y sin agrupar, sin llamar al modelo ni
   escalar por eso.

Starlette corre las background tasks en un threadpool y recién después de
haber mandado la respuesta HTTP, así que las llamadas bloqueantes de
SQLAlchemy y httpx no frenan el event loop. La contracara es que dos mensajes
seguidos del mismo número se procesan en paralelo: el chequeo de modo_humano
del paso 6 puede quedar viejo mientras el modelo piensa, así que se vuelve a
leer de la base justo antes de enviar (ver `esta_en_modo_humano`).

Con Kapso el webhook también atendía `whatsapp.message.sent` (ver
spec-pausa-por-intervencion-humana.md): el número estaba en modo
coexistencia, la secretaría respondía desde la app de WhatsApp Business sobre
el mismo número que usa el bot, y `procesar_mensaje_saliente` filtraba ese
evento a los mensajes `direction=outbound, origin=business_app` — la
combinación que solo podía mandar una persona desde la app, nunca el bot por
API — para prender o reiniciar la pausa por intervención manual.
`motivo_pausa` (`MotivoPausa` en app/models.py) distingue esa pausa de un
escalamiento del modelo: solo la manual expira, pasados
`PAUSA_HUMANA_MINUTOS` desde `modo_humano_desde`; un escalamiento
(`escalar_a_humano`) usa el mismo `modo_humano` pero no expira nunca, se
desmarca a mano.

**Con la Cloud API de Meta ese disparador no existe** (spec-meta-cloud-api.md,
sección 5): no hay app de negocio, los mensajes salen por API o no salen.
`procesar_mensaje_saliente` y `registrar_intervencion_humana` quedan
dormidos, no eliminados — POST /webhook ya no los llama, pero el código y sus
tests siguen ahí por si más adelante Meta habilita Coexistence.
"""

import enum
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app import mensajes
from app.config import config
from app.costo_meta import contabilizar_envio, liberar_reserva, mes_actual, reservar_gasto
from app.crm.auth import crm_habilitado
from app.crm.rutas import router as crm_router
from app.db import SessionLocal, crear_engine, init_db, obtener_engine
from app.historial import construir_historial
from app.limite import mensajes_ultima_hora
from app.meta import MetaClient, extraer_wa_message_id, verificar_challenge, verificar_firma_webhook
from app.models import CANAL_WHATSAPP, Conversacion, LlamadaIA, Mensaje, MotivoPausa, ResultadoLlamadaIA, RolMensaje
from app.pausa import pausa_vigente
from app.respuesta import ErrorTransitorioProveedor, generar_respuesta
from app.tiempos import Cronometro
from app.validacion_config import ConfigInvalida, resumen_config, validar_config

class _FormatterUTC(logging.Formatter):
    """`%(asctime)s` en UTC, no en la hora local del servidor.

    `logging.Formatter.converter` (el `time.struct_time` que arma `asctime`)
    apunta a `time.localtime` por default. En Render eso puede ser cualquier
    zona; las fechas que este proyecto guarda en la base son todas aware en
    UTC (ver DateTime(timezone=True) en app/models.py), así que si el log no
    coincide en zona, cruzar un timestamp del log con una fila de la base
    obliga a hacer la conversión a mano. Se pisa acá, en la clase, y no con
    `logging.Formatter.converter = time.gmtime` a nivel de módulo: eso
    afectaría a cualquier otro Formatter del proceso (incluido el de
    uvicorn, si en el futuro empieza a usar asctime), y este cambio tiene
    que quedar contenido al logger de la app.
    """

    converter = staticmethod(time.gmtime)


_handler = logging.StreamHandler()
_handler.setFormatter(
    _FormatterUTC(
        fmt="%(asctime)sZ [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
)
# `handlers=[...]` en vez de `format=`/`datefmt=`: así basicConfig usa este
# handler tal cual, sin construir uno propio que pisaría el formatter UTC de
# arriba. Solo toca el logger raíz — "uvicorn", "uvicorn.access" y
# "uvicorn.error" tienen sus propios handlers con propagate=False (ver
# uvicorn.config.LOGGING_CONFIG) y nunca llegan a este, así que no hay
# duplicado ni conflicto con lo que uvicorn imprime.
logging.basicConfig(level=logging.DEBUG if config.debug else logging.INFO, handlers=[_handler])
logger = logging.getLogger("bot")

app = FastAPI(title="Bot WhatsApp ENE IA LAB")
meta_client = MetaClient()

# El único `messages[].type` que la Cloud API de Meta manda y que hoy se
# trata como soportado (ver specs/spec-adjuntos-no-soportados.md). Es el
# default de `tipo` en `procesar_mensaje_entrante`, a propósito: antes de esa
# entrega todo mensaje se trataba como texto, y ningún llamador interno que
# no conozca la metadata real del webhook tiene por qué cambiar.
TIPO_TEXTO = "text"

# Referencia de módulo reemplazable, no una llamada directa a time.sleep en
# cada punto donde hace falta esperar (ver specs/spec-agrupamiento-mensajes.md,
# "Tests"). Los tests que necesitan controlar con precisión cuándo se
# intercala un mensaje nuevo durante la ventana de agrupamiento reemplazan
# `main.dormir` por una función sincronizada con threading.Event, en vez de
# depender de que el reloj real coincida con el tiempo de ejecución del test.
dormir = time.sleep

# El CRM (panel de conversaciones) se monta solo si está prendido: con
# CRM_HABILITADO=false no se registra ninguna de sus rutas y `/crm` responde
# 404 como cualquier URL inexistente. Un servidor que solo atiende el webhook
# no expone un panel que nadie configuró — y este servidor está publicado en
# internet para que le pegue Meta.
#
# El panel no agrega ningún middleware: el login es propio y la única cookie
# que existe es la de sesión, que pone y saca `app/crm/auth.py`. (Con Auth0
# hacía falta además el SessionMiddleware de Starlette para la cookie
# transitoria del flujo OIDC; se fue con el resto de aquel mecanismo.)
if crm_habilitado():
    app.include_router(crm_router)


@app.on_event("startup")
def al_iniciar() -> None:
    """Valida la configuración antes de tocar la base o aceptar requests
    (ver spec-validacion-config-arranque.md). Si falta o es incoherente, el
    proceso no debe levantar: se loguea todo lo que está mal y se corta acá,
    antes de crear_engine() — así una DATABASE_URL ausente no llega a crear
    ningún archivo SQLite ni deja el webhook respondiendo sobre una config
    rota. crear_engine() va antes de init_db() porque necesita el engine ya
    creado; los dos corren acá, en el hilo principal del startup, para que
    no exista ninguna ventana de concurrencia en la que crearlo (ver
    app/db.py sobre por qué no se crea "on demand")."""
    try:
        validar_config(config)
    except ConfigInvalida as error:
        logger.error(str(error))
        raise
    logger.info(resumen_config(config))
    crear_engine()
    init_db()

    if config.crm_habilitado:
        _revisar_crm()

    # Agrupamiento de mensajes (specs/spec-agrupamiento-mensajes.md): retoma
    # en background cualquier lote que haya quedado pendiente de un proceso
    # anterior. No se espera a que termine (dispara y sigue) — el server
    # tiene que poder aceptar requests nuevos ya, no en hasta 28s por cada
    # conversación recuperada.
    _recuperar_lotes_pendientes()


def _revisar_crm() -> None:
    """Dos chequeos del panel que necesitan la base ya abierta, así que no
    pueden vivir en `validar_config()`.

    1. **Que no queden tablas del login anterior (Auth0).** Este proyecto no
       tiene migraciones: `create_all` crea lo que falta y no modifica nada,
       así que una `crm_sesiones` de aquella época quedaría con su esquema
       viejo y sus filas viejas. Corta el arranque — es un error de estado de
       la base, no una advertencia.
    2. **Que exista al menos una cuenta.** No es un error: es el panel
       prendido antes de crear la primera cuenta, que es como queda recién
       prendido. Pero tiene que quedar dicho, porque el síntoma —nadie puede
       entrar y el login no dice por qué, a propósito— no se explica solo.
    """
    from app.crm import usuarios
    from app.crm.modelos import verificar_esquema

    verificar_esquema(obtener_engine())

    db = SessionLocal()
    try:
        if usuarios.cantidad(db) == 0:
            logger.warning(
                "CRM prendido y sin ninguna cuenta: no va a poder entrar nadie. "
                "Crear la primera con: python scripts/crm_usuario.py crear <usuario>"
            )
    finally:
        db.close()


@app.on_event("shutdown")
def al_apagar() -> None:
    meta_client.cerrar()


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


def _ya_escalada(conversacion: Conversacion) -> bool:
    """True si la conversación ya está escalada por el modelo.

    Distinto de `esta_en_modo_humano`/`pausa_vigente`: es el chequeo propio
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
    `pausa_vigente`, en app/pausa.py).

    Hace falta porque entre el chequeo de modo_humano de
    `procesar_mensaje_entrante` y el momento de enviar puede pasar bastante
    tiempo: la llamada al modelo tiene un presupuesto de 20 segundos, y en esa
    ventana otra entrega concurrente del mismo número puede haber escalado, o
    alguien del equipo puede haber marcado la conversación a mano. Si no se
    vuelve a mirar, el bot escribe encima de un humano — que es exactamente lo
    que el criterio de aceptación 4 del spec-etapa2.md prohíbe.
    """
    db.refresh(conversacion)
    return pausa_vigente(conversacion, datetime.now(timezone.utc))


class ResultadoEnvio(str, enum.Enum):
    """Qué pasó al llamar a `enviar_y_guardar()` (specs/spec-tope-duro-meta.md,
    etapa 2.1). Antes la función devolvía `bool`, y un `False` no alcanzaba
    para distinguir "la conversación ya está en modo humano" de "Meta
    rechazó el envío" de "no se llegó a intentar, bloqueado por
    presupuesto" — tres motivos con implicancias distintas para quien llama.
    `EXITOSO` es el único caso en el que el mensaje salió de verdad."""

    EXITOSO = "exitoso"
    MODO_HUMANO = "modo_humano"
    FALLO_META = "fallo_meta"
    BLOQUEADO_PRESUPUESTO = "bloqueado_presupuesto"


def enviar_y_guardar(
    db,
    conversacion: Conversacion,
    texto: str,
    aunque_este_en_modo_humano: bool = False,
) -> ResultadoEnvio:
    """Envía un texto por la Cloud API de Meta y, si se pudo mandar, lo guarda como mensaje
    del bot. Se usa tanto para la respuesta del modelo como para los avisos
    de escalamiento, límite y error: todos son mensajes "del bot" a efectos
    del historial.

    Antes de enviar vuelve a mirar modo_humano (ver `esta_en_modo_humano`) y
    descarta el mensaje si la conversación ya pasó a una persona. La única
    excepción es el aviso de escalamiento, que se manda justo después de
    prender modo_humano y por eso llega con
    `aunque_este_en_modo_humano=True`.

    Devuelve `ResultadoEnvio.EXITOSO` si el mensaje salió; `MODO_HUMANO` si
    se descartó porque la conversación ya pasó a una persona; `FALLO_META`
    si Meta rechazó o falló el envío; `BLOQUEADO_PRESUPUESTO` si
    `META_TOPE_DURO_HABILITADO=true` y el mes ya no tiene presupuesto para
    este envío — en ese caso ni siquiera se llega a llamar a Meta.

    Condición exacta para "contabilizado" (control preventivo de gasto,
    specs/spec-costo-whatsapp-meta.md): Meta tiene que haber aceptado el POST
    (no haber tirado `httpx.HTTPError`, sea cual sea el motivo) **y** haber
    devuelto un `wa_message_id` (`messages[0].id`). Si Meta rechaza el envío,
    no se contabiliza nada — se corta antes, en el `except` de abajo. Todo lo
    que sale de acá cuenta igual (respuesta del modelo, aviso de
    escalamiento, de límite o de error): los cuatro pasan por el mismo
    chequeo. Contabilizamos de forma preventiva a partir de esa aceptación:
    es una ESTIMACIÓN de gasto, no evidencia de facturación efectiva — el
    sistema no concilia todavía contra la factura real de Meta.

    Tope duro mensual (specs/spec-tope-duro-meta.md, etapa 2.1): con
    `META_TOPE_DURO_HABILITADO=false` (el default) esta función se comporta
    exactamente igual que antes de esta entrega — no reserva nada, no
    bloquea nada. Prendido, reserva la tarifa contra el presupuesto del mes
    ANTES de llamar a Meta (`reservar_gasto`, una única sentencia `UPDATE`
    atómica — nunca "sumar -> comprobar saldo -> enviar -> registrar", que
    dejaría una ventana de carrera entre envíos concurrentes). Si la reserva
    no entra, no se llama a Meta. Si Meta rechaza/falla el envío después de
    haber reservado, la reserva se libera (`liberar_reserva`). Si Meta
    acepta, la reserva queda — tanto si el guardado posterior de
    `Mensaje`/`EnvioWhatsapp` sale bien como si sale mal: liberarla en ese
    segundo caso dejaría que el tope real se corra hacia arriba cada vez que
    pase (ver el docstring de `PresupuestoMetaMensual`).
    """
    identificador_externo = conversacion.identificador_externo

    if not aunque_este_en_modo_humano and esta_en_modo_humano(db, conversacion):
        logger.info(
            "La conversación con %s pasó a modo humano mientras se generaba la "
            "respuesta: no se envía nada para no escribir encima de la persona",
            enmascarar_identificador(identificador_externo),
        )
        return ResultadoEnvio.MODO_HUMANO

    # Tope duro mensual (specs/spec-tope-duro-meta.md). Con el flag apagado
    # `mes_reservado` queda None y nada de lo de abajo se toca: ni se
    # reserva antes de llamar a Meta, ni se libera si Meta falla.
    mes_reservado = None
    if config.meta_tope_duro_habilitado:
        mes_reservado = mes_actual(datetime.now(timezone.utc))
        if not reservar_gasto(db, mes_reservado):
            logger.warning(
                "Envío a %s bloqueado por el tope mensual de presupuesto (mes=%s): no se llama a Meta",
                enmascarar_identificador(identificador_externo), mes_reservado,
            )
            return ResultadoEnvio.BLOQUEADO_PRESUPUESTO

    # Mide la llamada entera, reintentos y esperas de backoff incluidos (ver
    # MAX_REINTENTOS en app/meta.py): es lo que espera el usuario del otro
    # lado, no lo que tarda un intento suelto. Si hubo reintentos, quedan a la
    # vista en los WARNING que loguea el cliente de Meta.
    cronometro_envio = Cronometro()
    try:
        respuesta_meta = meta_client.enviar_mensaje_texto(identificador_externo, texto)
    except Exception:
        logger.info(
            "TIEMPOS %s | envío a Meta (fallo): %.0f ms",
            enmascarar_identificador(identificador_externo), cronometro_envio.ms(),
        )
        logger.exception("No se pudo enviar un mensaje a %s", enmascarar_identificador(identificador_externo))
        if mes_reservado is not None:
            liberar_reserva(db, mes_reservado)
        return ResultadoEnvio.FALLO_META
    logger.info(
        "TIEMPOS %s | envío a Meta: %.0f ms",
        enmascarar_identificador(identificador_externo), cronometro_envio.ms(),
    )

    # El id real que asignó Meta (ver app.meta.extraer_wa_message_id). Sin él
    # no hay con qué correlacionar sent -> delivered/read en una etapa futura
    # ni con qué contabilizar el costo estimado — nunca se inventa ni se
    # deriva del id interno de mensaje_bot.
    wa_message_id = extraer_wa_message_id(respuesta_meta)
    if not wa_message_id:
        logger.warning(
            "Meta aceptó el envío a %s pero la respuesta no trajo messages[0].id: "
            "queda sin wa_message_id y sin contabilizar en el costo estimado",
            enmascarar_identificador(identificador_externo),
        )

    mensaje_bot = Mensaje(
        conversacion_id=conversacion.id,
        rol=RolMensaje.BOT,
        contenido=texto,
        wa_message_id=wa_message_id,
    )
    db.add(mensaje_bot)
    conversacion.ultimo_mensaje_en = datetime.now(timezone.utc)

    if wa_message_id:
        # flush (no commit): hace falta el id de mensaje_bot para la FK de
        # EnvioWhatsapp, pero las dos filas se comitean juntas más abajo — no
        # puede quedar una sin la otra.
        db.flush()
        contabilizar_envio(db, conversacion, mensaje_bot)

    # Si esto falla (motivo de DB, no de Meta), la excepción se escapa sin
    # capturar hacia el except general de responder()/agrupar_y_responder —
    # mismo comportamiento que antes de esta entrega (ver
    # specs/spec-costo-whatsapp-meta.md, "Flujo inspeccionado"). La reserva
    # de más arriba, si la hubo, ya quedó comiteada en su propia
    # transacción y no se toca acá, a propósito: es justamente el caso
    # "Meta aceptó pero falló la persistencia" que el diseño del tope duro
    # deja intencionalmente sin liberar (ver PresupuestoMetaMensual).
    db.commit()

    logger.info("Mensaje enviado a %s: %s", enmascarar_identificador(identificador_externo), texto)
    return ResultadoEnvio.EXITOSO


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

    Con ESCALAMIENTO_HABILITADO=false no hace nada más que loguear: no hay
    bandeja de entrada donde alguien lea la conversación (spec-derivacion.md),
    así que prender modo_humano dejaría al usuario esperando a una persona que
    no va a llegar, y encima el bot no le volvería a contestar nunca más. El
    chequeo va acá y no en cada llamador a propósito: los caminos de error de
    `responder` escalaban aunque la herramienta no estuviera ni declarada, que
    es exactamente el agujero que esto tapa.
    """
    if not config.escalamiento_habilitado:
        logger.warning(
            "No se escala a humano (ESCALAMIENTO_HABILITADO=false) para %s. Motivo que "
            "lo habría disparado: %s",
            enmascarar_identificador(conversacion.identificador_externo), resumen,
        )
        return

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
    resultado_aviso = enviar_y_guardar(db, conversacion, aviso, aunque_este_en_modo_humano=True)
    if resultado_aviso != ResultadoEnvio.EXITOSO:
        logger.error(
            "La conversación con %s quedó escalada pero NO se pudo enviar el aviso de "
            "escalamiento (%s): la persona del equipo tiene que responder sin que el usuario "
            "sepa todavía que su consulta pasó a un humano.",
            enmascarar_identificador(conversacion.identificador_externo), resultado_aviso.value,
        )


def _registrar_llamada_ia(
    db,
    conversacion: Conversacion,
    *,
    resultado: ResultadoLlamadaIA,
    duracion_ms: float,
    escalo: bool,
    tokens_entrada: int | None = None,
    tokens_salida: int | None = None,
) -> None:
    """Una fila por cada llamada a generar_respuesta() desde responder(),
    para el dashboard de costos y actividad del CRM (ver
    specs/spec-dashboard-metricas.md). No guarda contenido de mensajes ni
    nada que identifique al contacto fuera del id numérico de la
    conversación.

    Se commitea en el acto, con su propio commit: es un registro de
    métricas, no algo que tenga que viajar atado a los commits de
    enviar_y_guardar/escalar_a_humano que vienen después en cada rama de
    responder()."""
    db.add(
        LlamadaIA(
            conversacion_id=conversacion.id,
            proveedor=config.proveedor_ia,
            modelo=config.modelo or None,
            duracion_ms=round(duracion_ms),
            resultado=resultado,
            escalo=escalo,
            tokens_entrada=tokens_entrada,
            tokens_salida=tokens_salida,
        )
    )
    db.commit()


def responder(db, conversacion: Conversacion, mensajes_agrupados: list[Mensaje]) -> None:
    """Arma el historial, genera la respuesta y actúa según lo que haya
    devuelto el modelo: enviar texto, escalar a humano, o ambas cosas. Si la
    llamada al modelo falla o vuelve vacía sin escalar, se avisa el error y
    se escala de todos modos: una conversación en manos de una persona es
    mejor que una conversación muerta.

    `mensajes_agrupados` es el lote armado por `agrupar_y_responder` (ver
    specs/spec-agrupamiento-mensajes.md) — uno o más mensajes de texto
    consecutivos de la misma conversación, en orden cronológico. Se
    concatenan con un salto de línea para `mensaje_nuevo`: se tratan como un
    solo turno del usuario, que es la intención de haberlos agrupado.

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
    `agrupar_y_responder` ya quedó vieja para cuando el modelo contesta."""
    identificador = enmascarar_identificador(conversacion.identificador_externo)
    # MENSAJE_ERROR_GENERICO dice "ya avisamos a una persona del equipo": solo
    # es verdad si el escalamiento está habilitado.
    mensaje_de_error = (
        mensajes.MENSAJE_ERROR_GENERICO
        if config.escalamiento_habilitado
        else mensajes.MENSAJE_ERROR_SIN_ESCALAMIENTO
    )

    cronometro_historial = Cronometro()
    historial = construir_historial(db, conversacion, mensajes_agrupados)
    logger.info(
        "TIEMPOS %s | historial: %.0f ms (%s mensajes, lote de %s)",
        identificador, cronometro_historial.ms(), len(historial), len(mensajes_agrupados),
    )

    mensaje_nuevo = "\n".join(m.contenido for m in mensajes_agrupados)

    # De punta a punta: incluye el reintento de con_un_reintento, la lectura
    # completa del cuerpo de la respuesta y el parseo, no solo el ida y vuelta
    # HTTP hasta los headers. El desglose por intento lo loguea
    # `con_un_reintento`, y el de headers vs. cuerpo el proveedor.
    cronometro_modelo = Cronometro()
    try:
        resultado = generar_respuesta(historial=historial, mensaje_nuevo=mensaje_nuevo)
    except ErrorTransitorioProveedor as error:
        duracion_ms = cronometro_modelo.ms()
        logger.info("TIEMPOS %s | modelo (fallo transitorio): %.0f ms", identificador, duracion_ms)
        logger.warning(
            "Error transitorio del proveedor de IA para %s: %s",
            enmascarar_identificador(conversacion.identificador_externo), error,
        )
        escala = not config.debug
        _registrar_llamada_ia(
            db, conversacion, resultado=ResultadoLlamadaIA.ERROR_TRANSITORIO,
            duracion_ms=duracion_ms, escalo=escala,
        )
        if config.debug:
            enviar_y_guardar(db, conversacion, mensajes.MENSAJE_ERROR_TRANSITORIO)
        else:
            enviar_y_guardar(db, conversacion, mensaje_de_error)
            escalar_a_humano(
                db, conversacion,
                resumen="Error automático: error transitorio del proveedor de IA.",
            )
        return
    except Exception:
        duracion_ms = cronometro_modelo.ms()
        logger.info("TIEMPOS %s | modelo (fallo): %.0f ms", identificador, duracion_ms)
        logger.exception(
            "Falló la llamada al modelo para %s", enmascarar_identificador(conversacion.identificador_externo),
        )
        _registrar_llamada_ia(
            db, conversacion, resultado=ResultadoLlamadaIA.ERROR, duracion_ms=duracion_ms, escalo=True,
        )
        enviar_y_guardar(db, conversacion, mensaje_de_error)
        escalar_a_humano(db, conversacion, resumen="Error automático: no se pudo generar una respuesta.")
        return

    duracion_ms = cronometro_modelo.ms()
    logger.info("TIEMPOS %s | modelo (punta a punta): %.0f ms", identificador, duracion_ms)

    if (not resultado.texto or not resultado.texto.strip()) and not resultado.escalar:
        logger.error(
            "El modelo devolvió una respuesta vacía sin escalar para %s",
            enmascarar_identificador(conversacion.identificador_externo),
        )
        _registrar_llamada_ia(
            db, conversacion, resultado=ResultadoLlamadaIA.VACIO, duracion_ms=duracion_ms, escalo=True,
            tokens_entrada=resultado.tokens_entrada, tokens_salida=resultado.tokens_salida,
        )
        enviar_y_guardar(db, conversacion, mensaje_de_error)
        escalar_a_humano(db, conversacion, resumen="Error automático: el modelo no generó una respuesta.")
        return

    _registrar_llamada_ia(
        db, conversacion, resultado=ResultadoLlamadaIA.OK, duracion_ms=duracion_ms, escalo=resultado.escalar,
        tokens_entrada=resultado.tokens_entrada, tokens_salida=resultado.tokens_salida,
    )

    if resultado.texto:
        enviar_y_guardar(db, conversacion, resultado.texto)

    if resultado.escalar:
        escalar_a_humano(db, conversacion, resultado.resumen)


# --- Agrupamiento de mensajes consecutivos (specs/spec-agrupamiento-mensajes.md) --


def _reclamar_generacion(db, conversacion: Conversacion) -> str | None:
    """Intenta tomar la reserva de "estoy generando una respuesta para esta
    conversación" con un único UPDATE condicional — atómico a nivel de fila
    sin importar cuántos hilos o procesos lo intenten a la vez, porque lo
    resuelve el motor de base, no este proceso (misma idea que la dedup por
    wa_message_id, ver CLAUDE.md).

    Devuelve un token si la reserva se tomó (rowcount == 1), o None si ya
    había una vigente — tomada hace menos de AGRUPAR_ABANDONO_SEGUNDOS por
    cualquier dueño, este proceso u otro. Una reserva más vieja que eso se
    considera abandonada (el dueño se cayó a mitad de camino) y se puede
    retomar."""
    ahora = datetime.now(timezone.utc)
    umbral_abandono = ahora - timedelta(seconds=config.agrupar_abandono_segundos)
    token = uuid.uuid4().hex

    filas = (
        db.query(Conversacion)
        .filter(
            Conversacion.id == conversacion.id,
            or_(Conversacion.generando_desde.is_(None), Conversacion.generando_desde < umbral_abandono),
        )
        # synchronize_session=False: sin esto, Query.update() reevalúa el
        # WHERE en Python contra el objeto ya cargado en la sesión, y ese
        # objeto puede traer generando_desde naive (SQLite lo devuelve así
        # aunque la columna sea DateTime(timezone=True), ver el docstring de
        # pausa_vigente en app/pausa.py) — comparado contra `umbral_abandono`
        # (aware) revienta con TypeError. No hace falta que el ORM sincronice
        # nada acá: donde importa tener el valor fresco ya se hace
        # db.refresh(conversacion) explícito.
        .update({"generando_desde": ahora, "generando_token": token}, synchronize_session=False)
    )
    db.commit()
    return token if filas == 1 else None


def _liberar_generacion(db, conversacion: Conversacion, token: str) -> None:
    """Suelta la reserva, pero solo si `token` sigue siendo el dueño actual.

    Sin esta condición, liberar "a ciegas" (solo mirando que generando_desde
    no sea NULL) podría borrarle la reserva a otro proceso que la retomó de
    buena fe creyendo que la nuestra había quedado abandonada — exactamente
    el caso de un procesamiento que tardó más de AGRUPAR_ABANDONO_SEGUNDOS
    sin haberse caído de verdad."""
    db.query(Conversacion).filter(
        Conversacion.id == conversacion.id,
        Conversacion.generando_token == token,
    ).update({"generando_desde": None, "generando_token": None}, synchronize_session=False)
    db.commit()


def _avanzar_marca_de_agrupado(db, conversacion: Conversacion, mensaje_id: int) -> None:
    """Mueve `ultimo_mensaje_agrupado_id` a `mensaje_id`, pero nunca hacia
    atrás — mismo patrón de UPDATE condicional que `_reclamar_generacion`,
    por la misma razón: hay más de un llamador que puede escribir esta
    columna sin coordinarse entre sí (el dueño del lote, al terminar de
    responder; y `procesar_mensaje_entrante`, cuando un mensaje de texto no
    llega a entrar a ningún lote porque superó el límite por hora). Sin un
    UPDATE atómico que solo avance el valor, el que commitea último "gana"
    sin importar cuál de los dos mensajes es más nuevo, y un mensaje que ya
    se marcó como fuera de lote podría volver a quedar pendiente."""
    db.query(Conversacion).filter(
        Conversacion.id == conversacion.id,
        or_(
            Conversacion.ultimo_mensaje_agrupado_id.is_(None),
            Conversacion.ultimo_mensaje_agrupado_id < mensaje_id,
        ),
    ).update({"ultimo_mensaje_agrupado_id": mensaje_id}, synchronize_session=False)
    db.commit()


def _mensajes_pendientes_de_texto(db, conversacion: Conversacion) -> list[Mensaje]:
    """Mensajes de texto (rol usuario, tipo TIPO_TEXTO) todavía no incluidos
    en ningún lote procesado, en orden cronológico. Filtra por `tipo`, no por
    el contenido guardado — un adjunto no soportado nunca entra acá, sin
    importar qué texto tenga su placeholder (ver
    specs/spec-adjuntos-no-soportados.md sobre por qué esa distinción no se
    hace por coincidencia textual)."""
    consulta = db.query(Mensaje).filter(
        Mensaje.conversacion_id == conversacion.id,
        Mensaje.rol == RolMensaje.USUARIO,
        Mensaje.tipo == TIPO_TEXTO,
    )
    if conversacion.ultimo_mensaje_agrupado_id is not None:
        consulta = consulta.filter(Mensaje.id > conversacion.ultimo_mensaje_agrupado_id)
    return consulta.order_by(Mensaje.creado_en.asc(), Mensaje.id.asc()).all()


def _ultimo_id_pendiente(db, conversacion: Conversacion) -> int | None:
    lote = _mensajes_pendientes_de_texto(db, conversacion)
    return lote[-1].id if lote else None


def _esperar_ventana_de_agrupamiento(db, conversacion: Conversacion) -> None:
    """Espera en vueltas de AGRUPAR_VENTANA_SEGUNDOS, y se corta apenas pasa
    una vuelta entera sin que llegue nada nuevo (ráfaga terminada) o al
    llegar a AGRUPAR_ESPERA_MAXIMA_SEGUNDOS desde que arrancó (tope duro,
    para que una ráfaga continua no posponga la respuesta para siempre)."""
    inicio = datetime.now(timezone.utc)
    ultimo_id_visto = _ultimo_id_pendiente(db, conversacion)
    while True:
        dormir(config.agrupar_ventana_segundos)
        ultimo_id_ahora = _ultimo_id_pendiente(db, conversacion)
        if ultimo_id_ahora == ultimo_id_visto:
            return
        if (datetime.now(timezone.utc) - inicio).total_seconds() >= config.agrupar_espera_maxima_segundos:
            return
        ultimo_id_visto = ultimo_id_ahora


def agrupar_y_responder(db, conversacion: Conversacion) -> None:
    """Punto de entrada del agrupamiento para un mensaje de texto recién
    guardado (ver specs/spec-agrupamiento-mensajes.md). Quien no gana la
    reserva no hace nada más: su mensaje ya quedó guardado y el dueño actual
    lo va a recoger, sea porque todavía está esperando (se suma al mismo
    lote) o porque ya terminó y vuelve a mirar si hay pendientes (arranca un
    lote nuevo, sin soltar la reserva entre uno y otro)."""
    token = _reclamar_generacion(db, conversacion)
    if token is None:
        logger.info(
            "Ya hay una generación en curso para %s, este mensaje se suma al lote en curso",
            enmascarar_identificador(conversacion.identificador_externo),
        )
        return

    try:
        while True:
            _esperar_ventana_de_agrupamiento(db, conversacion)

            db.refresh(conversacion)
            if pausa_vigente(conversacion, datetime.now(timezone.utc)):
                logger.info(
                    "La conversación con %s pasó a modo humano mientras se agrupaba: el "
                    "lote pendiente queda para la próxima vez que alguien escriba",
                    enmascarar_identificador(conversacion.identificador_externo),
                )
                return

            lote = _mensajes_pendientes_de_texto(db, conversacion)
            if not lote:
                return

            logger.info(
                "TIEMPOS %s | lote agrupado: %s mensaje(s)",
                enmascarar_identificador(conversacion.identificador_externo), len(lote),
            )

            responder(db, conversacion, lote)

            # Se actualiza recién después de que responder() vuelve, nunca
            # antes: así un reinicio a mitad de la llamada al modelo no
            # pierde el lote, lo reprocesa (ver "Reinicios y recuperación"
            # en el spec). El avance es monótono (ver
            # `_avanzar_marca_de_agrupado`) porque `procesar_mensaje_entrante`
            # también puede escribir esta columna, sin tomar la reserva, para
            # un mensaje que quedó fuera de lote por el límite por hora.
            _avanzar_marca_de_agrupado(db, conversacion, lote[-1].id)

            db.refresh(conversacion)
            if not _mensajes_pendientes_de_texto(db, conversacion):
                return
            # Llegaron mensajes nuevos mientras se generaba la respuesta: se
            # vuelve a esperar la ventana para ese lote nuevo, sin soltar la
            # reserva — es la misma conversación, el mismo dueño.
    finally:
        _liberar_generacion(db, conversacion, token)


def _conversaciones_con_texto_pendiente(db) -> list[int]:
    """Ids de conversación con al menos un mensaje de texto de usuario que
    todavía no entró a ningún lote (ver `_mensajes_pendientes_de_texto`), sin
    importar el estado de la reserva — eso lo arbitra `_reclamar_generacion`
    cuando se intente recuperar cada una."""
    filas = (
        db.query(Mensaje.conversacion_id)
        .join(Conversacion, Conversacion.id == Mensaje.conversacion_id)
        .filter(
            Mensaje.rol == RolMensaje.USUARIO,
            Mensaje.tipo == TIPO_TEXTO,
            or_(
                Conversacion.ultimo_mensaje_agrupado_id.is_(None),
                Mensaje.id > Conversacion.ultimo_mensaje_agrupado_id,
            ),
        )
        .distinct()
        .all()
    )
    return [id_ for (id_,) in filas]


def _recuperar_lote_pendiente(conversacion_id: int) -> None:
    """Reintenta `agrupar_y_responder` para una conversación puntual, con su
    propia sesión — pensada para correr en su propio hilo, ver
    `_recuperar_lotes_pendientes`."""
    db = SessionLocal()
    try:
        conversacion = db.query(Conversacion).filter_by(id=conversacion_id).one_or_none()
        if conversacion is None:
            return
        agrupar_y_responder(db, conversacion)
    except Exception:
        logger.error(
            "Error inesperado recuperando el lote pendiente de la conversación id=%s",
            conversacion_id, exc_info=True,
        )
    finally:
        db.close()


def _recuperar_lotes_pendientes() -> list[threading.Thread]:
    """Al arrancar, retoma cualquier lote de agrupamiento que haya quedado a
    medias porque el proceso anterior murió con la reserva tomada (ver
    specs/spec-agrupamiento-mensajes.md, "Reinicios y recuperación"). Sin
    esto, esos mensajes quedan esperando en silencio hasta que la persona
    escriba un mensaje nuevo — podían pasar horas, o no pasar nunca.

    Un hilo por conversación, para no bloquear el arranque del server con
    hasta `AGRUPAR_ESPERA_MAXIMA_SEGUNDOS` + `PRESUPUESTO_TOTAL_SEGUNDOS` de
    cada una. **Funciona igual con más de un proceso corriendo**: no asume
    que este proceso es el único que puede haber quedado con la reserva, ni
    el único que arranca a la vez que otro — `agrupar_y_responder` sigue
    arbitrando todo a través de `_reclamar_generacion`, así que si la
    reserva de una conversación sigue vigente (otro proceso la tomó hace
    poco y todavía la está usando de verdad) este hilo no hace nada
    (`token is None`), sin pisarle el trabajo a nadie.

    Devuelve los hilos que arrancó — `al_iniciar()` no los espera (dispara y
    sigue), pero los tests sí, para poder afirmar el resultado antes de
    terminar."""
    db = SessionLocal()
    try:
        ids_pendientes = _conversaciones_con_texto_pendiente(db)
    finally:
        db.close()

    if ids_pendientes:
        logger.info("Recuperando %s conversación(es) con un lote de agrupamiento pendiente", len(ids_pendientes))

    hilos = []
    for conversacion_id in ids_pendientes:
        hilo = threading.Thread(target=_recuperar_lote_pendiente, args=(conversacion_id,), daemon=True)
        hilo.start()
        hilos.append(hilo)
    return hilos


def responder_adjunto_no_soportado(db, conversacion: Conversacion, tipo: str | None) -> None:
    """Un mensaje que no es texto (imagen, documento, audio, o cualquier otro
    `type` que la Cloud API de Meta pueda mandar) no arma historial ni llama
    a `generar_respuesta`, y no escala por sí mismo: solo responde con el
    texto fijo que corresponda y pide la consulta por escrito (ver
    specs/spec-adjuntos-no-soportados.md). `enviar_y_guardar` se encarga de
    releer modo_humano antes de mandar nada, igual que con cualquier otro
    envío."""
    enviar_y_guardar(db, conversacion, mensajes.mensaje_adjunto_no_soportado(tipo))


def procesar_mensaje_entrante(
    identificador_externo: str,
    wa_message_id: str,
    contenido: str,
    tipo: str | None = TIPO_TEXTO,
    *,
    disparar_agrupamiento: bool = True,
) -> None:
    """Pasos 4 a 8 del flujo. Corre en background: la request del webhook ya
    devolvió 200 antes de que esto empiece.

    Los primeros cuatro parámetros son los de siempre, en el mismo orden
    que antes de esta entrega (ver specs/spec-meta-cloud-api.md, "principio
    rector", y specs/spec-adjuntos-no-soportados.md sobre `tipo`): nada que
    ya llame a esta función con esos cuatro argumentos necesita tocarse.
    `disparar_agrupamiento` es nuevo, solo por keyword y con default `True`
    — ese default preserva el comportamiento de siempre (guardar Y generar
    la respuesta agrupada en la misma llamada) para cualquier llamador que
    no sepa nada del agrupamiento (los tests que llaman esta función
    directo, y el caso común del webhook con un solo mensaje por contacto
    en el payload).

    **`disparar_agrupamiento=False` es lo que usa `_procesar_cambio` cuando
    un mismo payload de Meta trae más de un mensaje de texto del mismo
    contacto** (ver specs/spec-agrupamiento-mensajes.md, "Varios mensajes de
    texto del mismo contacto en un mismo POST"). Starlette ejecuta las
    `BackgroundTasks` de una misma respuesta en orden, una después de la
    otra — no en paralelo —, así que si esta función agrupara y respondiera
    ahí mismo, el primer mensaje se quedaría esperando la ventana de
    agrupamiento (y después llamando al modelo, hasta 20s) antes de que el
    segundo mensaje del mismo contacto llegara siquiera a guardarse. Con
    `disparar_agrupamiento=False` esta función solo hace los pasos 4 a 7
    (guardar, pausa, límite) y vuelve enseguida; es `_procesar_cambio` quien
    encola una tarea aparte, después de todas las de guardado, para recién
    ahí llamar a `agrupar_y_responder` — momento en el que todos los
    mensajes del payload ya están guardados y el lote los encuentra a
    todos.

    `tipo` decide, recién en el paso 8, si se agrupa (`tipo == TIPO_TEXTO`)
    o si alcanza con el texto fijo de adjunto no soportado
    (`responder_adjunto_no_soportado`, que no espera nada y no le importa
    `disparar_agrupamiento`). Nunca se decide comparando `contenido` contra
    el marcador — un usuario que escribe a mano algo parecido al marcador
    tiene `tipo == "text"` y sigue el camino normal.

    El webhook solo atiende WhatsApp por ahora, así que el canal queda
    hardcodeado acá; cuando exista otro canal, esta función pasa a recibirlo
    como parámetro en vez de asumirlo.

    El `except Exception` de más abajo (además del `except IntegrityError`
    puntual del commit) es a propósito: Starlette corre las background tasks
    después de haber mandado la respuesta HTTP (`Response.__call__` hace
    `send` del 200 y recién después `await background()`), así que para
    cuando algo revienta acá ya no hay respuesta que cambiar. Sin este catch,
    una excepción cualquiera se escapa de esta función, no pasa por
    `logger("bot")`, y termina en el logger de uvicorn — sin
    `identificador_externo` ni `wa_message_id`, y sin que quede registrado
    como el error de negocio que es. El mensaje del usuario se pierde en
    silencio: Meta ya recibió el 200 y no reintenta."""
    # Arranca antes de SessionLocal() a propósito: pedir la sesión puede
    # implicar abrir una conexión nueva contra Postgres, y eso también es
    # tiempo que el usuario espera.
    cronometro_total = Cronometro()
    db = SessionLocal()
    try:
        cronometro_guardado = Cronometro()
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
            tipo=tipo,
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

        logger.info(
            "TIEMPOS %s | guardado del mensaje entrante: %.0f ms",
            enmascarar_identificador(identificador_externo), cronometro_guardado.ms(),
        )

        pausado = pausa_vigente(conversacion, datetime.now(timezone.utc))
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
            if tipo == TIPO_TEXTO:
                # Este mensaje nunca va a entrar a un lote: sin esto, la
                # tarea de agrupamiento que `_procesar_cambio` encola aparte
                # (con `disparar_agrupamiento=False` acá arriba, ver el
                # docstring de esta función) lo encontraría igual como
                # pendiente y llamaría al modelo pasando por encima del
                # límite — `agrupar_y_responder` no conoce el límite por
                # hora, solo mira qué quedó sin marcar. El avance es
                # monótono (`_avanzar_marca_de_agrupado`) porque puede
                # correr en paralelo con el dueño de un lote en curso.
                _avanzar_marca_de_agrupado(db, conversacion, mensaje_usuario.id)
            return

        if tipo == TIPO_TEXTO:
            if disparar_agrupamiento:
                agrupar_y_responder(db, conversacion)
            # Si no, `_procesar_cambio` ya encoló una tarea aparte que va a
            # llamar a agrupar_y_responder después de que todos los mensajes
            # de este mismo payload se hayan guardado.
        else:
            responder_adjunto_no_soportado(db, conversacion, tipo)
    except Exception as error:
        logger.error(
            "Error inesperado procesando el mensaje de %s (wa_message_id=%s): %s: %s",
            enmascarar_identificador(identificador_externo), wa_message_id, type(error).__name__, error,
            exc_info=True,
        )
    finally:
        # En el finally para que salga por todos los caminos, incluidos los
        # que cortan antes de responder (duplicado, pausa, límite). Lo que no
        # cierra contra las etapas de arriba se fue en el resto: commits,
        # relecturas de modo humano, el conteo del límite.
        logger.info(
            "TIEMPOS %s | total del procesamiento: %.0f ms",
            enmascarar_identificador(identificador_externo), cronometro_total.ms(),
        )
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
    """Traduce `type`/`text.body` de un mensaje del payload de Meta (o del
    payload aún con forma de Kapso que usa el saliente dormido) al texto a
    guardar. Comparte esta lógica el mensaje entrante y el saliente de la
    secretaría: los dos pueden venir con tipos no soportados (una imagen, un
    audio). Solo guarda el marcador para historial y diagnóstico — qué se le
    responde al usuario por un tipo no soportado lo decide
    `responder_adjunto_no_soportado`, a partir del `type` real, no de este
    texto."""
    tipo = mensaje.get("type")
    if tipo == TIPO_TEXTO:
        return mensaje.get("text", {}).get("body", "")
    return f"[mensaje de tipo '{tipo}' no soportado en esta etapa]"


@app.get("/webhook")
def verificar_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    """Meta valida la URL del webhook con este GET antes de empezar a mandar
    eventos (spec-meta-cloud-api.md, sección 1). Si el modo y el verify token
    coinciden con lo configurado, hay que devolver `hub.challenge` tal cual,
    como texto plano — ni JSON ni comillas."""
    if verificar_challenge(hub_mode, hub_verify_token):
        return PlainTextResponse(hub_challenge or "")
    logger.warning("Verificación de webhook con verify token inválido")
    raise HTTPException(status_code=403, detail="Verify token inválido")


@app.post("/webhook")
async def recibir_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
):
    """Meta no manda un header de tipo de evento: hay que inspeccionar la
    estructura del payload (spec-meta-cloud-api.md, sección 2). Siempre
    devuelve 200 salvo firma inválida — un 4xx/5xx hace que Meta reintente y,
    si se repite, que desuscriba el webhook."""
    # La firma se calcula sobre el body crudo, por eso hay que leerlo antes
    # de que algo lo parsee como JSON. Es el único punto async del archivo.
    cuerpo_crudo = await request.body()

    if not verificar_firma_webhook(cuerpo_crudo, x_hub_signature_256):
        logger.warning("Firma de webhook inválida, se descarta la request")
        raise HTTPException(status_code=401, detail="Firma inválida")

    try:
        payload = json.loads(cuerpo_crudo)
    except json.JSONDecodeError:
        logger.warning("Body de webhook no es JSON válido, se ignora: %s", _cuerpo_para_loguear(cuerpo_crudo))
        return {"status": "ok"}

    try:
        for entry in payload.get("entry", []):
            for cambio in entry.get("changes", []):
                _procesar_cambio(cambio.get("value") or {}, background_tasks)
    except (AttributeError, TypeError, KeyError) as error:
        # El payload no tiene la forma que esperamos (un campo del tipo que
        # no es, algo donde iba una lista, etc.) — no es un bug propio, es
        # un payload que no anticipamos. 200 igual: un 4xx/5xx hace que Meta
        # reintente y, si se repite, que desuscriba el webhook.
        logger.warning(
            "Payload de webhook con estructura inesperada (%s: %s), se ignora: %s",
            type(error).__name__, error, _cuerpo_para_loguear(cuerpo_crudo),
        )
    except Exception:
        # Cualquier otra excepción es sospechosa de ser un bug propio del
        # procesamiento, no un problema del payload. Sin este log a nivel
        # ERROR con traceback, un bug acá se traga el mensaje en silencio:
        # Meta ya recibió el 200 y no reintenta.
        logger.error(
            "Error inesperado procesando el webhook, se ignora igual. Payload: %s",
            _cuerpo_para_loguear(cuerpo_crudo), exc_info=True,
        )

    return {"status": "ok"}


def _cuerpo_para_loguear(cuerpo_crudo: bytes, limite: int = 2000) -> str:
    """El body crudo del webhook, recortado para no inundar el log con un
    payload gigante (por ejemplo uno con muchos mensajes en el mismo entry)."""
    texto = cuerpo_crudo.decode("utf-8", errors="replace")
    if len(texto) > limite:
        return texto[:limite] + f"... ({len(texto)} caracteres en total)"
    return texto


def _disparar_agrupamiento_pendiente(identificador_externo: str) -> None:
    """Llama a `agrupar_y_responder` para una conversación, en su propia
    background task — la que `_procesar_cambio` encola después de TODAS las
    de guardado de un mismo payload (ver el docstring de
    `procesar_mensaje_entrante` sobre `disparar_agrupamiento`, y
    specs/spec-agrupamiento-mensajes.md). Para cuando esto corre, cualquier
    mensaje hermano del mismo contacto en el mismo payload ya se guardó, así
    que el lote los encuentra a todos — sin importar si son uno o varios.

    Es seguro llamarla aunque, al final, no haya nada pendiente (por
    ejemplo, si el único mensaje de texto de ese contacto en el payload
    resultó ser un duplicado, o la conversación está pausada):
    `agrupar_y_responder` ya maneja esos casos como no-op."""
    db = SessionLocal()
    try:
        conversacion = buscar_o_crear_conversacion(db, CANAL_WHATSAPP, identificador_externo)
        agrupar_y_responder(db, conversacion)
    except Exception:
        logger.error(
            "Error inesperado dispando el agrupamiento pendiente de %s",
            enmascarar_identificador(identificador_externo), exc_info=True,
        )
    finally:
        db.close()


def _procesar_cambio(value: dict, background_tasks: BackgroundTasks) -> None:
    """Un `changes[].value` del payload de Meta. Si no trae `messages`, es un
    evento de otro tipo (típicamente `statuses[]`, la confirmación de
    entrega) y no hay nada que hacer: se ignora explícitamente, no por
    descarte (spec-meta-cloud-api.md, sección 2).

    Encola dos tandas de background tasks, en este orden — Starlette las
    corre en el orden en que se agregan, una después de la otra (ver
    specs/spec-agrupamiento-mensajes.md, "Varios mensajes de texto del mismo
    contacto en un mismo POST"):

    1. Una por cada mensaje del payload, con `disparar_agrupamiento=False`:
       guardan (dedup, pausa, límite) y, si es un adjunto, responden de
       inmediato — pero ningún mensaje de texto agrupa ni llama al modelo
       todavía.
    2. Una por cada contacto distinto que tuvo al menos un mensaje de texto
       en este payload, recién después de todas las de arriba: ahí sí se
       dispara `agrupar_y_responder`, cuando ya no puede quedar ningún
       mensaje hermano sin guardar.
    """
    mensajes_entrantes = value.get("messages")
    if not mensajes_entrantes:
        logger.debug("Cambio de webhook sin messages (statuses u otro evento), se ignora: %s", value)
        return

    identificadores_con_texto: list[str] = []

    for mensaje in mensajes_entrantes:
        wa_message_id = mensaje.get("id")
        # `from` va tal cual llega, sin normalizar (spec-meta-cloud-api.md,
        # sección 3): el "9" de los números argentinos puede estar o no, y
        # tocarlo rompe la búsqueda de conversación o el envío.
        identificador_externo = mensaje.get("from")
        tipo = mensaje.get("type")
        contenido = _extraer_contenido(mensaje)

        if not identificador_externo or not wa_message_id:
            logger.warning("Mensaje de webhook incompleto, se descarta: %s", mensaje)
            continue

        # `tipo` va siempre explícito, incluido cuando es None (payload sin
        # `type`): este es el único llamador que conoce la metadata real del
        # mensaje, así que es el único que no puede confiar en el default de
        # `procesar_mensaje_entrante`.
        background_tasks.add_task(
            procesar_mensaje_entrante,
            identificador_externo, wa_message_id, contenido, tipo=tipo, disparar_agrupamiento=False,
        )

        if tipo == TIPO_TEXTO and identificador_externo not in identificadores_con_texto:
            identificadores_con_texto.append(identificador_externo)

    for identificador_externo in identificadores_con_texto:
        background_tasks.add_task(_disparar_agrupamiento_pendiente, identificador_externo)


@app.get("/health")
def salud():
    return {"status": "ok"}
