"""Escalamiento a humano (ver spec-etapa2.md, test 3 y criterio de
aceptación 3-4): si el modelo llama a la herramienta, se prende modo_humano,
se guarda el resumen, se manda el aviso, y el bot no vuelve a responder.

Los asserts sobre el aviso van contra las constantes de `app.mensajes`, no
contra el resultado de llamar a `mensaje_escalamiento()`: comparar contra la
propia función haría que el test pasara igual si esa función devolviera
cualquier cosa.
"""

import json
from datetime import datetime, timezone

from app import main as main_mod
from app.db import SessionLocal
from app.mensajes import (
    MENSAJE_ESCALAMIENTO_EN_HORARIO,
    MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO,
)
from app.models import Conversacion
from app.respuesta import RespuestaGenerada
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import AVISOS_DE_ESCALAMIENTO, RelojFijo, firmar_meta, payload_meta_texto

SECRETO = "test-app-secret"


def _post_mensaje(client, wa_message_id: str, texto: str, telefono: str = TELEFONO_DE_PRUEBA):
    payload = payload_meta_texto(wa_message_id, telefono, texto)
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)},
    )


def _conversacion_de_prueba() -> Conversacion:
    db = SessionLocal()
    try:
        return db.query(Conversacion).filter_by(canal="whatsapp", identificador_externo=TELEFONO_DE_PRUEBA).one()
    finally:
        db.close()


def test_escalar_marca_modo_humano_guarda_resumen_y_avisa(escalamiento_activo, client, meta_enviados, monkeypatch):
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

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is True
    assert conversacion.resumen_escalamiento == "quiere alquilar una sala para un evento"
    assert conversacion.escalada_en is not None

    # Primero el texto del modelo, después el aviso de escalamiento.
    textos = [texto for _, texto in meta_enviados]
    assert len(textos) == 2
    assert textos[0] == "Dale, ya te paso con alguien del equipo."
    assert textos[1] in AVISOS_DE_ESCALAMIENTO


def test_despues_de_escalar_el_bot_no_responde_mas(escalamiento_activo, client, meta_enviados, monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=True, resumen="reclamo"),
    )
    _post_mensaje(client, "wamid.escala2", "tengo un reclamo")
    cantidad_tras_escalar = len(meta_enviados)

    _post_mensaje(client, "wamid.despues", "otra consulta despues de escalar")

    assert len(meta_enviados) == cantidad_tras_escalar


def test_escalar_sin_texto_solo_manda_el_aviso(escalamiento_activo, client, meta_enviados, monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=True, resumen="algo puntual"),
    )

    _post_mensaje(client, "wamid.solo-escala", "consulta rara")

    textos = [texto for _, texto in meta_enviados]
    assert len(textos) == 1
    assert textos[0] in AVISOS_DE_ESCALAMIENTO


# --- Rama de horario del camino real ----------------------------------------
#
# No alcanza con probar `mensaje_escalamiento()` aislada (eso lo hace
# test_mensajes_horario.py): hay que verificar que `escalar_a_humano` convierta
# a la zona del polo. Por eso los dos instantes de abajo están elegidos para
# que UTC y Buenos Aires (UTC-3) no coincidan en qué rama corresponde: si el
# código hiciera `datetime.now()` en vez de `datetime.now(config.timezone)`,
# con el server en UTC elegiría el aviso equivocado y estos tests fallarían.


def _escalar_con_reloj(client, meta_enviados, monkeypatch, instante_utc, wa_message_id):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=True, resumen="lo que sea"),
    )
    monkeypatch.setattr(main_mod, "datetime", RelojFijo(instante_utc))

    _post_mensaje(client, wa_message_id, "algo que escala")

    textos = [texto for _, texto in meta_enviados]
    assert len(textos) == 1
    return textos[0]


def test_aviso_en_horario_usa_la_hora_del_polo_no_la_del_server(escalamiento_activo, client, meta_enviados, monkeypatch):
    # Martes 19:00 UTC = martes 16:00 en Buenos Aires → dentro del horario de
    # atención. Leído como UTC serían las 19, que está fuera.
    instante = datetime(2026, 7, 28, 19, 0, tzinfo=timezone.utc)

    aviso = _escalar_con_reloj(client, meta_enviados, monkeypatch, instante, "wamid.horario-dentro")

    assert aviso == MENSAJE_ESCALAMIENTO_EN_HORARIO


def test_aviso_fuera_de_horario_usa_la_hora_del_polo_no_la_del_server(escalamiento_activo, client, meta_enviados, monkeypatch):
    # Martes 10:00 UTC = martes 07:00 en Buenos Aires → todavía no abrió.
    # Leído como UTC serían las 10, que sí está dentro del horario.
    # (Era 11:00 UTC = 08:00 BA cuando el horario era 9-17; con 8-18 las 8 ya
    # es hora de apertura y el instante dejó de probar lo que dice probar.)
    instante = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)

    aviso = _escalar_con_reloj(client, meta_enviados, monkeypatch, instante, "wamid.horario-fuera")

    assert aviso == MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO


def test_aviso_de_fin_de_semana_avisa_el_horario_de_atencion(escalamiento_activo, client, meta_enviados, monkeypatch):
    # Sábado 2026-08-01, 22:00 en Buenos Aires (01:00 UTC del domingo).
    instante = datetime(2026, 8, 2, 1, 0, tzinfo=timezone.utc)

    aviso = _escalar_con_reloj(client, meta_enviados, monkeypatch, instante, "wamid.horario-finde")

    assert aviso == MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO


def test_si_falla_el_envio_del_aviso_el_escalamiento_queda_igual(escalamiento_activo, client, meta_enviados, monkeypatch, caplog):
    """El commit de modo_humano va antes que el envío del aviso, así que si
    Meta está caído la conversación queda escalada igual. Lo que no puede
    pasar es que eso se pierda: tiene que quedar el WARNING del escalamiento
    y un error que diga que lo que no salió fue el aviso."""
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=True, resumen="un reclamo"),
    )

    def meta_caido(telefono, texto):
        raise RuntimeError("Meta no responde")

    monkeypatch.setattr(main_mod.meta_client, "enviar_mensaje_texto", meta_caido)

    with caplog.at_level("WARNING", logger="bot"):
        _post_mensaje(client, "wamid.meta-caido", "tengo un reclamo")

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is True
    assert conversacion.resumen_escalamiento == "un reclamo"
    assert meta_enviados == []

    assert "ESCALADO A HUMANO" in caplog.text
    assert "NO se pudo enviar el aviso de escalamiento" in caplog.text


# --- Carrera entre una respuesta lenta y un escalamiento --------------------
#
# Hasta la entrega 1.2 (agrupamiento de mensajes, ver
# specs/spec-agrupamiento-mensajes.md) acá había dos tests que hacían
# competir dos mensajes concurrentes de la MISMA conversación esperando que
# cada uno disparara su propia llamada a generar_respuesta. Con el
# agrupamiento esa carrera ya no existe por construcción: el segundo mensaje
# no logra tomar la reserva de generación mientras la tiene el primero (ver
# `agrupar_y_responder` en app/main.py) — se suma al lote en curso o queda
# pendiente para el siguiente, nunca dispara una segunda llamada al modelo en
# paralelo. La propiedad de seguridad que esos tests protegían ("el bot no
# escribe encima de un humano") sigue vigente, pero bajo un mecanismo
# distinto: se prueba en tests/test_agrupamiento.py
# (test_no_se_genera_respuesta_si_modo_humano_se_activa_durante_la_espera).
# El caso simétrico donde la respuesta YA se generó y modo_humano se activó
# mientras tanto lo sigue cubriendo, sin cambios,
# tests/test_pausa_humana.py::test_secretaria_responde_mientras_el_modelo_genera_el_bot_no_escribe_encima
# (esa carrera es una sola llamada a procesar_mensaje_entrante, no dos, así
# que la reserva de agrupamiento no la afecta).
