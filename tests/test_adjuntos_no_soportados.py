"""Entrega 1.1 de roadmap-bot-crm.md (ver specs/spec-adjuntos-no-soportados.md):
un mensaje cuyo `type` real del webhook no es `text` no llama al modelo ni
escala por sí mismo, solo responde con un texto fijo pidiendo la consulta por
escrito. Respeta exactamente los mismos chequeos que un mensaje de texto:
dedup, modo_humano y límite por hora — esta suite los reejercita con adjuntos,
no los reimplementa.
"""

import json

from app import envio as envio_mod
from app import main as main_mod
from app.config import config
from app.db import SessionLocal
from app.mensajes import (
    MENSAJE_ADJUNTO_AUDIO,
    MENSAJE_ADJUNTO_DOCUMENTO,
    MENSAJE_ADJUNTO_GENERICO,
    MENSAJE_ADJUNTO_IMAGEN,
    MENSAJE_LIMITE_ALCANZADO,
)
from app.models import Mensaje, RolMensaje
from app.respuesta import RespuestaGenerada
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import firmar_meta, mensaje_meta_adjunto, mensaje_meta_texto, payload_meta_adjunto, payload_meta_mensajes, payload_meta_texto

SECRETO = "test-app-secret"


def _post(client, payload: dict):
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)},
    )


def _espiar_modelo(monkeypatch):
    """Reemplaza generar_respuesta por un espía que registra cada llamada.
    Los tests de esta suite tienen que poder afirmar que, para un adjunto,
    la lista queda vacía."""
    llamadas = []
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: (
            llamadas.append(mensaje_nuevo)
            or RespuestaGenerada(texto="no debería usarse", escalar=False, resumen=None)
        ),
    )
    return llamadas


# --- Texto fijo por tipo, sin llamar al modelo -------------------------------


def test_imagen_responde_texto_fijo_y_no_llama_al_modelo(client, meta_enviados, monkeypatch):
    llamadas = _espiar_modelo(monkeypatch)

    respuesta = _post(client, payload_meta_adjunto("wamid.img", TELEFONO_DE_PRUEBA, "image"))

    assert respuesta.status_code == 200
    assert llamadas == []
    assert meta_enviados == [(TELEFONO_DE_PRUEBA, MENSAJE_ADJUNTO_IMAGEN)]


def test_documento_responde_texto_fijo_y_no_llama_al_modelo(client, meta_enviados, monkeypatch):
    llamadas = _espiar_modelo(monkeypatch)

    respuesta = _post(client, payload_meta_adjunto("wamid.doc", TELEFONO_DE_PRUEBA, "document"))

    assert respuesta.status_code == 200
    assert llamadas == []
    assert meta_enviados == [(TELEFONO_DE_PRUEBA, MENSAJE_ADJUNTO_DOCUMENTO)]


def test_audio_responde_texto_fijo_y_no_llama_al_modelo(client, meta_enviados, monkeypatch):
    llamadas = _espiar_modelo(monkeypatch)

    respuesta = _post(client, payload_meta_adjunto("wamid.audio", TELEFONO_DE_PRUEBA, "audio"))

    assert respuesta.status_code == 200
    assert llamadas == []
    assert meta_enviados == [(TELEFONO_DE_PRUEBA, MENSAJE_ADJUNTO_AUDIO)]


def test_tipo_desconocido_responde_texto_generico_y_no_llama_al_modelo(client, meta_enviados, monkeypatch):
    """`sticker` no tiene texto propio: cae al genérico. Cualquier tipo que
    la Cloud API de Meta agregue en el futuro (video, ubicación, contacto,
    interactivo, botón...) tiene que caer acá también, sin que haga falta
    tocar código — por eso el test usa uno que hoy no está mapeado."""
    llamadas = _espiar_modelo(monkeypatch)

    respuesta = _post(client, payload_meta_adjunto("wamid.sticker", TELEFONO_DE_PRUEBA, "sticker"))

    assert respuesta.status_code == 200
    assert llamadas == []
    assert meta_enviados == [(TELEFONO_DE_PRUEBA, MENSAJE_ADJUNTO_GENERICO)]


def test_tipo_ausente_responde_texto_generico_y_no_llama_al_modelo(client, meta_enviados, monkeypatch):
    """Un mensaje sin `type` (payload raro, pero no imposible) tiene que
    tratarse como adjunto no soportado, no como texto — nunca se asume texto
    por default ante datos incompletos."""
    llamadas = _espiar_modelo(monkeypatch)

    respuesta = _post(client, payload_meta_adjunto("wamid.sin-tipo", TELEFONO_DE_PRUEBA, None))

    assert respuesta.status_code == 200
    assert llamadas == []
    assert meta_enviados == [(TELEFONO_DE_PRUEBA, MENSAJE_ADJUNTO_GENERICO)]


# --- No confundir un texto que imita el marcador con un adjunto real --------


def test_texto_que_imita_el_marcador_sigue_yendo_por_el_camino_de_texto(client, meta_enviados, monkeypatch):
    """Si alguien escribe a mano el mismo texto que usa el placeholder de un
    adjunto, `type` sigue siendo "text": tiene que llamar al modelo como
    cualquier mensaje de texto, no disparar el camino de adjunto. La decisión
    se toma sobre la metadata del webhook, nunca comparando el contenido."""
    llamadas = _espiar_modelo(monkeypatch)
    texto_marcador = "[mensaje de tipo 'image' no soportado en esta etapa]"

    respuesta = _post(client, payload_meta_texto("wamid.imita-marcador", TELEFONO_DE_PRUEBA, texto_marcador))

    assert respuesta.status_code == 200
    assert llamadas == [texto_marcador]
    assert meta_enviados == [(TELEFONO_DE_PRUEBA, "no debería usarse")]


# --- Guardado con wa_message_id, para dedup ----------------------------------


def test_adjunto_se_guarda_con_wa_message_id_y_se_procesa_una_sola_vez(client, meta_enviados, monkeypatch):
    _espiar_modelo(monkeypatch)
    payload = payload_meta_adjunto("wamid.dup-adjunto", TELEFONO_DE_PRUEBA, "image")

    _post(client, payload)
    _post(client, payload)

    db = SessionLocal()
    guardados = db.query(Mensaje).filter_by(wa_message_id="wamid.dup-adjunto").all()
    db.close()
    assert len(guardados) == 1
    assert guardados[0].rol == RolMensaje.USUARIO
    assert len(meta_enviados) == 1


# --- Respeta modo_humano, igual que un texto ---------------------------------


def test_adjunto_en_modo_humano_no_responde(client, meta_enviados, monkeypatch, db):
    _espiar_modelo(monkeypatch)

    # Primer mensaje (texto) crea la conversación.
    _post(client, payload_meta_texto("wamid.previo", TELEFONO_DE_PRUEBA, "hola"))
    from app.models import Conversacion

    conversacion = db.query(Conversacion).filter_by(canal="whatsapp", identificador_externo=TELEFONO_DE_PRUEBA).one()
    conversacion.modo_humano = True
    db.commit()
    meta_enviados.clear()

    respuesta = _post(client, payload_meta_adjunto("wamid.adjunto-en-pausa", TELEFONO_DE_PRUEBA, "image"))

    assert respuesta.status_code == 200
    assert meta_enviados == []
    db2 = SessionLocal()
    guardado = db2.query(Mensaje).filter_by(wa_message_id="wamid.adjunto-en-pausa").one()
    db2.close()
    assert guardado.rol == RolMensaje.USUARIO, "el mensaje entrante se guarda igual, solo no se responde"


# --- Respeta el límite de mensajes por hora, igual que un texto -------------


def test_adjunto_que_cruza_el_limite_dispara_el_aviso_de_limite_no_el_texto_de_adjunto(
    client, meta_enviados, monkeypatch
):
    monkeypatch.setattr(config, "limite_mensajes_hora", 2)
    _espiar_modelo(monkeypatch)

    _post(client, payload_meta_adjunto("wamid.lim1", TELEFONO_DE_PRUEBA, "image"))
    _post(client, payload_meta_adjunto("wamid.lim2", TELEFONO_DE_PRUEBA, "document"))
    assert [texto for _, texto in meta_enviados] == [MENSAJE_ADJUNTO_IMAGEN, MENSAJE_ADJUNTO_DOCUMENTO]

    # Tercer mensaje: cruza el límite (2). No responde con el texto de
    # adjunto, sino con el aviso de límite.
    _post(client, payload_meta_adjunto("wamid.lim3", TELEFONO_DE_PRUEBA, "audio"))
    assert meta_enviados[-1] == (TELEFONO_DE_PRUEBA, MENSAJE_LIMITE_ALCANZADO)

    # Cuarto: sigue por encima. Silencio total, ni el aviso se repite.
    cantidad_previa = len(meta_enviados)
    _post(client, payload_meta_adjunto("wamid.lim4", TELEFONO_DE_PRUEBA, "audio"))
    assert len(meta_enviados) == cantidad_previa


# --- Varios mensajes de tipos distintos en el mismo entry -------------------


def test_webhook_con_texto_y_adjunto_en_el_mismo_entry_cada_uno_responde_segun_su_tipo(
    client, meta_enviados, monkeypatch
):
    llamadas = _espiar_modelo(monkeypatch)
    otro_telefono = "5492996009999"
    mensajes = [
        mensaje_meta_texto("wamid.multi-texto", TELEFONO_DE_PRUEBA, "hola, quiero info"),
        mensaje_meta_adjunto("wamid.multi-imagen", otro_telefono, "image"),
    ]
    payload = payload_meta_mensajes(mensajes)

    respuesta = _post(client, payload)

    assert respuesta.status_code == 200
    assert llamadas == ["hola, quiero info"]
    enviados_por_telefono = dict(meta_enviados)
    assert enviados_por_telefono[TELEFONO_DE_PRUEBA] == "no debería usarse"
    assert enviados_por_telefono[otro_telefono] == MENSAJE_ADJUNTO_IMAGEN


# --- Error al enviar la respuesta fija: mismo tratamiento que cualquier envío


def test_si_falla_el_envio_de_la_respuesta_de_adjunto_no_rompe_ni_escala(client, meta_enviados, monkeypatch, caplog):
    """`enviar_y_guardar` ya maneja un fallo de Meta (log + no guarda el
    mensaje del bot) para cualquier envío. Este test confirma que el camino
    de adjunto no soportado no le agrega ningún manejo de error propio: ni
    excepción hacia arriba, ni escalamiento por el solo hecho de no poder
    mandar el texto fijo."""
    _espiar_modelo(monkeypatch)

    def meta_caido(telefono, texto):
        raise RuntimeError("Meta no responde")

    monkeypatch.setattr(envio_mod.meta_client, "enviar_mensaje_texto", meta_caido)

    with caplog.at_level("ERROR", logger="bot"):
        respuesta = _post(client, payload_meta_adjunto("wamid.envio-roto", TELEFONO_DE_PRUEBA, "image"))

    assert respuesta.status_code == 200
    assert meta_enviados == []
    assert "No se pudo enviar" in caplog.text

    db = SessionLocal()
    mensajes_guardados = db.query(Mensaje).filter(Mensaje.wa_message_id == "wamid.envio-roto").all()
    mensajes_bot = db.query(Mensaje).filter_by(rol=RolMensaje.BOT).all()
    conversacion = mensajes_guardados[0].conversacion
    db.close()
    assert len(mensajes_guardados) == 1, "el mensaje entrante se guardó igual"
    assert mensajes_bot == [], "no se guarda ninguna respuesta del bot si el envío falló"
    assert conversacion.modo_humano is False, "un fallo de envío no escala"
