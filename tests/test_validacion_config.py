"""Tests de validar_config() y resumen_config() (spec-validacion-config-arranque.md).

Llaman a las funciones directo con un Config armado a mano, sin pasar por el
startup de FastAPI ni por TestClient: son los criterios de aceptación 1 a 6
de la spec, ejercitados como tests unitarios en vez de a través del ciclo de
vida de la app (ver tests/conftest.py, fixture `client`, sobre por qué el
startup real no se puede disparar en esta suite).
"""

from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from app.config import Config
from app.validacion_config import ConfigInvalida, resumen_config, validar_config


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
    )
    return replace(base, **overrides)


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
