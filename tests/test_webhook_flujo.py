"""Flujo completo del webhook con TestClient y webhooks firmados (ver
spec-etapa1.md, criterio de aceptación 1-6)."""

import json

from app.db import SessionLocal
from app.models import Conversacion, Mensaje, RolMensaje
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


def test_mensaje_entrante_se_guarda_y_recibe_respuesta(client, kapso_enviados):
    respuesta = _post_mensaje(client, "wamid.1", "hola")

    assert respuesta.status_code == 200

    db = SessionLocal()
    conversacion = db.query(Conversacion).filter_by(telefono=TELEFONO_DE_PRUEBA).one()
    mensajes_guardados = db.query(Mensaje).filter_by(conversacion_id=conversacion.id).order_by(Mensaje.id).all()
    db.close()

    assert [m.rol for m in mensajes_guardados] == [RolMensaje.USUARIO, RolMensaje.BOT]
    assert mensajes_guardados[0].contenido == "hola"
    assert len(kapso_enviados) == 1
    assert kapso_enviados[0][0] == TELEFONO_DE_PRUEBA


def test_modo_humano_corta_la_respuesta(client, kapso_enviados, db):
    _post_mensaje(client, "wamid.2", "primer mensaje")

    conversacion = db.query(Conversacion).filter_by(telefono=TELEFONO_DE_PRUEBA).one()
    conversacion.modo_humano = True
    db.commit()
    kapso_enviados.clear()

    _post_mensaje(client, "wamid.3", "segundo mensaje, ya en modo humano")

    assert kapso_enviados == []


def test_reenviar_el_mismo_webhook_no_duplica(client, kapso_enviados):
    _post_mensaje(client, "wamid.duplicado", "hola de nuevo")
    _post_mensaje(client, "wamid.duplicado", "hola de nuevo")

    db = SessionLocal()
    conversacion = db.query(Conversacion).filter_by(telefono=TELEFONO_DE_PRUEBA).one()
    mensajes_usuario = (
        db.query(Mensaje)
        .filter_by(conversacion_id=conversacion.id, rol=RolMensaje.USUARIO)
        .all()
    )
    db.close()

    assert len(mensajes_usuario) == 1
    # Solo se respondió una vez: la segunda entrega se descartó por duplicada.
    assert len(kapso_enviados) == 1


def test_health_devuelve_200(client):
    assert client.get("/health").status_code == 200
