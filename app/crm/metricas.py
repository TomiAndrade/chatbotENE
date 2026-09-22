"""Agregados del dashboard de costos y actividad del CRM
(specs/spec-dashboard-metricas.md).

Todo con el ORM, como el resto del CRM (ver app/crm/servicio.py): nada de SQL
crudo. Nunca expone teléfonos ni contenido de mensajes — solo conteos,
duraciones y tokens agregados por proveedor+modelo.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import config
from app.costo_meta import consumo_mensual
from app.crm.servicio import contar_pausadas
from app.models import Conversacion, LlamadaIA, Mensaje, ResultadoLlamadaIA, RolMensaje

# Sin parámetros, el rango por defecto: los últimos 7 días terminando hoy.
DIAS_POR_DEFECTO = 7

_RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Precio por millón de tokens: es la unidad en la que vienen las tarifas de
# TARIFAS_IA_JSON (ver app/config.py). Constante acá y no repetida en cada
# cuenta, para que un cambio de unidad no obligue a tocar dos lugares.
TOKENS_POR_UNIDAD_DE_TARIFA = 1_000_000


class RangoInvalido(ValueError):
    """`desde`/`hasta` no son fechas YYYY-MM-DD válidas, o `desde` es
    posterior a `hasta`. La ruta lo traduce a un 400."""


def _parsear_fecha(valor: str) -> date:
    if not _RE_FECHA.match(valor):
        raise RangoInvalido(f"Fecha inválida, se espera YYYY-MM-DD: {valor!r}")
    try:
        return date.fromisoformat(valor)
    except ValueError as error:
        raise RangoInvalido(f"Fecha inválida, se espera YYYY-MM-DD: {valor!r}") from error


def rango_utc(desde: str | None, hasta: str | None) -> tuple[datetime, datetime]:
    """Convierte un rango de días calendario en TIMEZONE a límites UTC
    `[inicio, fin)` para consultar la base, que guarda todo en UTC (ver
    app/models.py).

    `hasta` es el último día incluido entero: el límite superior real es el
    inicio del día siguiente, y las consultas comparan con `<` contra ese
    límite — nunca con `<=` contra "23:59:59", que dejaría afuera el último
    tramo del día por debajo del segundo.

    Sin `desde`/`hasta`, los últimos DIAS_POR_DEFECTO días terminando hoy, en
    TIMEZONE (la misma zona que ya usa el panel para mostrar horas, ver
    `/crm/api/sesion`).
    """
    hoy_local = datetime.now(config.timezone).date()
    fecha_hasta = _parsear_fecha(hasta) if hasta else hoy_local
    fecha_desde = _parsear_fecha(desde) if desde else fecha_hasta - timedelta(days=DIAS_POR_DEFECTO - 1)

    if fecha_desde > fecha_hasta:
        raise RangoInvalido("La fecha 'desde' no puede ser posterior a 'hasta'")

    inicio_local = datetime.combine(fecha_desde, time.min, tzinfo=config.timezone)
    fin_local = datetime.combine(fecha_hasta + timedelta(days=1), time.min, tzinfo=config.timezone)
    return inicio_local.astimezone(timezone.utc), fin_local.astimezone(timezone.utc)


def _div(numerador: float, denominador: float) -> float | None:
    """None (N/D) en vez de ZeroDivisionError o un 0 que sugeriría que hubo
    actividad y dio cero."""
    if not denominador:
        return None
    return numerador / denominador


def _conteo_mensajes_por_rol(db: Session, desde: datetime, hasta: datetime) -> dict[str, int]:
    filas = (
        db.query(Mensaje.rol, func.count(Mensaje.id))
        .filter(Mensaje.creado_en >= desde, Mensaje.creado_en < hasta)
        .group_by(Mensaje.rol)
        .all()
    )
    conteos = {rol.value: 0 for rol in RolMensaje}
    for rol, cantidad in filas:
        conteos[rol.value] = cantidad
    return conteos


def _conversaciones_con_actividad(db: Session, desde: datetime, hasta: datetime) -> int:
    """Conversaciones con al menos un mensaje (de cualquier rol) en el
    rango — la definición de "conversaciones totales" del dashboard."""
    return (
        db.query(func.count(func.distinct(Mensaje.conversacion_id)))
        .filter(Mensaje.creado_en >= desde, Mensaje.creado_en < hasta)
        .scalar()
        or 0
    )


def _conversaciones_nuevas(db: Session, desde: datetime, hasta: datetime) -> int:
    return (
        db.query(func.count(Conversacion.id))
        .filter(Conversacion.creada_en >= desde, Conversacion.creada_en < hasta)
        .scalar()
        or 0
    )


def _tarifa(proveedor: str, modelo: str | None) -> dict | None:
    return config.tarifas_ia.get(f"{proveedor}:{modelo or ''}")


def _costo_usd(tokens_entrada: int, tokens_salida: int, tarifa: dict) -> float | None:
    precio_entrada = tarifa.get("entrada")
    precio_salida = tarifa.get("salida")
    if precio_entrada is None or precio_salida is None:
        return None
    return (tokens_entrada * precio_entrada + tokens_salida * precio_salida) / TOKENS_POR_UNIDAD_DE_TARIFA


def _por_proveedor_modelo(db: Session, desde: datetime, hasta: datetime) -> list[dict]:
    """Uso y costo agrupados por (proveedor, modelo). El costo de un grupo es
    N/D si falta la tarifa configurada, o si alguna llamada del grupo no
    informó tokens (sumar solo las que sí tienen subestimaría el costo real
    sin decirlo)."""
    filas = (
        db.query(
            LlamadaIA.proveedor,
            LlamadaIA.modelo,
            func.count(LlamadaIA.id),
            func.count(LlamadaIA.tokens_entrada),
            func.sum(LlamadaIA.tokens_entrada),
            func.sum(LlamadaIA.tokens_salida),
        )
        .filter(LlamadaIA.creado_en >= desde, LlamadaIA.creado_en < hasta)
        .group_by(LlamadaIA.proveedor, LlamadaIA.modelo)
        .order_by(LlamadaIA.proveedor, LlamadaIA.modelo)
        .all()
    )

    resultado = []
    for proveedor, modelo, llamadas, con_tokens, suma_entrada, suma_salida in filas:
        tokens_completos = llamadas > 0 and con_tokens == llamadas
        tokens_entrada = int(suma_entrada) if tokens_completos else None
        tokens_salida = int(suma_salida) if tokens_completos else None

        costo = None
        if tokens_completos:
            tarifa = _tarifa(proveedor, modelo)
            if tarifa is not None:
                costo = _costo_usd(tokens_entrada, tokens_salida, tarifa)

        resultado.append(
            {
                "proveedor": proveedor,
                "modelo": modelo,
                "llamadas": llamadas,
                "tokens_entrada": tokens_entrada,
                "tokens_salida": tokens_salida,
                "costo_estimado_usd": costo,
            }
        )
    return resultado


def resumen(db: Session, desde: datetime, hasta: datetime) -> dict:
    """El dashboard completo: actividad del rango más el estado actual de
    pausa (que no es algo que tenga sentido recortar por fecha, ver el
    spec)."""
    ahora = datetime.now(timezone.utc)

    mensajes = _conteo_mensajes_por_rol(db, desde, hasta)
    conversaciones_con_actividad = _conversaciones_con_actividad(db, desde, hasta)
    conversaciones_nuevas = _conversaciones_nuevas(db, desde, hasta)

    filtro_rango = (LlamadaIA.creado_en >= desde, LlamadaIA.creado_en < hasta)
    llamadas_totales = db.query(func.count(LlamadaIA.id)).filter(*filtro_rango).scalar() or 0
    llamadas_error = (
        db.query(func.count(LlamadaIA.id))
        .filter(*filtro_rango, LlamadaIA.resultado.in_([ResultadoLlamadaIA.ERROR, ResultadoLlamadaIA.ERROR_TRANSITORIO]))
        .scalar()
        or 0
    )
    llamadas_escaladas = db.query(func.count(LlamadaIA.id)).filter(*filtro_rango, LlamadaIA.escalo.is_(True)).scalar() or 0
    duracion_promedio = (
        db.query(func.avg(LlamadaIA.duracion_ms)).filter(*filtro_rango, LlamadaIA.duracion_ms.isnot(None)).scalar()
    )

    total_conversaciones = db.query(func.count(Conversacion.id)).scalar() or 0
    total_pausadas = contar_pausadas(db, ahora)

    por_proveedor_modelo = _por_proveedor_modelo(db, desde, hasta)
    costos = [fila["costo_estimado_usd"] for fila in por_proveedor_modelo]
    costos_disponibles = [costo for costo in costos if costo is not None]
    costo_total = sum(costos_disponibles) if costos_disponibles else None
    costo_incompleto = len(costos_disponibles) < len(costos)

    # Mes calendario actual, no el rango desde/hasta del resto del dashboard
    # (mismo criterio que "estado_actual" arriba): el consumo de WhatsApp/Meta
    # se mide por mes de facturación, no por el recorte de fechas que se esté
    # mirando en pantalla.
    whatsapp_meta = consumo_mensual(db, ahora)

    return {
        "rango": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        "conversaciones": {
            "con_actividad": conversaciones_con_actividad,
            "nuevas": conversaciones_nuevas,
        },
        "mensajes": {
            "entrantes": mensajes[RolMensaje.USUARIO.value],
            "bot": mensajes[RolMensaje.BOT.value],
            "humano": mensajes[RolMensaje.HUMANO.value],
        },
        "estado_actual": {
            "total": total_conversaciones,
            "activas": total_conversaciones - total_pausadas,
            "pausadas": total_pausadas,
        },
        "modelo": {
            "llamadas": llamadas_totales,
            "errores": llamadas_error,
            "escaladas": llamadas_escaladas,
            "tasa_escalamiento": _div(llamadas_escaladas, llamadas_totales),
            "duracion_ms_promedio": float(duracion_promedio) if duracion_promedio is not None else None,
            "por_proveedor_modelo": por_proveedor_modelo,
            "costo_estimado_usd_total": costo_total,
            "costo_estimado_incompleto": costo_incompleto,
        },
        "mensajes_por_conversacion": _div(mensajes[RolMensaje.USUARIO.value], conversaciones_con_actividad),
        "whatsapp_meta": whatsapp_meta,
    }
