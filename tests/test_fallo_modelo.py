"""Manejo de errores del modelo (ver spec-etapa2.md, test 5: que escale y
avise en vez de quedarse mudo, sea porque la llamada falla o porque vuelve
vacía sin escalar)."""

import json

from app import main as main_mod
from app.db import SessionLocal
from app.mensajes import MENSAJE_ERROR_GENERICO
from app.models import Conversacion
from app.respuesta import RespuestaGenerada
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import firmar, payload_mensaje_texto

SECRETO = "test-webhook-secret"


def _post_mensaje(client, wa_message_id: str, texto: str, telefono: str = TELEFONO_DE_PRUEBA):
    payload = payload_mensaje_texto(wa_message_id, telefono, texto)
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook",
        content=cuerpo,
        headers={
            "X-Webhook-Signature": firmar(cuerpo, SECRETO),
            "X-Webhook-Event": "whatsapp.message.received",
        },
    )


def test_si_la_llamada_al_modelo_falla_se_disculpa_y_escala(client, kapso_enviados, monkeypatch):
    def _reventar(historial, mensaje_nuevo):
        raise TimeoutError("el modelo tardó demasiado")

    monkeypatch.setattr(main_mod, "generar_respuesta", _reventar)

    _post_mensaje(client, "wamid.error1", "hola")

    db = SessionLocal()
    conversacion = db.query(Conversacion).filter_by(telefono=TELEFONO_DE_PRUEBA).one()
    db.close()

    assert conversacion.modo_humano is True
    assert len(kapso_enviados) == 2
    assert kapso_enviados[0][1] == MENSAJE_ERROR_GENERICO


def test_respuesta_vacia_sin_escalar_se_trata_como_error(client, kapso_enviados, monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=False, resumen=None),
    )

    _post_mensaje(client, "wamid.error2", "hola")

    db = SessionLocal()
    conversacion = db.query(Conversacion).filter_by(telefono=TELEFONO_DE_PRUEBA).one()
    db.close()

    assert conversacion.modo_humano is True
    assert len(kapso_enviados) == 2
    assert kapso_enviados[0][1] == MENSAJE_ERROR_GENERICO
