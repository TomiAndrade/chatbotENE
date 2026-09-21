"""Armado del historial de conversación (ver spec-etapa2.md, "Armado del
historial"). Trae los últimos N mensajes en orden cronológico y corta todo si
pasó demasiado tiempo desde el último.
"""

from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import config
from app.models import Conversacion, Mensaje, RolMensaje

MARCADOR_HUMANO = "[una persona del equipo respondió]"


def construir_historial(
    db: Session, conversacion: Conversacion, mensaje_actual: Mensaje | list[Mensaje]
) -> list[Mensaje]:
    """Devuelve los mensajes previos a `mensaje_actual`, más recientes primero
    en la consulta y ya invertidos a orden cronológico al devolverlos.

    `mensaje_actual` acepta un `Mensaje` suelto (uso de siempre, sin tocar
    ningún llamador existente) o una lista — el lote agrupado de
    specs/spec-agrupamiento-mensajes.md. Se excluyen **todos** los ids del
    lote de la consulta, así ninguno entra dos veces al contexto del modelo
    (ni acá ni en el texto armado a mano con el contenido del lote). Para el
    chequeo de antigüedad se usa el mensaje más viejo del lote: es el que
    marca cuándo arrancó la ráfaga actual respecto del historial previo.

    Si pasaron más de HISTORIAL_DIAS_VALIDEZ días entre el último mensaje
    anterior y el primero del lote, se ignora todo el historial: el
    asistente arranca de cero y vuelve a presentarse.
    """
    mensajes_actuales = mensaje_actual if isinstance(mensaje_actual, list) else [mensaje_actual]
    ids_actuales = [m.id for m in mensajes_actuales]
    primero_actual = min(mensajes_actuales, key=lambda m: (m.creado_en, m.id))

    mensajes_recientes = (
        db.query(Mensaje)
        .filter(
            Mensaje.conversacion_id == conversacion.id,
            ~Mensaje.id.in_(ids_actuales),
        )
        # El id desempata: dos mensajes guardados en el mismo instante (el
        # texto del modelo y el aviso de escalamiento salen uno detrás del
        # otro) tienen que quedar en el orden en que se crearon, y no en uno
        # arbitrario que además puede hacer que el limit descarte el que no
        # corresponde.
        .order_by(Mensaje.creado_en.desc(), Mensaje.id.desc())
        .limit(config.historial_max_mensajes)
        .all()
    )

    if not mensajes_recientes:
        return []

    historial = list(reversed(mensajes_recientes))
    ultimo_anterior = historial[-1]

    limite_antiguedad = timedelta(days=config.historial_dias_validez)
    if primero_actual.creado_en - ultimo_anterior.creado_en > limite_antiguedad:
        return []

    return historial


def mapear_mensaje(mensaje: Mensaje) -> tuple[str, str]:
    """Traduce un Mensaje al par (rol_lógico, texto) que consume cada
    proveedor. Rol lógico es "usuario" o "asistente" — el mapeo a los roles
    propios de cada API vive dentro de cada proveedor.

    Los mensajes de rol `humano` viajan como asistente, pero con un marcador
    fijo en vez del texto real (MARCADOR_HUMANO): el modelo tiene que saber
    que hubo una intervención humana, no leer qué se dijo. La secretaría
    puede responder cosas que el bot nunca debe repetir — datos bancarios,
    por ejemplo, que el circuito documentado en knowledge-base.md pone en
    manos del equipo humano por WhatsApp — y antes de este cambio ese texto
    entraba íntegro al contexto del modelo. El contenido real se sigue
    guardando en la base tal cual (lo necesitan el inbox y el diagnóstico);
    lo único que cambia es lo que ve el modelo.
    """
    if mensaje.rol == RolMensaje.USUARIO:
        return "usuario", mensaje.contenido
    if mensaje.rol == RolMensaje.HUMANO:
        return "asistente", MARCADOR_HUMANO
    return "asistente", mensaje.contenido
