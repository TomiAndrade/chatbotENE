"""Matriz de verificación de firma y de challenge del webhook de Meta (ver
spec-meta-cloud-api.md, secciones 1 y 4). Análogo a test_firma.py para Kapso:
mismo criterio de "sin secreto y DEBUG=false, cerrado", más lo específico de
Meta — el prefijo "sha256=" y el challenge GET."""

import hashlib
import hmac

from app.config import config
from app.meta import verificar_challenge, verificar_firma_webhook

CUERPO = b'{"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.1"}]}}]}]}'
SECRETO = "un-secreto-cualquiera"


def _firma_correcta(cuerpo: bytes = CUERPO, secreto: str = SECRETO) -> str:
    return "sha256=" + hmac.new(secreto.encode("utf-8"), cuerpo, hashlib.sha256).hexdigest()


def test_firma_valida_pasa(monkeypatch):
    monkeypatch.setattr(config, "meta_app_secret", SECRETO)
    monkeypatch.setattr(config, "debug", False)

    assert verificar_firma_webhook(CUERPO, _firma_correcta()) is True


def test_firma_invalida_se_rechaza(monkeypatch):
    monkeypatch.setattr(config, "meta_app_secret", SECRETO)
    monkeypatch.setattr(config, "debug", False)

    assert verificar_firma_webhook(CUERPO, "sha256=" + "0" * 64) is False


def test_firma_ausente_se_rechaza(monkeypatch):
    monkeypatch.setattr(config, "meta_app_secret", SECRETO)
    monkeypatch.setattr(config, "debug", False)

    assert verificar_firma_webhook(CUERPO, None) is False


def test_firma_sin_prefijo_sha256_se_rechaza(monkeypatch):
    """Meta siempre manda el valor con el prefijo "sha256=". Un hexdigest
    pelado (como lo manda Kapso) no es una firma válida acá."""
    monkeypatch.setattr(config, "meta_app_secret", SECRETO)
    monkeypatch.setattr(config, "debug", False)

    hexdigest_pelado = hmac.new(SECRETO.encode("utf-8"), CUERPO, hashlib.sha256).hexdigest()
    assert verificar_firma_webhook(CUERPO, hexdigest_pelado) is False


def test_cuerpo_alterado_invalida_la_firma(monkeypatch):
    monkeypatch.setattr(config, "meta_app_secret", SECRETO)
    monkeypatch.setattr(config, "debug", False)

    firma = _firma_correcta()
    assert verificar_firma_webhook(CUERPO + b"algo mas", firma) is False


def test_sin_secreto_y_debug_false_se_rechaza_todo(monkeypatch):
    """El caso que importa para el checklist de deploy: sin App Secret
    configurado y DEBUG=false, el webhook tiene que quedar cerrado."""
    monkeypatch.setattr(config, "meta_app_secret", "")
    monkeypatch.setattr(config, "debug", False)

    assert verificar_firma_webhook(CUERPO, _firma_correcta()) is False
    assert verificar_firma_webhook(CUERPO, None) is False


def test_sin_secreto_y_debug_true_deja_pasar(monkeypatch):
    """Permitido solo para poder levantar el proyecto en desarrollo antes de
    tener el App Secret de la app de Meta."""
    monkeypatch.setattr(config, "meta_app_secret", "")
    monkeypatch.setattr(config, "debug", True)

    assert verificar_firma_webhook(CUERPO, None) is True


def test_verificar_challenge_con_modo_y_token_correctos():
    original = config.meta_verify_token
    config.meta_verify_token = "el-token-correcto"
    try:
        assert verificar_challenge("subscribe", "el-token-correcto") is True
    finally:
        config.meta_verify_token = original


def test_verificar_challenge_con_token_incorrecto():
    original = config.meta_verify_token
    config.meta_verify_token = "el-token-correcto"
    try:
        assert verificar_challenge("subscribe", "otro-token") is False
    finally:
        config.meta_verify_token = original


def test_verificar_challenge_con_modo_distinto_de_subscribe():
    original = config.meta_verify_token
    config.meta_verify_token = "el-token-correcto"
    try:
        assert verificar_challenge("unsubscribe", "el-token-correcto") is False
    finally:
        config.meta_verify_token = original
