"""Escalamiento a humano (ver spec-etapa2.md, test 3 y criterio de
aceptación 3-4): si el modelo llama a la herramienta, se prende modo_humano,
se guarda el resumen, se manda el aviso, y el bot no vuelve a responder."""

import json

from app import main as main_mod
from app.db import SessionLocal
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


def test_escalar_marca_modo_humano_guarda_resumen_y_avisa(client, kapso_enviados, monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(
            texto="Dale, ya te paso con alguien del equipo.",
            escalar=True,
            resumen="quiere alquilar una sala para un evento",
        ),
    )

    _post_mensaje(client, "wamid.escala", "quiero alquilar una sala")

    db = SessionLocal()
    conversacion = db.query(Conversacion).filter_by(telefono=TELEFONO_DE_PRUEBA).one()
    db.close()

    assert conversacion.modo_humano is True
    assert conversacion.resumen_escalamiento == "quiere alquilar una sala para un evento"
    assert conversacion.escalada_en is not None

    # Primero el texto del modelo, después el aviso de escalamiento.
    assert len(kapso_enviados) == 2
    assert kapso_enviados[0][1] == "Dale, ya te paso con alguien del equipo."


def test_despues_de_escalar_el_bot_no_responde_mas(client, kapso_enviados, monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=True, resumen="reclamo"),
    )
    _post_mensaje(client, "wamid.escala2", "tengo un reclamo")
    cantidad_tras_escalar = len(kapso_enviados)

    _post_mensaje(client, "wamid.despues", "otra consulta despues de escalar")

    assert len(kapso_enviados) == cantidad_tras_escalar


def test_escalar_sin_texto_solo_manda_el_aviso(client, kapso_enviados, monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=True, resumen="algo puntual"),
    )

    _post_mensaje(client, "wamid.solo-escala", "consulta rara")

    assert len(kapso_enviados) == 1
