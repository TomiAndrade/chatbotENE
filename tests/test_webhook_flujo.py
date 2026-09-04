"""Flujo completo del webhook con TestClient y webhooks firmados (ver
spec-etapa1.md, criterio de aceptación 1-6, y spec-meta-cloud-api.md,
sección 6)."""

import json

from app.db import SessionLocal
from app.models import Conversacion, Mensaje, RolMensaje
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import firmar_meta, payload_meta_texto

SECRETO = "test-app-secret"


def _post_mensaje(client, wa_message_id: str, texto: str, telefono: str = TELEFONO_DE_PRUEBA):
    payload = payload_meta_texto(wa_message_id, telefono, texto)
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)},
    )


def test_mensaje_entrante_se_guarda_y_recibe_respuesta(client, meta_enviados):
    respuesta = _post_mensaje(client, "wamid.1", "hola")

    assert respuesta.status_code == 200

    db = SessionLocal()
    conversacion = db.query(Conversacion).filter_by(canal="whatsapp", identificador_externo=TELEFONO_DE_PRUEBA).one()
    mensajes_guardados = db.query(Mensaje).filter_by(conversacion_id=conversacion.id).order_by(Mensaje.id).all()
    db.close()

    assert [m.rol for m in mensajes_guardados] == [RolMensaje.USUARIO, RolMensaje.BOT]
    assert mensajes_guardados[0].contenido == "hola"
    assert len(meta_enviados) == 1
    assert meta_enviados[0][0] == TELEFONO_DE_PRUEBA


def test_modo_humano_corta_la_respuesta(client, meta_enviados, db):
    _post_mensaje(client, "wamid.2", "primer mensaje")

    conversacion = db.query(Conversacion).filter_by(canal="whatsapp", identificador_externo=TELEFONO_DE_PRUEBA).one()
    conversacion.modo_humano = True
    db.commit()
    meta_enviados.clear()

    _post_mensaje(client, "wamid.3", "segundo mensaje, ya en modo humano")

    assert meta_enviados == []


def test_reenviar_el_mismo_webhook_no_duplica(client, meta_enviados):
    _post_mensaje(client, "wamid.duplicado", "hola de nuevo")
    _post_mensaje(client, "wamid.duplicado", "hola de nuevo")

    db = SessionLocal()
    conversacion = db.query(Conversacion).filter_by(canal="whatsapp", identificador_externo=TELEFONO_DE_PRUEBA).one()
    mensajes_usuario = (
        db.query(Mensaje)
        .filter_by(conversacion_id=conversacion.id, rol=RolMensaje.USUARIO)
        .all()
    )
    db.close()

    assert len(mensajes_usuario) == 1
    # Solo se respondió una vez: la segunda entrega se descartó por duplicada.
    assert len(meta_enviados) == 1


def test_health_devuelve_200(client):
    assert client.get("/health").status_code == 200
