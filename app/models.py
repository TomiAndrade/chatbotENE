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


class Conversacion(Base):
    __tablename__ = "conversaciones"
    __table_args__ = (
        UniqueConstraint("canal", "identificador_externo", name="uq_conversaciones_canal_identificador"),
    )

    id = Column(Integer, primary_key=True)
    canal = Column(String, nullable=False, default=CANAL_WHATSAPP)
    identificador_externo = Column(String, nullable=False, index=True)
    modo_humano = Column(Boolean, default=False, nullable=False)
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
