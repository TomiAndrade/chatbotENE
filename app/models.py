"""Modelos SQLAlchemy: conversaciones y mensajes."""

import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db import Base

CANAL_WHATSAPP = "whatsapp"


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


class RolMensaje(str, enum.Enum):
    USUARIO = "usuario"
    BOT = "bot"
    HUMANO = "humano"


class MotivoPausa(str, enum.Enum):
    """Por qué está prendido modo_humano. La distinción existe porque solo
    una de las dos expira sola (ver `app.main._pausa_vigente`):

    - ESCALAMIENTO: el modelo decidió que hacía falta una persona. No expira,
      se desmarca a mano (`scripts/resetear_modo_humano.py`).
    - INTERVENCION_MANUAL: la secretaría respondió desde la app de WhatsApp
      Business. Expira sola a los PAUSA_HUMANA_MINUTOS de la última vez que
      respondió (ver `modo_humano_desde` en `Conversacion`).
    """

    ESCALAMIENTO = "escalamiento"
    INTERVENCION_MANUAL = "intervencion_manual"


class Conversacion(Base):
    __tablename__ = "conversaciones"
    __table_args__ = (
        UniqueConstraint("canal", "identificador_externo", name="uq_conversaciones_canal_identificador"),
    )

    id = Column(Integer, primary_key=True)
    canal = Column(String, nullable=False, default=CANAL_WHATSAPP)
    identificador_externo = Column(String, nullable=False, index=True)
    modo_humano = Column(Boolean, default=False, nullable=False)
    # Por qué está prendida la pausa. Es lo que decide si expira sola — ver
    # MotivoPausa. None mientras no hay pausa.
    motivo_pausa = Column(
        Enum(MotivoPausa, values_callable=lambda enum_cls: [e.value for e in enum_cls], name="motivo_pausa"),
        nullable=True,
    )
    # Cuándo se prendió modo_humano por última vez. Es solo un timestamp, sin
    # semántica propia — quién decide si la pausa expira es `motivo_pausa`,
    # no si este campo tiene valor o no. (Antes del cambio a motivo_pausa,
    # este campo hacía las dos cosas a la vez — None significaba tanto "sin
    # pausa" como "escalamiento" — y esa ambigüedad causó un bug real: un
    # escalamiento del modelo podía heredar una fecha vieja de una pausa
    # manual ya vencida. Ver spec-pausa-por-intervencion-humana.md.)
    modo_humano_desde = Column(DateTime(timezone=True), nullable=True)
    creada_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False)
    ultimo_mensaje_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False)
    resumen_escalamiento = Column(Text, nullable=True)
    escalada_en = Column(DateTime(timezone=True), nullable=True)

    mensajes = relationship("Mensaje", back_populates="conversacion")


class Mensaje(Base):
    __tablename__ = "mensajes"

    id = Column(Integer, primary_key=True)
    conversacion_id = Column(Integer, ForeignKey("conversaciones.id"), nullable=False)
    rol = Column(
        Enum(RolMensaje, values_callable=lambda enum_cls: [e.value for e in enum_cls], name="rol_mensaje"),
        nullable=False,
    )
    contenido = Column(Text, nullable=False)
    wa_message_id = Column(String, unique=True, nullable=True, index=True)
    creado_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False)

    conversacion = relationship("Conversacion", back_populates="mensajes")
