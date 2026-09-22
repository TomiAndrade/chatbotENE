"""Control preventivo de gasto de WhatsApp/Meta — etapas 1 y 2.1
(specs/spec-costo-whatsapp-meta.md, specs/spec-tope-duro-meta.md).

Es una ESTIMACIÓN preventiva: el sistema hoy no concilia contra la factura
real de Meta ni persiste los estados `sent`/`delivered`/`read`/`failed` de
`statuses[]`. Sirve para tener una idea del consumo mensual y no gastar de
más sin darse cuenta — no para cuadrar centavos contra Meta Business Suite.

Mismo patrón que app/limite.py y app/pausa.py: un módulo de dominio propio,
sin nada de CRM, que tanto app/main.py (para contabilizar y, con
META_TOPE_DURO_HABILITADO=true, para reservar/liberar presupuesto) como
app/crm/metricas.py (para mostrar el consumo en el panel) importan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import config
from app.models import Conversacion, EnvioWhatsapp, Mensaje, PresupuestoMetaMensual

# Única categoría de esta etapa (ver spec, sección "Categoría"): todo envío
# saliente se tarifa como "service", sin distinguir todavía Marketing/Utility/
# Authentication — eso queda fuera de alcance a propósito.
CATEGORIA_SERVICE = "service"


def mes_actual(ahora: datetime) -> str:
    """Clave `YYYY-MM` del mes calendario de `ahora` en TIMEZONE.

    Único punto que decide "qué mes es ahora" para todo lo de costo de
    Meta: lo usa `consumo_mensual` (para el rango de la consulta y la
    etiqueta que devuelve) y `reservar_gasto`/`asegurar_fila_mensual` (para
    la clave de la fila de control del tope duro). Que las dos vistas del
    mes — lo que se muestra y lo que bloquea — compartan el mismo criterio
    temporal es intencional: specs/spec-tope-duro-meta.md lo pide
    explícito, para que nunca puedan desincronizarse por una diferencia de
    huso horario o de redondeo.
    """
    return ahora.astimezone(config.timezone).strftime("%Y-%m")


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
        "mes": mes_actual(ahora),
        "mensajes_contabilizados": cantidad,
        "costo_estimado_ars": costo_acumulado,
        "presupuesto_mensual_ars": presupuesto,
        "porcentaje_utilizado": porcentaje_utilizado,
        "saldo_estimado_restante_ars": presupuesto - costo_acumulado,
    }


# --- Tope duro mensual, etapa 2.1 (specs/spec-tope-duro-meta.md) ----------
#
# Solo se ejercita con META_TOPE_DURO_HABILITADO=true (app/main.py,
# enviar_y_guardar). Con el flag apagado nada de acá abajo se llama.


def asegurar_fila_mensual(db: Session, mes: str) -> PresupuestoMetaMensual:
    """Get-or-create de la fila de control de `mes`. Segura ante dos
    procesos/hilos creándola a la vez: primero intenta el `SELECT` (camino
    rápido — la fila ya existe casi siempre, salvo el primer envío del
    mes); si no está, inserta, y si otro ganó la carrera entre el `SELECT`
    y el `INSERT` la `UNIQUE` de `mes` tira `IntegrityError`. Mismo patrón
    que la dedup de `wa_message_id` en `procesar_mensaje_entrante` (ver
    CLAUDE.md, "La dedup es por wa_message_id").

    El intento de `INSERT` va dentro de un `SAVEPOINT` (`db.begin_nested()`),
    a propósito: un `db.rollback()` a secas deshace TODA la transacción
    externa de `db`, no solo este `INSERT` — si en el momento de la carrera
    hubiera otro cambio ya pendiente y sin comitear en la misma sesión (hoy
    no lo hay en ningún llamador real, pero nada del código lo garantiza),
    ese rollback global se lo llevaría puesto. Con el SAVEPOINT, un
    `IntegrityError` solo deshace lo que pasó dentro del `with` —el intento
    de `INSERT`— y dejar la sesión con cualquier otro pendiente intacto.

    `presupuesto_ars` queda fijo al valor de config vigente en este
    instante — ver el docstring de `PresupuestoMetaMensual`.
    """
    fila = db.query(PresupuestoMetaMensual).filter_by(mes=mes).first()
    if fila is not None:
        return fila

    fila = PresupuestoMetaMensual(
        mes=mes,
        presupuesto_ars=config.meta_presupuesto_mensual_ars,
        costo_comprometido_ars=Decimal("0"),
    )
    try:
        with db.begin_nested():
            # flush explícito: esta sesión tiene autoflush=False (ver
            # app/db.py), así que sin esto el INSERT no se manda todavía y
            # el IntegrityError no aparecería acá adentro, sino recién en
            # el próximo flush/commit — ya fuera del SAVEPOINT, que es
            # justo lo que hay que evitar.
            db.add(fila)
            db.flush()
    except IntegrityError:
        # begin_nested() ya emitió "ROLLBACK TO SAVEPOINT" al salir del
        # `with` por la excepción, antes de que llegue hasta acá — la
        # transacción externa sigue viva y cualquier otro pendiente de
        # `db` sigue ahí. No hace falta (ni corresponde) un
        # db.rollback() global acá.
        fila = db.query(PresupuestoMetaMensual).filter_by(mes=mes).one()
    else:
        db.commit()
    return fila


def reservar_gasto(db: Session, mes: str) -> bool:
    """Reserva la tarifa configurada (`META_TARIFA_SERVICE_ARS`) contra el
    presupuesto de `mes`, en una única sentencia `UPDATE` atómica: el check
    ("¿entra?") y la reserva ("sumalo") son la misma operación, no dos
    pasos separados que dejarían una ventana de carrera entre hilos o
    procesos concurrentes — el motor de base serializa el `UPDATE` a nivel
    de fila, mismo idioma que `_reclamar_generacion` en app/main.py
    (`UPDATE` condicional, el `rowcount` dice si ganaste), aplicado acá a
    una suma en vez de a un token.

    Devuelve `True` si la reserva entró (`rowcount == 1`); `False` si
    hubiera superado el presupuesto (`rowcount == 0`) — en ese caso
    `enviar_y_guardar` (app/main.py) no llama a Meta.
    """
    asegurar_fila_mensual(db, mes)

    tarifa = config.meta_tarifa_service_ars
    filas_afectadas = (
        db.query(PresupuestoMetaMensual)
        .filter(
            PresupuestoMetaMensual.mes == mes,
            PresupuestoMetaMensual.costo_comprometido_ars + tarifa <= PresupuestoMetaMensual.presupuesto_ars,
        )
        .update(
            {
                PresupuestoMetaMensual.costo_comprometido_ars:
                    PresupuestoMetaMensual.costo_comprometido_ars + tarifa
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return filas_afectadas == 1


def liberar_reserva(db: Session, mes: str) -> None:
    """Deshace una reserva tomada con `reservar_gasto` — solo cuando Meta
    rechazó o falló el envío.

    Si Meta aceptó el envío y lo que falló fue la persistencia posterior de
    `Mensaje`/`EnvioWhatsapp`, **no se llama a esto**: el gasto ya ocurrió
    del lado de Meta aunque no quede el detalle guardado localmente, y
    liberar la reserva dejaría que el tope mensual se corra hacia arriba
    con cada falla de ese tipo — ver el docstring de `PresupuestoMetaMensual`
    y specs/spec-tope-duro-meta.md.
    """
    tarifa = config.meta_tarifa_service_ars
    db.query(PresupuestoMetaMensual).filter(PresupuestoMetaMensual.mes == mes).update(
        {PresupuestoMetaMensual.costo_comprometido_ars: PresupuestoMetaMensual.costo_comprometido_ars - tarifa},
        synchronize_session=False,
    )
    db.commit()
