"""Matriz de verificación de firma del webhook (ver spec-etapa1.md y
CLAUDE.md, checklist de deploy: sin secreto y DEBUG=false el webhook debe
quedar cerrado)."""

import hashlib
import hmac

from app.config import config
from app.kapso import verificar_firma_webhook

CUERPO = b'{"message": {"id": "wamid.1"}}'
SECRETO = "un-secreto-cualquiera"


def _firma_correcta(cuerpo: bytes = CUERPO, secreto: str = SECRETO) -> str:
    return hmac.new(secreto.encode("utf-8"), cuerpo, hashlib.sha256).hexdigest()


def test_firma_valida_pasa(monkeypatch):
    monkeypatch.setattr(config, "kapso_webhook_secret", SECRETO)
    monkeypatch.setattr(config, "debug", False)

    assert verificar_firma_webhook(CUERPO, _firma_correcta()) is True


def test_firma_invalida_se_rechaza(monkeypatch):
    monkeypatch.setattr(config, "kapso_webhook_secret", SECRETO)
    monkeypatch.setattr(config, "debug", False)

    assert verificar_firma_webhook(CUERPO, "0" * 64) is False


def test_firma_ausente_se_rechaza(monkeypatch):
    monkeypatch.setattr(config, "kapso_webhook_secret", SECRETO)
    monkeypatch.setattr(config, "debug", False)

    assert verificar_firma_webhook(CUERPO, None) is False


def test_cuerpo_alterado_invalida_la_firma(monkeypatch):
    monkeypatch.setattr(config, "kapso_webhook_secret", SECRETO)
    monkeypatch.setattr(config, "debug", False)

    firma = _firma_correcta()
    assert verificar_firma_webhook(CUERPO + b"algo mas", firma) is False


def test_sin_secreto_y_debug_false_se_rechaza_todo(monkeypatch):
    """El caso que importa para el checklist de deploy: sin secreto
    configurado y DEBUG=false, el webhook tiene que quedar cerrado."""
    monkeypatch.setattr(config, "kapso_webhook_secret", "")
    monkeypatch.setattr(config, "debug", False)

    assert verificar_firma_webhook(CUERPO, _firma_correcta()) is False
    assert verificar_firma_webhook(CUERPO, None) is False


def test_sin_secreto_y_debug_true_deja_pasar(monkeypatch):
    """Permitido solo para poder levantar el proyecto en desarrollo antes de
    tener el secreto del sandbox de Kapso."""
    monkeypatch.setattr(config, "kapso_webhook_secret", "")
    monkeypatch.setattr(config, "debug", True)

    assert verificar_firma_webhook(CUERPO, None) is True
