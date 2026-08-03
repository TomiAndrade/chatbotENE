"""Camino de ErrorTransitorioProveedor en responder() (ver spec-etapa2.md,
"Manejo de errores"): en desarrollo (DEBUG=true) no escala, solo pide
reintentar; en producción (DEBUG=false) escala como cualquier otro fallo."""

import json

from app import main as main_mod
from app.config import config
from app.db import SessionLocal
from app.mensajes import MENSAJE_ERROR_GENERICO, MENSAJE_ERROR_TRANSITORIO
from app.models import Conversacion
from app.respuesta import ErrorTransitorioProveedor
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import AVISOS_DE_ESCALAMIENTO, firmar, payload_mensaje_texto

SECRETO = "test-webhook-secret"


def _post_mensaje(client, wa_message_id: str, texto: str):
    payload = payload_mensaje_texto(wa_message_id, TELEFONO_DE_PRUEBA, texto)
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook",
        content=cuerpo,
        headers={
            "X-Webhook-Signature": firmar(cuerpo, SECRETO),
            "X-Webhook-Event": "whatsapp.message.received",
        },
    )


def _reventar_transitorio(historial, mensaje_nuevo):
    raise ErrorTransitorioProveedor("Proveedor devolvió error en el body: saturado")


def test_en_desarrollo_no_escala_y_pide_reintentar(client, kapso_enviados, monkeypatch):
    monkeypatch.setattr(config, "debug", True)
    monkeypatch.setattr(main_mod, "generar_respuesta", _reventar_transitorio)

    _post_mensaje(client, "wamid.transitorio-dev", "hola")

    db = SessionLocal()
    conversacion = db.query(Conversacion).filter_by(telefono=TELEFONO_DE_PRUEBA).one()
    db.close()

    assert conversacion.modo_humano is False
    textos = [texto for _, texto in kapso_enviados]
    assert len(textos) == 1
    assert textos[0] == MENSAJE_ERROR_TRANSITORIO
    # Y nada de avisos de escalamiento: en desarrollo esto no escala.
    assert not AVISOS_DE_ESCALAMIENTO.intersection(textos)


def test_en_produccion_escala_como_cualquier_otro_fallo(client, kapso_enviados, monkeypatch):
    monkeypatch.setattr(config, "debug", False)
    monkeypatch.setattr(main_mod, "generar_respuesta", _reventar_transitorio)

    _post_mensaje(client, "wamid.transitorio-prod", "hola")

    db = SessionLocal()
    conversacion = db.query(Conversacion).filter_by(telefono=TELEFONO_DE_PRUEBA).one()
    db.close()

    assert conversacion.modo_humano is True
    assert conversacion.resumen_escalamiento == "Error automático: error transitorio del proveedor de IA."
    textos = [texto for _, texto in kapso_enviados]
    assert len(textos) == 2
    assert textos[0] == MENSAJE_ERROR_GENERICO
    assert textos[1] in AVISOS_DE_ESCALAMIENTO
