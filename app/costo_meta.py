"""Control preventivo de gasto de WhatsApp/Meta — etapa 1
(specs/spec-costo-whatsapp-meta.md).

Es una ESTIMACIÓN preventiva: el sistema hoy no concilia contra la factura
real de Meta ni persiste los estados `sent`/`delivered`/`read`/`failed` de
`statuses[]`. Sirve para tener una idea del consumo mensual y no gastar de
más sin darse cuenta — no para cuadrar centavos contra Meta Business Suite.

Mismo patrón que app/limite.py y app/pausa.py: un módulo de dominio propio,
sin nada de CRM, que tanto app/main.py (para contabilizar) como
app/crm/metricas.py (para mostrar el consumo en el panel) importan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import config
from app.models import Conversacion, EnvioWhatsapp, Mensaje

# Única categoría de esta etapa (ver spec, sección "Categoría"): todo envío
# saliente se tarifa como "service", sin distinguir todavía Marketing/Utility/
# Authentication — eso queda fuera de alcance a propósito.
CATEGORIA_SERVICE = "service"


def contabilizar_envio(db: Session, conversacion: Conversacion, mensaje_bot: Mensaje) -> EnvioWhatsapp | None:
    """Agrega (sin comitear) la fila de costo estimado para un envío que Meta
    ya aceptó. La llama `enviar_y_guardar` (app/main.py) en la misma
    transacción que crea `mensaje_bot`, después de un `db.flush()` que le da
    id — así las dos filas se comitean juntas: no puede quedar un `Mensaje`
    sin su costo, ni un costo sin el mensaje que lo generó.

    None si `mensaje_bot.wa_message_id` está vacío (nunca debería pasar si el
    llamador ya comprobó que Meta devolvió un id) — no contabiliza nada sin
    "evidencia suficiente" del envío, misma condición que exige el spec.
    """
    if not mensaje_bot.wa_message_id:
        return None

    envio = EnvioWhatsapp(
        conversacion_id=conversacion.id,
        mensaje_id=mensaje_bot.id,
        wa_message_id=mensaje_bot.wa_message_id,
        categoria=CATEGORIA_SERVICE,
        tarifa_ars=config.meta_tarifa_service_ars,
        # Costo por mensaje aceptado = la tarifa configurada, sin
        # multiplicadores todavía (ver spec, "Cálculo del costo").
        costo_estimado_ars=config.meta_tarifa_service_ars,
    )
    db.add(envio)
    return envio


def _limites_mes_actual_utc(ahora: datetime) -> tuple[datetime, datetime]:
    """El mes calendario de `ahora` en TIMEZONE, como límites UTC
    `[inicio, fin)` — mismo criterio que `app.crm.metricas.rango_utc`: la
    base guarda todo en UTC, pero el mes que le importa a alguien mirando el
    panel es el mes en Argentina, no en UTC."""
    ahora_local = ahora.astimezone(config.timezone)
    inicio_local = ahora_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if inicio_local.month == 12:
        fin_local = inicio_local.replace(year=inicio_local.year + 1, month=1)
    else:
        fin_local = inicio_local.replace(month=inicio_local.month + 1)
    return inicio_local.astimezone(timezone.utc), fin_local.astimezone(timezone.utc)


def consumo_mensual(db: Session, ahora: datetime) -> dict:
    """Cantidad y costo estimado acumulado del mes calendario de `ahora`
    (TIMEZONE), contra el presupuesto configurado. El cambio de mes ocurre
    solo por el rango de fechas de la consulta — no hay ninguna fila que se
    borre ni se resetee, así que el histórico de meses anteriores queda
    intacto en `envios_whatsapp` para siempre.

    `ahora` se recibe como parámetro (no `datetime.now()` adentro) para que
    los tests puedan fijar en qué mes están parados, mismo criterio que el
    resto del código que depende de la hora (ver RelojFijo en tests/helpers.py).
    """
    inicio, fin = _limites_mes_actual_utc(ahora)
    filtro = (EnvioWhatsapp.creado_en >= inicio, EnvioWhatsapp.creado_en < fin)

    cantidad = db.query(func.count(EnvioWhatsapp.id)).filter(*filtro).scalar() or 0
    costo_acumulado = db.query(func.sum(EnvioWhatsapp.costo_estimado_ars)).filter(*filtro).scalar() or Decimal("0")

    presupuesto = config.meta_presupuesto_mensual_ars
    # validar_config() ya garantiza presupuesto > 0 al arrancar la app real;
    # el guard queda igual acá porque este módulo también corre bajo tests
    # que no pasan por validar_config() (ver tests/conftest.py).
    porcentaje_utilizado = float(costo_acumulado / presupuesto) if presupuesto > 0 else None

    return {
        "mes": inicio.astimezone(config.timezone).strftime("%Y-%m"),
        "mensajes_contabilizados": cantidad,
        "costo_estimado_ars": costo_acumulado,
        "presupuesto_mensual_ars": presupuesto,
        "porcentaje_utilizado": porcentaje_utilizado,
        "saldo_estimado_restante_ars": presupuesto - costo_acumulado,
    }
