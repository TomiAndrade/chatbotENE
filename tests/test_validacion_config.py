"""Tests de validar_config() y resumen_config() (spec-validacion-config-arranque.md).

Llaman a las funciones directo con un Config armado a mano, sin pasar por el
startup de FastAPI ni por TestClient: son los criterios de aceptación 1 a 6
de la spec, ejercitados como tests unitarios en vez de a través del ciclo de
vida de la app (ver tests/conftest.py, fixture `client`, sobre por qué el
startup real no se puede disparar en esta suite).
"""

from dataclasses import replace
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.config import Config
from app.validacion_config import (
    ConfigInvalida,
    crm_sobre_https,
    minimo_seguro_agrupar_abandono_segundos,
    resumen_config,
    validar_config,
)


def _config_valida(**overrides) -> Config:
    """Config completa y coherente (proveedor `fijo`, Postgres): el punto de
    partida de cada test, así cada uno solo cambia lo que quiere romper."""
    base = Config(
        kapso_api_key="",
        kapso_phone_number_id="",
        kapso_webhook_secret="",
        meta_phone_number_id="000000000000",
        meta_access_token="token-de-meta",
        meta_app_secret="app-secret",
        meta_verify_token="verify-token",
        meta_api_version="v23.0",
        database_url="postgresql://usuario:password@host:5432/bot",
        proveedor_ia="fijo",
        debug=False,
        modelo="",
        anthropic_api_key="",
        base_url="",
        openai_compat_api_key="",
        timezone=ZoneInfo("America/Argentina/Buenos_Aires"),
        historial_max_mensajes=20,
        historial_dias_validez=7,
        limite_mensajes_hora=30,
        pausa_humana_minutos=120,
        escalamiento_habilitado=False,
        crm_habilitado=False,
        crm_base_url="",
        agrupar_ventana_segundos=2.0,
        agrupar_espera_maxima_segundos=8.0,
        agrupar_abandono_segundos=60.0,
        tarifas_ia={},
        meta_tarifa_service_ars=Decimal("37.6798"),
        meta_presupuesto_mensual_ars=Decimal("37679.80"),
    )
    return replace(base, **overrides)


def _config_con_crm(**overrides) -> Config:
    """Una config con el panel prendido y bien configurado. Cada test de CRM
    rompe una cosa sobre esta base."""
    base = dict(
        crm_habilitado=True,
        crm_base_url="https://bot.ene.example",
    )
    base.update(overrides)
    return _config_valida(**base)


def _mensaje_error(config: Config) -> str:
    with pytest.raises(ConfigInvalida) as exc_info:
        validar_config(config)
    return str(exc_info.value)


def test_config_completa_y_coherente_no_levanta_nada():
    """Criterio 6: con todo bien, validar_config() no hace nada — ni
    excepción ni efecto secundario."""
    validar_config(_config_valida())


def test_database_url_ausente():
    """Criterio 1: sin DATABASE_URL, el error nombra la variable."""
    mensaje = _mensaje_error(_config_valida(database_url=""))
    assert "DATABASE_URL" in mensaje


def test_database_url_solo_espacios_cuenta_como_ausente():
    mensaje = _mensaje_error(_config_valida(database_url="   "))
    assert "DATABASE_URL" in mensaje


def test_database_url_sqlite_es_invalida():
    """Criterio 2: SQLite no es un valor válido, aunque esté presente."""
    mensaje = _mensaje_error(_config_valida(database_url="sqlite:///./bot.db"))
    assert "DATABASE_URL" in mensaje
    assert "postgresql://" in mensaje


def test_database_url_postgresql_pasa():
    validar_config(_config_valida(database_url="postgresql://u:p@host:5432/bot"))


def test_proveedor_ia_ausente():
    """Criterio 3: sin PROVEEDOR_IA, el error nombra la variable."""
    mensaje = _mensaje_error(_config_valida(proveedor_ia=""))
    assert "PROVEEDOR_IA" in mensaje


def test_proveedor_ia_desconocido():
    mensaje = _mensaje_error(_config_valida(proveedor_ia="inventado"))
    assert "PROVEEDOR_IA" in mensaje
    assert "inventado" in mensaje


@pytest.mark.parametrize(
    "campo,nombre_variable",
    [
        ("meta_access_token", "META_ACCESS_TOKEN"),
        ("meta_phone_number_id", "META_PHONE_NUMBER_ID"),
        ("meta_verify_token", "META_VERIFY_TOKEN"),
        ("meta_app_secret", "META_APP_SECRET"),
    ],
)
def test_credencial_de_meta_ausente(campo, nombre_variable):
    mensaje = _mensaje_error(_config_valida(**{campo: ""}))
    assert nombre_variable in mensaje


def test_openai_compat_sin_api_key():
    """Criterio 4: proveedor openai_compat sin su API key explica por qué
    se exige."""
    mensaje = _mensaje_error(
        _config_valida(
            proveedor_ia="openai_compat",
            openai_compat_api_key="",
            base_url="https://openrouter.ai/api/v1",
            modelo="algun-modelo",
        )
    )
    assert "OPENAI_COMPAT_API_KEY" in mensaje
    assert "openai_compat" in mensaje


def test_openai_compat_sin_base_url():
    mensaje = _mensaje_error(
        _config_valida(
            proveedor_ia="openai_compat",
            openai_compat_api_key="key",
            base_url="",
            modelo="algun-modelo",
        )
    )
    assert "BASE_URL" in mensaje


def test_openai_compat_sin_modelo():
    mensaje = _mensaje_error(
        _config_valida(
            proveedor_ia="openai_compat",
            openai_compat_api_key="key",
            base_url="https://openrouter.ai/api/v1",
            modelo="",
        )
    )
    assert "MODELO" in mensaje


def test_openai_compat_completo_pasa():
    validar_config(
        _config_valida(
            proveedor_ia="openai_compat",
            openai_compat_api_key="key",
            base_url="https://openrouter.ai/api/v1",
            modelo="algun-modelo",
        )
    )


def test_claude_sin_api_key():
    mensaje = _mensaje_error(
        _config_valida(proveedor_ia="claude", anthropic_api_key="", modelo="claude-haiku")
    )
    assert "ANTHROPIC_API_KEY" in mensaje
    assert "claude" in mensaje


def test_claude_sin_modelo():
    mensaje = _mensaje_error(
        _config_valida(proveedor_ia="claude", anthropic_api_key="key", modelo="")
    )
    assert "MODELO" in mensaje


def test_claude_completo_pasa():
    validar_config(_config_valida(proveedor_ia="claude", anthropic_api_key="key", modelo="claude-haiku"))


def test_fijo_no_exige_ni_api_key_ni_modelo():
    """El proveedor `fijo` (el que usan los tests del resto de la suite) no
    tiene dependencias condicionales."""
    validar_config(_config_valida(proveedor_ia="fijo", modelo="", anthropic_api_key="", openai_compat_api_key=""))


def test_varios_errores_juntos_se_reportan_todos():
    """Criterio 5: tres variables faltantes aparecen las tres, no solo la
    primera."""
    mensaje = _mensaje_error(
        _config_valida(database_url="", proveedor_ia="", meta_access_token="")
    )
    assert "DATABASE_URL" in mensaje
    assert "PROVEEDOR_IA" in mensaje
    assert "META_ACCESS_TOKEN" in mensaje


def test_resumen_no_incluye_secretos():
    """Criterio 6: la línea de resumen no filtra usuario ni contraseña de
    DATABASE_URL, ni ningún token o key."""
    config = _config_valida(
        database_url="postgresql://usuario_secreto:password_secreto@dbhost:5432/bot_prod",
        meta_access_token="token-super-secreto",
        meta_app_secret="app-secret-secreto",
        anthropic_api_key="anthropic-key-secreta",
    )
    resumen = resumen_config(config)

    assert "usuario_secreto" not in resumen
    assert "password_secreto" not in resumen
    assert "token-super-secreto" not in resumen
    assert "app-secret-secreto" not in resumen
    assert "anthropic-key-secreta" not in resumen
    assert "dbhost" in resumen
    assert "bot_prod" in resumen


def test_resumen_incluye_lo_pedido_por_la_spec():
    config = _config_valida(
        proveedor_ia="claude",
        modelo="claude-haiku-4.5",
        database_url="postgresql://u:p@dbhost:5432/bot_prod",
        meta_api_version="v23.0",
        escalamiento_habilitado=True,
    )
    resumen = resumen_config(config)

    assert "claude" in resumen
    assert "claude-haiku-4.5" in resumen
    assert "dbhost" in resumen
    assert "bot_prod" in resumen
    assert "v23.0" in resumen
    assert "True" in resumen


# --- CRM (panel de conversaciones) ---------------------------------------
#
# El CRM viene apagado (CRM_HABILITADO=false): apagado no se registra ninguna
# de sus rutas y nada de esto se valida. Prendido, lo único obligatorio es
# CRM_BASE_URL — el login es propio y las cuentas viven en la base, así que
# no hay ninguna credencial del panel en la config.


def test_con_el_crm_apagado_la_validacion_no_se_queja():
    """El default tiene que seguir siendo válido: quien solo corre el bot no
    necesita configurar nada nuevo."""
    validar_config(_config_valida(crm_habilitado=False))


def test_crm_bien_configurado_es_valido():
    validar_config(_config_con_crm())


def test_falta_la_base_url_del_crm():
    mensaje = _mensaje_error(_config_con_crm(crm_base_url=""))
    assert "CRM_BASE_URL" in mensaje


def test_produccion_con_http_no_arranca():
    """Sobre http la cookie de sesión no puede salir con el flag Secure, y la
    contraseña del login viajaría en claro. Solo se permite en local."""
    mensaje = _mensaje_error(_config_con_crm(crm_base_url="http://bot.ene.example", debug=False))
    assert "CRM_BASE_URL" in mensaje
    assert "https" in mensaje


def test_http_en_localhost_con_debug_si_arranca():
    """La única excepción, explícita: desarrollo local."""
    validar_config(_config_con_crm(crm_base_url="http://localhost:8000", debug=True))


def test_http_en_localhost_sin_debug_no_arranca():
    """Localhost solo no alcanza: `DEBUG=false` es producción."""
    mensaje = _mensaje_error(_config_con_crm(crm_base_url="http://localhost:8000", debug=False))
    assert "CRM_BASE_URL" in mensaje


def test_la_base_url_no_puede_tener_ruta():
    mensaje = _mensaje_error(_config_con_crm(crm_base_url="https://bot.ene.example/panel"))
    assert "CRM_BASE_URL" in mensaje


def test_una_base_url_que_no_es_url_no_arranca():
    mensaje = _mensaje_error(_config_con_crm(crm_base_url="bot.ene.example"))
    assert "CRM_BASE_URL" in mensaje


def test_crm_sobre_https_decide_el_flag_secure():
    assert crm_sobre_https(_config_con_crm()) is True
    assert crm_sobre_https(_config_con_crm(crm_base_url="http://localhost:8000", debug=True)) is False


# --- Agrupamiento de mensajes (specs/spec-agrupamiento-mensajes.md) --------


@pytest.mark.parametrize(
    "campo,nombre_variable",
    [
        ("agrupar_ventana_segundos", "AGRUPAR_VENTANA_SEGUNDOS"),
        ("agrupar_espera_maxima_segundos", "AGRUPAR_ESPERA_MAXIMA_SEGUNDOS"),
        ("agrupar_abandono_segundos", "AGRUPAR_ABANDONO_SEGUNDOS"),
    ],
)
@pytest.mark.parametrize("valor", [0, -1])
def test_agrupar_valor_no_positivo_no_arranca(campo, nombre_variable, valor):
    mensaje = _mensaje_error(_config_valida(**{campo: valor}))
    assert nombre_variable in mensaje


def test_agrupar_abandono_por_debajo_del_minimo_seguro_no_arranca():
    """El abandono tiene que quedar por encima del peor caso legítimo (espera
    máxima + presupuesto de la llamada al modelo + reintentos de envío a
    Meta + margen) — si no, una generación que todavía está en curso de
    verdad puede perder su reserva antes de terminar y otro proceso la
    retoma encima de ella."""
    minimo = minimo_seguro_agrupar_abandono_segundos(_config_valida())

    mensaje = _mensaje_error(_config_valida(agrupar_abandono_segundos=minimo))
    assert "AGRUPAR_ABANDONO_SEGUNDOS" in mensaje


def test_agrupar_abandono_justo_por_encima_del_minimo_seguro_pasa():
    minimo = minimo_seguro_agrupar_abandono_segundos(_config_valida())

    validar_config(_config_valida(agrupar_abandono_segundos=minimo + 1))


def test_agrupar_valores_por_default_de_la_spec_son_validos():
    """Los defaults propuestos en specs/spec-agrupamiento-mensajes.md (2/8/60)
    tienen que pasar la validación real, no solo la cuenta a mano del spec."""
    validar_config(
        _config_valida(
            agrupar_ventana_segundos=2.0,
            agrupar_espera_maxima_segundos=8.0,
            agrupar_abandono_segundos=60.0,
        )
    )


# --- Control preventivo de gasto de WhatsApp/Meta (specs/spec-costo-whatsapp-meta.md) --


@pytest.mark.parametrize(
    "campo,nombre_variable",
    [
        ("meta_tarifa_service_ars", "META_TARIFA_SERVICE_ARS"),
        ("meta_presupuesto_mensual_ars", "META_PRESUPUESTO_MENSUAL_ARS"),
    ],
)
@pytest.mark.parametrize("valor", [Decimal("0"), Decimal("-1")])
def test_costo_meta_valor_no_positivo_no_arranca(campo, nombre_variable, valor):
    mensaje = _mensaje_error(_config_valida(**{campo: valor}))
    assert nombre_variable in mensaje


def test_costo_meta_valores_por_default_de_la_spec_son_validos():
    """Los defaults del encargo (37.6798 / 37679.80) tienen que pasar la
    validación real, no solo la cuenta a mano."""
    validar_config(
        _config_valida(
            meta_tarifa_service_ars=Decimal("37.6798"),
            meta_presupuesto_mensual_ars=Decimal("37679.80"),
        )
    )


def test_el_resumen_dice_si_el_crm_esta_prendido():
    """Prendido o apagado y nada más: quién tiene cuenta son datos de
    personas y no hacen falta en el log de arranque."""
    prendido = resumen_config(_config_con_crm())
    apagado = resumen_config(_config_valida())

    assert "crm=activo" in prendido
    assert "crm=apagado" in apagado
