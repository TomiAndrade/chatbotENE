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
    una de las dos expira sola (ver `app.pausa.pausa_vigente`):

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

    # Agrupamiento de mensajes consecutivos (ver specs/spec-agrupamiento-mensajes.md).
    # `generando_desde` es NULL cuando nadie está generando una respuesta
    # para esta conversación; un valor no nulo es la reserva tomada por
    # `app.main._reclamar_generacion`. `generando_token` identifica a quién
    # pertenece esa reserva — separado del timestamp porque comparar
    # timestamps después de un viaje a la base no es seguro (mismo problema
    # de precisión/tzinfo que documenta app/pausa.py) y porque hace falta
    # poder distinguir "mi reserva" de "una reserva nueva que retomó la mía
    # por abandono" al momento de liberarla.
    generando_desde = Column(DateTime(timezone=True), nullable=True)
    generando_token = Column(String, nullable=True)
    # El id del último Mensaje (rol usuario, tipo texto) ya incluido en un
    # lote procesado. NULL: todavía no se procesó ningún lote. Se actualiza
    # recién después de que responder() vuelve, nunca antes — así un
    # reinicio a mitad de la llamada al modelo no pierde el lote, lo
    # reprocesa (ver "Reinicios y recuperación" en el spec).
    ultimo_mensaje_agrupado_id = Column(Integer, nullable=True)

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
    # El `messages[].type` real del webhook de Meta (ver
    # specs/spec-adjuntos-no-soportados.md), solo para rol=usuario. Decide si
    # un mensaje entra al agrupamiento (specs/spec-agrupamiento-mensajes.md)
    # sin mirar `contenido` — la misma razón por la que 1.1 no decide por
    # coincidencia textual con el marcador de adjunto. NULL para mensajes que
    # no vienen del webhook con esa semántica (bot, humano).
    tipo = Column(String, nullable=True)

    conversacion = relationship("Conversacion", back_populates="mensajes")


class ResultadoLlamadaIA(str, enum.Enum):
    """Cómo terminó una llamada a generar_respuesta() (ver app/respuesta.py y
    responder() en app/main.py). Distingue los mismos cuatro caminos que ya
    distingue responder(): éxito, error transitorio del proveedor (no
    escala en desarrollo, ver ErrorTransitorioProveedor), cualquier otro
    error, y respuesta vacía sin escalar."""

    OK = "ok"
    ERROR_TRANSITORIO = "error_transitorio"
    ERROR = "error"
    VACIO = "vacio"


class LlamadaIA(Base):
    """Una fila por cada llamada a generar_respuesta() desde responder(),
    éxito o fracaso (ver specs/spec-dashboard-metricas.md). Es la fuente del
    dashboard de costos y actividad del CRM: nunca guarda contenido de
    mensajes ni el identificador de la conversación fuera del id numérico.

    tokens_entrada/tokens_salida quedan NULL cuando el proveedor no informó
    uso (pasa siempre con "fijo", y puede pasar con un openai_compat que no
    devuelva "usage"). Para Claude, tokens_entrada incluye los tokens de
    caché (creación y lectura) sumados al input fresco — no se separan en
    columnas propias, ver el spec sobre esa simplificación.
    """

    __tablename__ = "llamadas_ia"

    id = Column(Integer, primary_key=True)
    conversacion_id = Column(Integer, ForeignKey("conversaciones.id"), nullable=False, index=True)
    proveedor = Column(String, nullable=False, index=True)
    modelo = Column(String, nullable=True, index=True)
    creado_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False, index=True)
    duracion_ms = Column(Integer, nullable=True)
    resultado = Column(
        Enum(
            ResultadoLlamadaIA,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            name="resultado_llamada_ia",
        ),
        nullable=False,
    )
    escalo = Column(Boolean, default=False, nullable=False)
    tokens_entrada = Column(Integer, nullable=True)
    tokens_salida = Column(Integer, nullable=True)
