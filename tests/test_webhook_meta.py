"""Verificación de la sección 6 de spec-meta-cloud-api.md: los ocho puntos
del checklist específicos de la migración a la Cloud API de Meta (challenge
GET, firma X-Hub-Signature-256, statuses[] descartado, varios mensajes en un
mismo entry, tipo no soportado, dedup, y el número argentino verbatim de la
sección 3). El punto 8 ("la suite completa sigue pasando") no es un test
propio: es correr `pytest` entero.
"""

import json

from app.db import SessionLocal
from app.models import Conversacion, Mensaje
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import firmar_meta, mensaje_meta_texto, payload_meta_mensajes, payload_meta_statuses, payload_meta_texto

SECRETO = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


def _post(client, payload: dict):
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)},
    )


# --- 1-2: challenge GET de verificación --------------------------------------


def test_get_webhook_con_token_correcto_devuelve_el_challenge_en_texto_plano(client):
    respuesta = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444"},
    )

    assert respuesta.status_code == 200
    assert respuesta.text == "1158201444"
    assert respuesta.headers["content-type"].startswith("text/plain")


def test_get_webhook_con_token_incorrecto_devuelve_403(client):
    respuesta = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "token-equivocado", "hub.challenge": "123"},
    )

    assert respuesta.status_code == 403


def test_get_webhook_con_modo_distinto_de_subscribe_devuelve_403(client):
    respuesta = client.get(
        "/webhook",
        params={"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "123"},
    )

    assert respuesta.status_code == 403


# --- 3: firma inválida --------------------------------------------------------


def test_post_webhook_con_firma_invalida_devuelve_401(client):
    payload = payload_meta_texto("wamid.firma-mala", TELEFONO_DE_PRUEBA, "hola")
    cuerpo = json.dumps(payload).encode("utf-8")

    respuesta = client.post(
        "/webhook",
        content=cuerpo,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )

    assert respuesta.status_code == 401


def test_post_webhook_sin_firma_devuelve_401(client):
    payload = payload_meta_texto("wamid.sin-firma", TELEFONO_DE_PRUEBA, "hola")

    respuesta = client.post("/webhook", content=json.dumps(payload).encode("utf-8"))

    assert respuesta.status_code == 401


# --- 4: statuses[] se descarta, siempre 200 -----------------------------------


def test_statuses_no_encola_nada_y_devuelve_200(client, meta_enviados):
    payload = payload_meta_statuses("wamid.status1", TELEFONO_DE_PRUEBA)

    respuesta = _post(client, payload)

    assert respuesta.status_code == 200
    assert meta_enviados == []
    db = SessionLocal()
    cantidad = db.query(Mensaje).count()
    db.close()
    assert cantidad == 0


# --- 5: dos mensajes en el mismo entry encolan dos tareas ---------------------


def test_dos_mensajes_en_el_mismo_entry_encolan_dos_tareas(client, meta_enviados):
    mensajes = [
        mensaje_meta_texto("wamid.multi1", TELEFONO_DE_PRUEBA, "primero"),
        mensaje_meta_texto("wamid.multi2", "5492996009999", "segundo, de otra persona"),
    ]
    payload = payload_meta_mensajes(mensajes)

    respuesta = _post(client, payload)

    assert respuesta.status_code == 200
    assert len(meta_enviados) == 2
    db = SessionLocal()
    guardados = db.query(Mensaje).filter(Mensaje.wa_message_id.in_(["wamid.multi1", "wamid.multi2"])).all()
    db.close()
    assert len(guardados) == 2


# --- 6: tipo no soportado guarda el placeholder y no rompe --------------------


def test_tipo_no_soportado_guarda_placeholder_y_no_rompe(client, meta_enviados):
    mensaje = {
        "from": TELEFONO_DE_PRUEBA,
        "id": "wamid.imagen1",
        "timestamp": "1735689600",
        "type": "image",
        "image": {"id": "media123", "mime_type": "image/jpeg"},
    }
    payload = payload_meta_mensajes([mensaje])

    respuesta = _post(client, payload)

    assert respuesta.status_code == 200
    db = SessionLocal()
    guardado = db.query(Mensaje).filter_by(wa_message_id="wamid.imagen1").one()
    db.close()
    assert guardado.contenido == "[mensaje de tipo 'image' no soportado en esta etapa]"


# --- 7: el mismo wa_message_id entregado dos veces se procesa una sola vez --


def test_mismo_wa_message_id_entregado_dos_veces_se_procesa_una_sola_vez(client, meta_enviados):
    payload = payload_meta_texto("wamid.dup-meta", TELEFONO_DE_PRUEBA, "hola de nuevo")

    _post(client, payload)
    _post(client, payload)

    db = SessionLocal()
    guardados = db.query(Mensaje).filter_by(wa_message_id="wamid.dup-meta").all()
    db.close()
    assert len(guardados) == 1
    assert len(meta_enviados) == 1


# --- Payload incompleto: 200 igual, nunca error -------------------------------


def test_mensaje_sin_from_se_descarta_y_devuelve_200(client, meta_enviados):
    mensaje = {"id": "wamid.sin-from", "type": "text", "text": {"body": "hola"}}
    payload = payload_meta_mensajes([mensaje])

    respuesta = _post(client, payload)

    assert respuesta.status_code == 200
    assert meta_enviados == []


def test_json_roto_devuelve_200(client, meta_enviados):
    cuerpo = b"esto no es json"

    respuesta = client.post(
        "/webhook",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)},
    )

    assert respuesta.status_code == 200


# --- Sección 3: número argentino, verbatim ------------------------------------


def test_numero_argentino_se_guarda_y_responde_verbatim(client, meta_enviados):
    """El "9" puede venir o no en `from`: no hay que tocarlo, ni al guardar
    la conversación ni al responder."""
    numero_con_9 = "5492996001234"
    payload = payload_meta_texto("wamid.arg1", numero_con_9, "hola desde argentina")

    _post(client, payload)

    db = SessionLocal()
    conversacion = db.query(Conversacion).filter_by(canal="whatsapp", identificador_externo=numero_con_9).one()
    db.close()
    assert conversacion.identificador_externo == numero_con_9
    assert meta_enviados[0][0] == numero_con_9
