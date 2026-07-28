"""Límite de mensajes por hora (ver spec-etapa2.md, test 6: el mensaje 31 en
una hora no llama al modelo). Se usa un límite chico para no mandar 31
mensajes reales en el test."""

import json

from app import main as main_mod
from app.config import config
from app.mensajes import MENSAJE_LIMITE_ALCANZADO
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import firmar, payload_mensaje_texto

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


def test_al_superar_el_limite_no_se_llama_al_modelo(client, kapso_enviados, monkeypatch):
    monkeypatch.setattr(config, "limite_mensajes_hora", 3)

    llamadas_al_modelo = []
    from app.respuesta import RespuestaGenerada

    def modelo_falso(historial, mensaje_nuevo):
        llamadas_al_modelo.append(mensaje_nuevo)
        return RespuestaGenerada(texto="ok", escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo_falso)

    for i in range(3):
        _post_mensaje(client, f"wamid.limite.{i}", f"mensaje {i}")
    assert len(llamadas_al_modelo) == 3

    # Mensaje 4: supera el límite (3). No se llama al modelo, y se avisa una vez.
    _post_mensaje(client, "wamid.limite.3", "mensaje 3")
    assert len(llamadas_al_modelo) == 3
    assert kapso_enviados[-1][1] == MENSAJE_LIMITE_ALCANZADO

    # Mensaje 5: sigue por encima del límite. Silencio total, ni el aviso se repite.
    cantidad_enviados_previa = len(kapso_enviados)
    _post_mensaje(client, "wamid.limite.4", "mensaje 4")
    assert len(llamadas_al_modelo) == 3
    assert len(kapso_enviados) == cantidad_enviados_previa
