"""Límite de uso por número: protección básica contra abuso y contra costos
inesperados (ver spec-etapa2.md, "Límite de uso por número").
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Conversacion, Mensaje, RolMensaje


def mensajes_ultima_hora(db: Session, canal: str, identificador_externo: str) -> int:
    """Cuenta los mensajes de rol `usuario` de esa conversación en la última
    hora, incluyendo el que se acaba de guardar. Es una ventana deslizante: a
    medida que pasa el tiempo los mensajes viejos dejan de contar solos.

    Se filtra por (canal, identificador_externo), no solo por el
    identificador: nada garantiza que un id de sesión de otro canal no
    coincida con un número de teléfono."""
    hace_una_hora = datetime.now(timezone.utc) - timedelta(hours=1)
    return (
        db.query(Mensaje)
        .join(Conversacion)
        .filter(
            Conversacion.canal == canal,
            Conversacion.identificador_externo == identificador_externo,
            Mensaje.rol == RolMensaje.USUARIO,
            Mensaje.creado_en >= hace_una_hora,
        )
        .count()
    )
