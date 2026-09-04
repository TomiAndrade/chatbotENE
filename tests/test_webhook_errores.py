"""Qué nivel de log deja cada tipo de falla en POST /webhook, y que un bug
propio no se trague un mensaje en silencio (ver CLAUDE.md: "un bug propio se
traga un mensaje sin dejar rastro visible" era el problema concreto).

Dos niveles, a propósito:
- Payload mal formado o con una forma que no esperamos (JSON roto, un campo
  del tipo que no es): WARNING. Es un dato de entrada raro, no un bug.
- Cualquier otra excepción durante el procesamiento: ERROR con traceback
  completo — es sospechosa de ser un bug propio, y sin el traceback no hay
  forma de encontrarlo después (Meta ya recibió el 200 y no reintenta).

También cubre el mismo problema un escalón más abajo: una excepción dentro
de `procesar_mensaje_entrante`, que corre en background *después* de que el
webhook ya devolvió 200 (ver Starlette `Response.__call__`: manda la
respuesta y recién después espera las background tasks). Sin un `except`
ahí, esa excepción no pasa por `logger("bot")` — se pierde en el logger de
uvicorn, sin `identificador_externo` ni `wa_message_id`.
"""

import json

from app import main as main_mod
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import firmar_meta, payload_meta_mensajes, payload_meta_texto

SECRETO = "test-app-secret"


def _post(client, cuerpo: bytes):
    return client.post(
        "/webhook",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)},
    )


def test_json_roto_devuelve_200_y_loguea_warning(client, meta_enviados, caplog):
    cuerpo = b"esto no es json"

    with caplog.at_level("WARNING", logger="bot"):
        respuesta = _post(client, cuerpo)

    assert respuesta.status_code == 200
    assert meta_enviados == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("no es JSON válido" in r.message for r in warnings)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_entry_con_forma_inesperada_devuelve_200_y_loguea_warning_no_error(client, meta_enviados, caplog):
    """`entry` viene string en vez de lista: al iterar, cada carácter no
    tiene `.get`, así que el `.get("changes", [])` explota con AttributeError
    — justo el tipo de error que la sección "estructura inesperada" tiene
    que atrapar como WARNING, no como ERROR."""
    payload = {"object": "whatsapp_business_account", "entry": "no-es-una-lista"}
    cuerpo = json.dumps(payload).encode("utf-8")

    with caplog.at_level("WARNING", logger="bot"):
        respuesta = _post(client, cuerpo)

    assert respuesta.status_code == 200
    assert meta_enviados == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("estructura inesperada" in r.message for r in warnings)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_bug_propio_en_procesar_cambio_devuelve_200_y_loguea_error_con_traceback(
    client, meta_enviados, monkeypatch, caplog
):
    """Un bug de verdad (no un problema del payload) tiene que quedar
    registrado a nivel ERROR, con traceback, para poder encontrarlo — no
    confundirse con un WARNING de payload raro."""

    def _procesar_cambio_roto(value, background_tasks):
        raise RuntimeError("bug simulado, no tiene nada que ver con el payload")

    monkeypatch.setattr(main_mod, "_procesar_cambio", _procesar_cambio_roto)

    payload = payload_meta_texto("wamid.bug1", TELEFONO_DE_PRUEBA, "hola")
    cuerpo = json.dumps(payload).encode("utf-8")

    with caplog.at_level("WARNING", logger="bot"):
        respuesta = _post(client, cuerpo)

    assert respuesta.status_code == 200
    errores = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errores) == 1
    assert "Error inesperado procesando el webhook" in errores[0].message
    assert errores[0].exc_info is not None, "tiene que llevar el traceback completo"


def test_bug_dentro_de_la_background_task_no_se_pierde_en_silencio(monkeypatch, caplog):
    """El caso que motivó este archivo: la excepción no pasa por el webhook
    (que ya devolvió 200) sino por `procesar_mensaje_entrante` corriendo en
    background. Tiene que quedar en el log de la app igual, con el
    identificador y el wa_message_id, y la función no tiene que explotar
    hacia quien la llamó (el background-task runner de Starlette)."""

    def _responder_roto(db, conversacion, mensaje_usuario):
        raise RuntimeError("bug simulado dentro de responder()")

    monkeypatch.setattr(main_mod, "responder", _responder_roto)

    with caplog.at_level("ERROR", logger="bot"):
        main_mod.procesar_mensaje_entrante(TELEFONO_DE_PRUEBA, "wamid.bug-background", "hola")

    errores = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errores) == 1
    assert "Error inesperado procesando el mensaje" in errores[0].message
    assert "wamid.bug-background" in errores[0].message
    assert errores[0].exc_info is not None
