"""Camino único para enviar mensajes salientes por Meta y persistirlos.

No importa ``app.main``: así el CRM podrá reutilizar este flujo cuando tenga
respuesta manual sin crear una dependencia circular con el webhook.
"""

import enum
import logging
from dataclasses import dataclass
from datetime import timedelta
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import atencion
from app.config import config
from app.costo_meta import contabilizar_envio, liberar_reserva, mes_actual, reservar_gasto
from app.meta import MetaClient, extraer_wa_message_id
from app.models import Conversacion, Mensaje, RolMensaje
from app.pausa import pausa_vigente
from app.tiempos import Cronometro


logger = logging.getLogger("bot")
meta_client = MetaClient()


def enmascarar_identificador(identificador: str) -> str:
    """Muestra los primeros 4 y los últimos 2 caracteres, el resto tapado.

    Pensada originalmente para números de WhatsApp, pero no asume ese formato:
    no valida dígitos ni longitud de país, solo tapa el medio de la cadena.
    Sirve igual para un id de sesión de otro canal.
    """
    if len(identificador) <= 6:
        return "*" * len(identificador)
    return identificador[:4] + "*" * (len(identificador) - 6) + identificador[-2:]


def esta_en_modo_humano(db, conversacion: Conversacion) -> bool:
    """Re-lee la pausa de la base justo antes de enviar un mensaje.

    La lectura previa al llamado al modelo puede haber quedado vieja mientras
    se generaba la respuesta. ``pausa_vigente`` conserva además la semántica
    de expiración de la pausa manual.
    """
    db.refresh(conversacion)
    return pausa_vigente(conversacion, datetime.now(timezone.utc))


class ResultadoEnvio(str, enum.Enum):
    """Qué pasó al llamar a ``enviar_y_guardar``."""

    EXITOSO = "exitoso"
    MODO_HUMANO = "modo_humano"
    FALLO_META = "fallo_meta"
    BLOQUEADO_PRESUPUESTO = "bloqueado_presupuesto"
    FUERA_DE_VENTANA = "fuera_de_ventana"


@dataclass(frozen=True)
class EnvioProcesado:
    """Resultado detallado para callers que necesitan el Mensaje creado."""

    resultado: ResultadoEnvio
    mensaje: Mensaje | None = None


VENTANA_TEXTO_LIBRE = timedelta(hours=24)


def _ultimo_mensaje_del_usuario(db: Session, conversacion_id: int) -> Mensaje | None:
    """Último entrante según el reloj local de procesamiento del webhook.

    `creado_en` no es el timestamp exacto de Meta: es cuándo este servidor
    persistió el mensaje. Se usa como aproximación preventiva acordada para
    esta etapa. `Conversacion.ultimo_mensaje_en` no sirve porque también se
    actualiza con mensajes salientes.
    """
    return (
        db.query(Mensaje)
        .filter(
            Mensaje.conversacion_id == conversacion_id,
            Mensaje.rol == RolMensaje.USUARIO,
        )
        .order_by(Mensaje.creado_en.desc(), Mensaje.id.desc())
        .first()
    )


def _ventana_de_texto_libre_abierta(db: Session, conversacion_id: int) -> bool:
    ultimo = _ultimo_mensaje_del_usuario(db, conversacion_id)
    if ultimo is None:
        return False

    creado_en = ultimo.creado_en
    # SQLite pierde el tzinfo al hacer round-trip aun con timezone=True. Todo
    # lo que guarda la app en esta columna es UTC.
    if creado_en.tzinfo is None:
        creado_en = creado_en.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - creado_en < VENTANA_TEXTO_LIBRE


def _enviar_por_meta_y_guardar(
    db: Session,
    conversacion: Conversacion,
    texto: str,
    *,
    rol: RolMensaje,
    autor_crm_id: int | None = None,
    revalidar_antes_de_meta=None,
) -> EnvioProcesado:
    """Core privado compartido: presupuesto, Meta, persistencia y costo.

    Los caminos públicos deciden quién puede llegar hasta acá y con qué rol.
    `revalidar_antes_de_meta`, cuando existe, corre después de la reserva
    porque `reservar_gasto` hace commit. Si falla, la reserva se libera y no
    se llama a Meta.
    """
    identificador_externo = conversacion.identificador_externo

    mes_reservado = None
    if config.meta_tope_duro_habilitado:
        mes_reservado = mes_actual(datetime.now(timezone.utc))
        if not reservar_gasto(db, mes_reservado):
            logger.warning(
                "Envío a %s bloqueado por el tope mensual de presupuesto (mes=%s): no se llama a Meta",
                enmascarar_identificador(identificador_externo), mes_reservado,
            )
            return EnvioProcesado(ResultadoEnvio.BLOQUEADO_PRESUPUESTO)

    if revalidar_antes_de_meta is not None:
        try:
            revalidar_antes_de_meta()
        except Exception:
            if mes_reservado is not None:
                liberar_reserva(db, mes_reservado)
            else:
                # Puede haberse tomado un FOR UPDATE antes de detectar que la
                # atención ya no es válida. Liberarlo antes de responder.
                db.rollback()
            raise

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
        elif revalidar_antes_de_meta is not None:
            # En el camino humano libera el FOR UPDATE. En el del bot no hay
            # bloqueo de atención que liberar ni se cambia la semántica de
            # rollback previa a este refactor.
            db.rollback()
        return EnvioProcesado(ResultadoEnvio.FALLO_META)
    logger.info(
        "TIEMPOS %s | envío a Meta: %.0f ms",
        enmascarar_identificador(identificador_externo), cronometro_envio.ms(),
    )

    wa_message_id = extraer_wa_message_id(respuesta_meta)
    if not wa_message_id:
        logger.warning(
            "Meta aceptó el envío a %s pero la respuesta no trajo messages[0].id: "
            "queda sin wa_message_id y sin contabilizar en el costo estimado",
            enmascarar_identificador(identificador_externo),
        )

    mensaje = Mensaje(
        conversacion_id=conversacion.id,
        rol=rol,
        contenido=texto,
        wa_message_id=wa_message_id,
        autor_crm_id=autor_crm_id,
    )
    db.add(mensaje)
    conversacion.ultimo_mensaje_en = datetime.now(timezone.utc)

    if wa_message_id:
        db.flush()
        contabilizar_envio(db, conversacion, mensaje)

    db.commit()

    logger.info("Mensaje enviado a %s: %s", enmascarar_identificador(identificador_externo), texto)
    return EnvioProcesado(ResultadoEnvio.EXITOSO, mensaje)


def enviar_y_guardar(
    db: Session,
    conversacion: Conversacion,
    texto: str,
) -> ResultadoEnvio:
    """Camino BOT normal: nunca envía si la conversación está pausada."""
    identificador_externo = conversacion.identificador_externo

    if esta_en_modo_humano(db, conversacion):
        logger.info(
            "La conversación con %s pasó a modo humano mientras se generaba la "
            "respuesta: no se envía nada para no escribir encima de la persona",
            enmascarar_identificador(identificador_externo),
        )
        return ResultadoEnvio.MODO_HUMANO

    return _enviar_por_meta_y_guardar(
        db, conversacion, texto, rol=RolMensaje.BOT,
    ).resultado


def enviar_aviso_escalamiento(
    db: Session,
    conversacion: Conversacion,
    texto: str,
) -> ResultadoEnvio:
    """Camino explícito para el aviso que sale después de prender la pausa.

    Es la única excepción BOT al chequeo de pausa y conserva el comportamiento
    anterior sin exponer un booleano genérico a otros callers.
    """
    return _enviar_por_meta_y_guardar(
        db, conversacion, texto, rol=RolMensaje.BOT,
    ).resultado


def enviar_respuesta_humana(
    db: Session,
    *,
    conversacion_id: int,
    texto: str,
    autor_crm_id: int,
) -> EnvioProcesado:
    """Envía una respuesta humana solo si su autor conserva la atención.

    La primera validación evita trabajo innecesario. La segunda toma el
    bloqueo autoritativo después de la posible reserva y justo antes de Meta.
    No toca la pausa ni modifica la atención.
    """
    atencion.validar_para_responder(db, conversacion_id, autor_crm_id)

    if not _ventana_de_texto_libre_abierta(db, conversacion_id):
        return EnvioProcesado(ResultadoEnvio.FUERA_DE_VENTANA)

    conversacion = (
        db.query(Conversacion)
        .filter(Conversacion.id == conversacion_id)
        .one()
    )

    return _enviar_por_meta_y_guardar(
        db,
        conversacion,
        texto,
        rol=RolMensaje.HUMANO,
        autor_crm_id=autor_crm_id,
        revalidar_antes_de_meta=lambda: atencion.bloquear_para_responder(
            db, conversacion_id, autor_crm_id
        ),
    )
