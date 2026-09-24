"""Modelos SQLAlchemy: conversaciones y mensajes."""

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from app.db import Base

CANAL_WHATSAPP = "whatsapp"

# El único `messages[].type` de la Cloud API de Meta que hoy se trata como
# soportado (ver specs/spec-adjuntos-no-soportados.md) y el único que entra
# al agrupamiento (ver app/agrupamiento.py). Vive acá, junto a CANAL_WHATSAPP,
# porque los dos son valores de columna que necesita más de un módulo que no
# se puede importar entre sí (app/main.py y app/agrupamiento.py).
TIPO_TEXTO = "text"


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


class RolMensaje(str, enum.Enum):
    USUARIO = "usuario"
    BOT = "bot"
    HUMANO = "humano"


class MotivoPausa(str, enum.Enum):
    """Por qué está prendido modo_humano. La distinción existe porque solo
    INTERVENCION_MANUAL expira sola (ver `app.pausa.pausa_vigente`):

    - ESCALAMIENTO: el modelo decidió que hacía falta una persona. No expira,
      se desmarca a mano (`scripts/resetear_modo_humano.py`) o resolviendo
      la atención desde el CRM.
    - INTERVENCION_MANUAL: la secretaría respondió desde la app de WhatsApp
      Business. Expira sola a los PAUSA_HUMANA_MINUTOS de la última vez que
      respondió (ver `modo_humano_desde` en `Conversacion`).
    - ATENCION_CRM: alguien del equipo inició una atención desde el CRM (ver
      `Atencion` y app/atencion.py). No expira: dura lo que dure la
      atención y se levanta al resolverla.

    Es un tipo nativo de Postgres: agregar un valor acá exige un
    `ALTER TYPE motivo_pausa ADD VALUE` a mano en una base que ya existe
    (ver scripts/migracion_atenciones.sql).
    """

    ESCALAMIENTO = "escalamiento"
    INTERVENCION_MANUAL = "intervencion_manual"
    ATENCION_CRM = "atencion_crm"


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
    # Cuenta del CRM que escribió el mensaje, únicamente para respuestas
    # humanas enviadas desde el panel. Es nullable porque BOT/USUARIO no
    # tienen autor CRM y porque existen mensajes humanos históricos o
    # provenientes del flujo dormido de WhatsApp Business sin una cuenta
    # identificable. Las cuentas se desactivan en vez de borrarse, así que
    # la autoría histórica permanece válida.
    autor_crm_id = Column(
        Integer,
        ForeignKey("crm_usuarios.id", name="fk_mensajes_autor_crm_id"),
        nullable=True,
    )
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


class EnvioWhatsapp(Base):
    """Control preventivo de gasto de WhatsApp/Meta, etapa 1 (ver
    specs/spec-costo-whatsapp-meta.md). Una fila por cada envío saliente que
    la Cloud API de Meta aceptó — nunca por uno que falló ni por uno que el
    bot solo intentó armar. Que la fila exista ya significa "Meta aceptó el
    POST": no hace falta una columna de estado para eso (ver `contabilizar_envio`
    en app/costo_meta.py).

    `mensaje_id` es el `Mensaje` saliente (bot o humano) que el camino
    centralizado de `app.envio` acaba de crear con este mismo
    `wa_message_id` — mismo par que va a hacer falta para
    correlacionar `sent -> delivered -> read` en una etapa futura, cuando se
    procesen los `statuses[]` del webhook. `wa_message_id` es único acá (además
    de en `mensajes.wa_message_id`) para no contabilizar dos veces la misma
    respuesta de Meta si algún reintento la procesara más de una vez.

    `categoria`, `tarifa_ars` y `costo_estimado_ars` quedan grabados en la
    fila (no recalculados después a partir de la config vigente): si mañana
    cambia META_TARIFA_SERVICE_ARS, el histórico tiene que seguir mostrando la
    tarifa que estaba vigente cuando se mandó cada mensaje, no la de hoy.
    Es una ESTIMACIÓN preventiva, no facturación exacta — el sistema todavía
    no concilia contra la factura real de Meta ni persiste delivered/read/failed.
    """

    __tablename__ = "envios_whatsapp"

    id = Column(Integer, primary_key=True)
    conversacion_id = Column(Integer, ForeignKey("conversaciones.id"), nullable=False, index=True)
    mensaje_id = Column(Integer, ForeignKey("mensajes.id"), nullable=False, unique=True)
    # Sin index=True: `unique=True` sola ya alcanza para que Postgres arme el
    # índice que hace cumplir la constraint, mismo criterio que mensaje_id
    # arriba. No hace falta un segundo índice para las búsquedas por esta
    # columna — el que crea la UNIQUE ya sirve para eso.
    wa_message_id = Column(String, unique=True, nullable=False)
    categoria = Column(String, nullable=False)
    tarifa_ars = Column(Numeric(12, 4), nullable=False)
    costo_estimado_ars = Column(Numeric(12, 4), nullable=False)
    creado_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False, index=True)


class PresupuestoMetaMensual(Base):
    """Control preventivo de gasto de WhatsApp/Meta, etapa 2.1 — bloqueo
    duro mensual (ver specs/spec-tope-duro-meta.md). Una fila por mes
    calendario (`mes`, formato `YYYY-MM` en TIMEZONE, ver
    `app.costo_meta.mes_actual`), creada perezosamente al primer intento de
    envío de ese mes (`app.costo_meta.asegurar_fila_mensual`).

    `presupuesto_ars` queda congelado con el valor de
    `META_PRESUPUESTO_MENSUAL_ARS` vigente al crear la fila — mismo criterio
    que `EnvioWhatsapp.tarifa_ars`: un cambio de config a mitad de mes no
    reescribe retroactivamente el tope de un mes ya empezado.

    `costo_comprometido_ars` es la fuente de verdad para bloquear, no un
    espejo de `SUM(EnvioWhatsapp.costo_estimado_ars)`. Se incrementa con una
    única sentencia `UPDATE` atómica (`app.costo_meta.reservar_gasto`) ANTES
    de llamar a Meta — check y reserva son la misma operación, para que dos
    envíos concurrentes no puedan superar juntos el presupuesto. Si Meta
    rechaza o falla el envío, la reserva se libera
    (`app.costo_meta.liberar_reserva`). **Si Meta acepta el envío pero
    después falla el guardado de `Mensaje`/`EnvioWhatsapp`, la reserva NO se
    libera** — es intencional y conservador: el gasto ya ocurrió del lado de
    Meta aunque no quede el detalle local, y liberarla dejaría que el tope
    real se corra hacia arriba con cada falla de ese tipo. Consecuencia:
    `costo_comprometido_ars` puede quedar por encima de
    `SUM(EnvioWhatsapp.costo_estimado_ars)` del mismo mes — nunca por
    debajo. Nunca se resetea ni se borra una fila: el histórico de meses
    pasados queda intacto.
    """

    __tablename__ = "presupuesto_meta_mensual"

    id = Column(Integer, primary_key=True)
    mes = Column(String, unique=True, nullable=False)
    presupuesto_ars = Column(Numeric(12, 4), nullable=False)
    costo_comprometido_ars = Column(Numeric(12, 4), nullable=False, default=Decimal("0"))
    creado_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False)


class EstadoAtencion(str, enum.Enum):
    """En qué punto está una atención humana (ver `Atencion`)."""

    PENDIENTE = "pendiente"
    EN_ATENCION = "en_atencion"
    RESUELTA = "resuelta"


class MotivoAtencion(str, enum.Enum):
    """Por qué hace falta una persona. Arranca con dos valores a propósito:
    el catálogo completo (Evento, Reserva, etc.) todavía no está definido."""

    ESCALAMIENTO = "escalamiento"
    SIN_CLASIFICAR = "sin_clasificar"


class Atencion(Base):
    """Un episodio de atención humana sobre una conversación: nace cuando el
    bot deriva o cuando alguien del equipo la inicia desde el CRM, y termina
    al resolverla. Ver app/atencion.py, que es el único que la escribe.

    Vive aparte de `Conversacion` por dos motivos:

    - `reactivar_bot` limpia `resumen_escalamiento` y `escalada_en` de la
      conversación. Acá el motivo y el resumen quedan guardados después de
      resolver, uno por episodio.
    - Es una tabla nueva: `create_all` la crea sola en una base existente,
      sin `ALTER TABLE` sobre `conversaciones`.

    `estado` y `motivo` son String y no Enum a propósito: un Enum de
    SQLAlchemy es un tipo nativo de Postgres, y cada valor nuevo exigiría un
    `ALTER TYPE` a mano (ver PENDIENTES.md). El catálogo de motivos va a
    crecer, así que el control de valores válidos queda en Python
    (`EstadoAtencion`, `MotivoAtencion`).

    **Una sola atención abierta por conversación**, garantizado por la base
    con un índice único parcial (solo cubre las filas no resueltas). Dos
    personas que inician a la vez sobre la misma conversación no pueden
    crear dos: la segunda choca con el índice.

    `iniciada_por_id` en NULL quiere decir que la abrió el bot al derivar.
    """

    __tablename__ = "atenciones"
    __table_args__ = (
        Index(
            "uq_atenciones_una_abierta_por_conversacion",
            "conversacion_id",
            unique=True,
            postgresql_where=text("estado <> 'resuelta'"),
            sqlite_where=text("estado <> 'resuelta'"),
        ),
    )

    id = Column(Integer, primary_key=True)
    conversacion_id = Column(Integer, ForeignKey("conversaciones.id"), nullable=False, index=True)
    estado = Column(String, nullable=False, default=EstadoAtencion.PENDIENTE.value)
    motivo = Column(String, nullable=False, default=MotivoAtencion.SIN_CLASIFICAR.value)
    resumen = Column(Text, nullable=True)
    # Las tres columnas de personas apuntan a `crm_usuarios` (app/crm/modelos.py).
    # Las cuentas se desactivan en vez de borrarse, así que el rastro no se rompe.
    iniciada_por_id = Column(Integer, ForeignKey("crm_usuarios.id"), nullable=True)
    responsable_id = Column(Integer, ForeignKey("crm_usuarios.id"), nullable=True)
    resuelta_por_id = Column(Integer, ForeignKey("crm_usuarios.id"), nullable=True)
    creada_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False)
    tomada_en = Column(DateTime(timezone=True), nullable=True)
    resuelta_en = Column(DateTime(timezone=True), nullable=True)

    # Sin relationship() hacia UsuarioCrm, a propósito: resolverla exige que
    # app/crm/modelos.py esté importado, y hay scripts que usan app.models
    # sin el CRM (scripts/resetear_modo_humano.py). Los nombres los busca
    # app/crm/servicio.py a partir de los ids.
    conversacion = relationship("Conversacion")
