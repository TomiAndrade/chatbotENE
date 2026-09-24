"""Helpers sobre la marca de agrupamiento (`Conversacion.
ultimo_mensaje_agrupado_id`, ver specs/spec-agrupamiento-mensajes.md).

Vive en su propio módulo, y no en `app/main.py` donde nació, porque ahora lo
necesitan dos lados que no se pueden importar entre sí: el flujo del bot
(`app/main.py`) y el cierre de una atención humana (`app/atencion.py`,
que no puede importar `app/main.py` — mismo motivo que documenta
`app/pausa.py`).
"""

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import TIPO_TEXTO, Conversacion, Mensaje, RolMensaje


def avanzar_marca(db: Session, conversacion: Conversacion, mensaje_id: int) -> None:
    """Mueve `ultimo_mensaje_agrupado_id` a `mensaje_id`, pero nunca hacia
    atrás — mismo patrón de UPDATE condicional que `_reclamar_generacion`
    (app/main.py), por la misma razón: hay más de un llamador que puede
    escribir esta columna sin coordinarse entre sí (el dueño del lote, al
    terminar de responder; `procesar_mensaje_entrante`, cuando un mensaje de
    texto no llega a entrar a ningún lote porque superó el límite por hora;
    y `app.atencion.resolver`, al cerrar una atención humana). Sin un UPDATE
    atómico que solo avance el valor, el que comitea último "gana" sin
    importar cuál de los mensajes es más nuevo, y uno que ya se marcó como
    fuera de lote podría volver a quedar pendiente.

    No comitea: lo hace quien llama, en la misma transacción que el resto
    del cambio (el commit del lote respondido, el cierre de la atención)."""
    db.query(Conversacion).filter(
        Conversacion.id == conversacion.id,
        or_(
            Conversacion.ultimo_mensaje_agrupado_id.is_(None),
            Conversacion.ultimo_mensaje_agrupado_id < mensaje_id,
        ),
    ).update({"ultimo_mensaje_agrupado_id": mensaje_id}, synchronize_session=False)


def ultimo_id_de_texto_entrante(db: Session, conversacion_id: int) -> int | None:
    """El id del mensaje de texto del usuario más nuevo de la conversación,
    esté o no todavía pendiente de responder. `None` si nunca escribió
    ningún mensaje de texto (conversación recién creada, o solo con
    adjuntos).

    Solo mira `TIPO_TEXTO`: es el mismo filtro que usa `agrupar_y_responder`
    (app/main.py) para decidir qué hay pendiente — basarse en otro criterio
    dejaría "pendiente" algo que el bot nunca iba a recoger de entrada (un
    adjunto), o al revés."""
    return (
        db.query(func.max(Mensaje.id))
        .filter(
            Mensaje.conversacion_id == conversacion_id,
            Mensaje.rol == RolMensaje.USUARIO,
            Mensaje.tipo == TIPO_TEXTO,
        )
        .scalar()
    )


def avanzar_hasta_el_ultimo_entrante(db: Session, conversacion: Conversacion) -> None:
    """Marca como ya agrupado todo mensaje de texto del usuario que exista
    en este momento, sin comitear.

    La llama quien cierra una atención humana (`app.atencion.resolver`,
    `app.atencion.resolver_si_hay_abierta`) en la misma transacción que
    reactiva el bot. Regla de producto: mientras hubo una atención abierta,
    cualquier mensaje del usuario le pertenece a esa atención, no al bot.
    Sin este avance, `agrupar_y_responder` (app/main.py) los recogería en el
    próximo lote y el bot terminaría respondiendo algo que la secretaría ya
    contestó — o, peor, si el proceso se reinicia después de resolver y
    antes de que llegue un mensaje nuevo, `_recuperar_lotes_pendientes` los
    dispararía sin que haya entrado nada (`_conversaciones_con_texto_
    pendiente` ya no los va a encontrar, justamente porque quedan marcados).

    No hace nada si todavía no hay ningún mensaje de texto."""
    ultimo_id = ultimo_id_de_texto_entrante(db, conversacion.id)
    if ultimo_id is not None:
        avanzar_marca(db, conversacion, ultimo_id)
