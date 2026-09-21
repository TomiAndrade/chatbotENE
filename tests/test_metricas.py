"""Agregados del dashboard de costos y actividad (app/crm/metricas.py, ver
specs/spec-dashboard-metricas.md): rango de fechas y zona horaria, conteos,
costo estimado con tarifas configurables, y que todo salga N/D sin datos
suficientes en vez de un cero engañoso.

No pasa por el webhook: arma conversaciones, mensajes y llamadas directo en
la base (mismo criterio que tests/test_crm_historial.py) porque lo que se
prueba es qué agrega el dashboard, no cómo llegó cada fila.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import config
from app.crm import metricas
from app.models import Conversacion, LlamadaIA, Mensaje, MotivoPausa, ResultadoLlamadaIA, RolMensaje

ZONA = config.timezone  # America/Argentina/Buenos_Aires, UTC-3 todo el año


def _conversacion(db, identificador: str, creada_en: datetime, **kwargs) -> Conversacion:
    conversacion = Conversacion(canal="whatsapp", identificador_externo=identificador, **kwargs)
    db.add(conversacion)
    db.commit()
    db.refresh(conversacion)
    conversacion.creada_en = creada_en
    db.commit()
    db.refresh(conversacion)
    return conversacion


def _mensaje(db, conversacion: Conversacion, rol: RolMensaje, creado_en: datetime) -> Mensaje:
    mensaje = Mensaje(conversacion_id=conversacion.id, rol=rol, contenido="x", creado_en=creado_en)
    db.add(mensaje)
    db.commit()
    return mensaje


def _llamada(
    db,
    conversacion: Conversacion,
    creado_en: datetime,
    *,
    proveedor: str = "claude",
    modelo: str | None = "modelo-de-test",
    resultado: ResultadoLlamadaIA = ResultadoLlamadaIA.OK,
    escalo: bool = False,
    duracion_ms: int | None = 1000,
    tokens_entrada: int | None = None,
    tokens_salida: int | None = None,
) -> LlamadaIA:
    llamada = LlamadaIA(
        conversacion_id=conversacion.id,
        proveedor=proveedor,
        modelo=modelo,
        creado_en=creado_en,
        duracion_ms=duracion_ms,
        resultado=resultado,
        escalo=escalo,
        tokens_entrada=tokens_entrada,
        tokens_salida=tokens_salida,
    )
    db.add(llamada)
    db.commit()
    return llamada


# --- Rango de fechas y zona horaria --------------------------------------


def test_rango_por_defecto_son_los_ultimos_7_dias():
    inicio, fin = metricas.rango_utc(None, None)

    assert (fin - inicio) == timedelta(days=7)
    # Medianoche en Buenos Aires (UTC-3) es las 03:00 UTC.
    assert inicio.hour == 3
    assert inicio.tzinfo is not None and fin.tzinfo is not None


def test_rango_explicito_se_interpreta_en_la_zona_horaria_configurada():
    inicio, fin = metricas.rango_utc("2026-01-01", "2026-01-01")

    assert inicio == datetime(2026, 1, 1, 3, 0, 0, tzinfo=timezone.utc)
    # `hasta` es el día entero: el límite real es el inicio del día siguiente.
    assert fin == datetime(2026, 1, 2, 3, 0, 0, tzinfo=timezone.utc)


def test_rango_de_varios_dias_incluye_el_ultimo_dia_completo():
    inicio, fin = metricas.rango_utc("2026-01-01", "2026-01-03")

    assert (fin - inicio) == timedelta(days=3)


@pytest.mark.parametrize("desde,hasta", [("no-es-fecha", None), (None, "2026-13-40"), ("2026-01-01", "01/01/2026")])
def test_fecha_con_formato_invalido_lanza_rango_invalido(desde, hasta):
    with pytest.raises(metricas.RangoInvalido):
        metricas.rango_utc(desde, hasta)


def test_desde_posterior_a_hasta_lanza_rango_invalido():
    with pytest.raises(metricas.RangoInvalido):
        metricas.rango_utc("2026-01-10", "2026-01-01")


def test_un_mensaje_justo_antes_de_la_medianoche_local_cae_en_el_dia_correcto(db):
    """23:50 hora de Buenos Aires del 1 de enero sigue siendo "1 de enero"
    para el dashboard, aunque en UTC ya sea 2 de enero — es la razón de ser
    de convertir el rango a la zona horaria antes de consultar."""
    conversacion = _conversacion(db, "5491111111111", creada_en=datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc))
    instante_utc = datetime(2026, 1, 2, 2, 50, tzinfo=timezone.utc)  # 23:50 del 1/1 en Bs. As.
    _mensaje(db, conversacion, RolMensaje.USUARIO, instante_utc)

    inicio, fin = metricas.rango_utc("2026-01-01", "2026-01-01")
    datos = metricas.resumen(db, inicio, fin)

    assert datos["mensajes"]["entrantes"] == 1

    inicio2, fin2 = metricas.rango_utc("2026-01-02", "2026-01-02")
    datos2 = metricas.resumen(db, inicio2, fin2)
    assert datos2["mensajes"]["entrantes"] == 0


# --- Conteos de conversaciones y mensajes ---------------------------------


def test_cuenta_conversaciones_con_actividad_y_nuevas_dentro_del_rango(db):
    dentro = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)
    fuera = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)

    conv_nueva_y_activa = _conversacion(db, "5490000000001", creada_en=dentro)
    _mensaje(db, conv_nueva_y_activa, RolMensaje.USUARIO, dentro)

    conv_vieja_pero_activa = _conversacion(db, "5490000000002", creada_en=fuera)
    _mensaje(db, conv_vieja_pero_activa, RolMensaje.BOT, dentro)

    conv_sin_actividad_en_rango = _conversacion(db, "5490000000003", creada_en=fuera)
    _mensaje(db, conv_sin_actividad_en_rango, RolMensaje.USUARIO, fuera)

    inicio, fin = datetime(2026, 3, 5, tzinfo=timezone.utc), datetime(2026, 3, 15, tzinfo=timezone.utc)
    datos = metricas.resumen(db, inicio, fin)

    assert datos["conversaciones"]["con_actividad"] == 2
    assert datos["conversaciones"]["nuevas"] == 1


def test_mensajes_se_cuentan_por_rol(db):
    dentro = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)
    conversacion = _conversacion(db, "5490000000004", creada_en=dentro)
    _mensaje(db, conversacion, RolMensaje.USUARIO, dentro)
    _mensaje(db, conversacion, RolMensaje.USUARIO, dentro)
    _mensaje(db, conversacion, RolMensaje.BOT, dentro)
    _mensaje(db, conversacion, RolMensaje.HUMANO, dentro)

    inicio, fin = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 20, tzinfo=timezone.utc)
    datos = metricas.resumen(db, inicio, fin)

    assert datos["mensajes"] == {"entrantes": 2, "bot": 1, "humano": 1}
    assert datos["mensajes_por_conversacion"] == pytest.approx(2 / 1)


def test_estado_actual_es_independiente_del_rango(db):
    """Activas/pausadas es un snapshot de ahora mismo, no algo que dependa
    del rango elegido — una conversación pausada hoy sigue contando aunque el
    rango pedido sea de hace un mes."""
    ahora = datetime.now(timezone.utc)
    _conversacion(db, "5490000000005", creada_en=ahora)
    _conversacion(
        db, "5490000000006", creada_en=ahora,
        modo_humano=True, motivo_pausa=MotivoPausa.ESCALAMIENTO, modo_humano_desde=ahora,
    )

    rango_viejo = metricas.rango_utc("2000-01-01", "2000-01-01")
    datos = metricas.resumen(db, *rango_viejo)

    assert datos["estado_actual"] == {"total": 2, "activas": 1, "pausadas": 1}


# --- Llamadas al modelo: errores, escalamiento, tiempo de respuesta ------


def test_llamadas_errores_y_escaladas_se_cuentan_en_el_rango(db):
    dentro = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)
    conversacion = _conversacion(db, "5490000000007", creada_en=dentro)

    _llamada(db, conversacion, dentro, resultado=ResultadoLlamadaIA.OK, escalo=False, duracion_ms=800)
    _llamada(db, conversacion, dentro, resultado=ResultadoLlamadaIA.OK, escalo=True, duracion_ms=1200)
    _llamada(db, conversacion, dentro, resultado=ResultadoLlamadaIA.ERROR, escalo=True, duracion_ms=None)
    _llamada(db, conversacion, dentro, resultado=ResultadoLlamadaIA.ERROR_TRANSITORIO, escalo=False, duracion_ms=None)

    inicio, fin = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 20, tzinfo=timezone.utc)
    datos = metricas.resumen(db, inicio, fin)["modelo"]

    assert datos["llamadas"] == 4
    assert datos["errores"] == 2
    assert datos["escaladas"] == 2
    assert datos["tasa_escalamiento"] == pytest.approx(0.5)
    # Promedio solo sobre las dos llamadas que sí midieron duración.
    assert datos["duracion_ms_promedio"] == pytest.approx(1000.0)


def test_llamadas_fuera_del_rango_no_se_cuentan(db):
    conversacion = _conversacion(db, "5490000000008", creada_en=datetime(2026, 3, 1, tzinfo=timezone.utc))
    _llamada(db, conversacion, datetime(2026, 2, 1, tzinfo=timezone.utc))

    inicio, fin = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 20, tzinfo=timezone.utc)
    datos = metricas.resumen(db, inicio, fin)["modelo"]

    assert datos["llamadas"] == 0


# --- Costo estimado, con tarifas configurables ----------------------------


def test_costo_estimado_usa_la_tarifa_configurada(db, monkeypatch):
    monkeypatch.setattr(
        config, "tarifas_ia",
        {"claude:modelo-x": {"entrada": 1.0, "salida": 2.0}},
    )
    dentro = datetime(2026, 3, 10, tzinfo=timezone.utc)
    conversacion = _conversacion(db, "5490000000009", creada_en=dentro)
    _llamada(
        db, conversacion, dentro, proveedor="claude", modelo="modelo-x",
        tokens_entrada=1_000_000, tokens_salida=500_000,
    )

    inicio, fin = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 20, tzinfo=timezone.utc)
    fila = metricas.resumen(db, inicio, fin)["modelo"]["por_proveedor_modelo"][0]

    # 1.000.000 tokens de entrada a USD 1/millón + 500.000 de salida a USD 2/millón.
    assert fila["costo_estimado_usd"] == pytest.approx(1.0 + 1.0)


def test_sin_tarifa_configurada_el_costo_es_nd(db, monkeypatch):
    monkeypatch.setattr(config, "tarifas_ia", {})
    dentro = datetime(2026, 3, 10, tzinfo=timezone.utc)
    conversacion = _conversacion(db, "5490000000010", creada_en=dentro)
    _llamada(db, conversacion, dentro, proveedor="claude", modelo="modelo-x", tokens_entrada=100, tokens_salida=50)

    inicio, fin = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 20, tzinfo=timezone.utc)
    resultado = metricas.resumen(db, inicio, fin)["modelo"]

    fila = resultado["por_proveedor_modelo"][0]
    assert fila["tokens_entrada"] == 100  # los tokens sí se muestran
    assert fila["costo_estimado_usd"] is None  # el costo no, sin tarifa
    assert resultado["costo_estimado_usd_total"] is None


def test_tokens_parciales_en_un_grupo_dan_nd_en_vez_de_subestimar(db, monkeypatch):
    """Si una llamada del grupo no informó tokens, sumar solo las que sí
    los tienen daría un total más bajo que el real sin decirlo — mejor N/D
    que un número que parece preciso y no lo es."""
    monkeypatch.setattr(config, "tarifas_ia", {"claude:modelo-x": {"entrada": 1.0, "salida": 1.0}})
    dentro = datetime(2026, 3, 10, tzinfo=timezone.utc)
    conversacion = _conversacion(db, "5490000000011", creada_en=dentro)
    _llamada(db, conversacion, dentro, proveedor="claude", modelo="modelo-x", tokens_entrada=100, tokens_salida=50)
    _llamada(db, conversacion, dentro, proveedor="claude", modelo="modelo-x", tokens_entrada=None, tokens_salida=None)

    inicio, fin = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 20, tzinfo=timezone.utc)
    fila = metricas.resumen(db, inicio, fin)["modelo"]["por_proveedor_modelo"][0]

    assert fila["llamadas"] == 2
    assert fila["tokens_entrada"] is None
    assert fila["costo_estimado_usd"] is None


def test_costo_total_parcial_se_marca_incompleto(db, monkeypatch):
    monkeypatch.setattr(
        config, "tarifas_ia",
        {"claude:con-tarifa": {"entrada": 1.0, "salida": 1.0}},
    )
    dentro = datetime(2026, 3, 10, tzinfo=timezone.utc)
    conversacion = _conversacion(db, "5490000000012", creada_en=dentro)
    _llamada(db, conversacion, dentro, proveedor="claude", modelo="con-tarifa", tokens_entrada=1000, tokens_salida=1000)
    _llamada(db, conversacion, dentro, proveedor="openai_compat", modelo="sin-tarifa", tokens_entrada=1000, tokens_salida=1000)

    inicio, fin = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 20, tzinfo=timezone.utc)
    resultado = metricas.resumen(db, inicio, fin)["modelo"]

    assert resultado["costo_estimado_usd_total"] == pytest.approx(0.002)
    assert resultado["costo_estimado_incompleto"] is True


# --- Cero datos: todo N/D, nunca cero engañoso ----------------------------


def test_sin_ninguna_actividad_todo_sale_nd(db):
    inicio, fin = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 20, tzinfo=timezone.utc)

    datos = metricas.resumen(db, inicio, fin)

    assert datos["conversaciones"] == {"con_actividad": 0, "nuevas": 0}
    assert datos["mensajes"] == {"entrantes": 0, "bot": 0, "humano": 0}
    assert datos["mensajes_por_conversacion"] is None
    assert datos["modelo"]["llamadas"] == 0
    assert datos["modelo"]["tasa_escalamiento"] is None
    assert datos["modelo"]["duracion_ms_promedio"] is None
    assert datos["modelo"]["por_proveedor_modelo"] == []
    assert datos["modelo"]["costo_estimado_usd_total"] is None
    assert datos["modelo"]["costo_estimado_incompleto"] is False
