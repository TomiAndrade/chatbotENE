"""Armado del historial de conversación (ver spec-etapa2.md, "Armado del
historial"). Trae los últimos N mensajes en orden cronológico y corta todo si
pasó demasiado tiempo desde el último.
"""

from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import config
from app.models import Conversacion, Mensaje, RolMensaje

PREFIJO_HUMANO = "[Respuesta de una persona del equipo] "


def construir_historial(db: Session, conversacion: Conversacion, mensaje_actual: Mensaje) -> list[Mensaje]:
    """Devuelve los mensajes previos a `mensaje_actual`, más recientes primero
    en la consulta y ya invertidos a orden cronológico al devolverlos.

    Si pasaron más de HISTORIAL_DIAS_VALIDEZ días entre el último mensaje
    anterior y el actual, se ignora todo el historial: el asistente arranca
    de cero y vuelve a presentarse.
    """
    mensajes_recientes = (
        db.query(Mensaje)
        .filter(
            Mensaje.conversacion_id == conversacion.id,
            Mensaje.id != mensaje_actual.id,
        )
        .order_by(Mensaje.creado_en.desc())
        .limit(config.historial_max_mensajes)
        .all()
    )

    if not mensajes_recientes:
        return []

    historial = list(reversed(mensajes_recientes))
    ultimo_anterior = historial[-1]

    limite_antiguedad = timedelta(days=config.historial_dias_validez)
    if mensaje_actual.creado_en - ultimo_anterior.creado_en > limite_antiguedad:
        return []

    return historial


def mapear_mensaje(mensaje: Mensaje) -> tuple[str, str]:
    """Traduce un Mensaje al par (rol_lógico, texto) que consume cada
    proveedor. Rol lógico es "usuario" o "asistente" — el mapeo a los roles
    propios de cada API vive dentro de cada proveedor.

    Los mensajes de rol `humano` viajan como asistente, pero con el texto
    prefijado: son contexto válido, no ejemplos del estilo del bot.
    """
    if mensaje.rol == RolMensaje.USUARIO:
        return "usuario", mensaje.contenido
    if mensaje.rol == RolMensaje.HUMANO:
        return "asistente", PREFIJO_HUMANO + mensaje.contenido
    return "asistente", mensaje.contenido
