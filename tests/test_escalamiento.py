"""Escalamiento a humano (ver spec-etapa2.md, test 3 y criterio de
aceptación 3-4): si el modelo llama a la herramienta, se prende modo_humano,
se guarda el resumen, se manda el aviso, y el bot no vuelve a responder.

Los asserts sobre el aviso van contra las constantes de `app.mensajes`, no
contra el resultado de llamar a `mensaje_escalamiento()`: comparar contra la
propia función haría que el test pasara igual si esa función devolviera
cualquier cosa.
"""

import json
import threading
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
from tests.helpers import AVISOS_DE_ESCALAMIENTO, RelojFijo, firmar, payload_mensaje_texto

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


def _conversacion_de_prueba() -> Conversacion:
    db = SessionLocal()
    try:
        return db.query(Conversacion).filter_by(canal="whatsapp", identificador_externo=TELEFONO_DE_PRUEBA).one()
    finally:
        db.close()


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

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is True
    assert conversacion.resumen_escalamiento == "quiere alquilar una sala para un evento"
    assert conversacion.escalada_en is not None

    # Primero el texto del modelo, después el aviso de escalamiento.
    textos = [texto for _, texto in kapso_enviados]
    assert len(textos) == 2
    assert textos[0] == "Dale, ya te paso con alguien del equipo."
    assert textos[1] in AVISOS_DE_ESCALAMIENTO


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

    textos = [texto for _, texto in kapso_enviados]
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


def _escalar_con_reloj(client, kapso_enviados, monkeypatch, instante_utc, wa_message_id):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=True, resumen="lo que sea"),
    )
    monkeypatch.setattr(main_mod, "datetime", RelojFijo(instante_utc))

    _post_mensaje(client, wa_message_id, "algo que escala")

    textos = [texto for _, texto in kapso_enviados]
    assert len(textos) == 1
    return textos[0]


def test_aviso_en_horario_usa_la_hora_del_polo_no_la_del_server(client, kapso_enviados, monkeypatch):
    # Martes 19:00 UTC = martes 16:00 en Buenos Aires → dentro del horario de
    # atención. Leído como UTC serían las 19, que está fuera.
    instante = datetime(2026, 7, 28, 19, 0, tzinfo=timezone.utc)

    aviso = _escalar_con_reloj(client, kapso_enviados, monkeypatch, instante, "wamid.horario-dentro")

    assert aviso == MENSAJE_ESCALAMIENTO_EN_HORARIO


def test_aviso_fuera_de_horario_usa_la_hora_del_polo_no_la_del_server(client, kapso_enviados, monkeypatch):
    # Martes 11:00 UTC = martes 08:00 en Buenos Aires → todavía no abrió.
    # Leído como UTC serían las 11, que sí está dentro del horario.
    instante = datetime(2026, 7, 28, 11, 0, tzinfo=timezone.utc)

    aviso = _escalar_con_reloj(client, kapso_enviados, monkeypatch, instante, "wamid.horario-fuera")

    assert aviso == MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO


def test_aviso_de_fin_de_semana_avisa_el_horario_de_atencion(client, kapso_enviados, monkeypatch):
    # Sábado 2026-08-01, 22:00 en Buenos Aires (01:00 UTC del domingo).
    instante = datetime(2026, 8, 2, 1, 0, tzinfo=timezone.utc)

    aviso = _escalar_con_reloj(client, kapso_enviados, monkeypatch, instante, "wamid.horario-finde")

    assert aviso == MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO


def test_si_falla_el_envio_del_aviso_el_escalamiento_queda_igual(client, kapso_enviados, monkeypatch, caplog):
    """El commit de modo_humano va antes que el envío del aviso, así que si
    Kapso está caído la conversación queda escalada igual. Lo que no puede
    pasar es que eso se pierda: tiene que quedar el WARNING del escalamiento
    y un error que diga que lo que no salió fue el aviso."""
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=True, resumen="un reclamo"),
    )

    def kapso_caido(telefono, texto):
        raise RuntimeError("Kapso no responde")

    monkeypatch.setattr(main_mod.kapso_client, "enviar_mensaje_texto", kapso_caido)

    with caplog.at_level("WARNING", logger="bot"):
        _post_mensaje(client, "wamid.kapso-caido", "tengo un reclamo")

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is True
    assert conversacion.resumen_escalamiento == "un reclamo"
    assert kapso_enviados == []

    assert "ESCALADO A HUMANO" in caplog.text
    assert "NO se pudo enviar el aviso de escalamiento" in caplog.text


# --- Carrera entre una respuesta lenta y un escalamiento --------------------


def test_el_bot_no_escribe_encima_de_un_humano(kapso_enviados, monkeypatch):
    """Dos mensajes concurrentes del mismo número: el primero tarda en el
    modelo y el segundo escala mientras tanto. Cuando el primero vuelve, la
    conversación ya está en modo humano y su respuesta no tiene que salir.

    Es el escenario que rompía el criterio de aceptación 4 del spec-etapa2.md:
    el chequeo de modo_humano se hacía una sola vez, antes de llamar al
    modelo, y quedaba viejo para cuando el modelo contestaba.
    """
    ya_escalo = threading.Event()
    entro_al_modelo = threading.Event()

    def modelo(historial, mensaje_nuevo):
        if mensaje_nuevo == "consulta lenta":
            entro_al_modelo.set()
            assert ya_escalo.wait(timeout=5), "el mensaje que escala nunca terminó"
            return RespuestaGenerada(texto="respuesta tardía", escalar=False, resumen=None)
        return RespuestaGenerada(texto=None, escalar=True, resumen="escala mientras el otro piensa")

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    lento = threading.Thread(
        target=main_mod.procesar_mensaje_entrante,
        args=(TELEFONO_DE_PRUEBA, "wamid.lento", "consulta lenta"),
    )
    lento.start()
    assert entro_al_modelo.wait(timeout=5), "el mensaje lento nunca llegó al modelo"

    main_mod.procesar_mensaje_entrante(TELEFONO_DE_PRUEBA, "wamid.escala-rapido", "quiero hablar con alguien")
    ya_escalo.set()
    lento.join(timeout=5)
    assert not lento.is_alive()

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is True

    textos = [texto for _, texto in kapso_enviados]
    assert "respuesta tardía" not in textos, (
        f"el bot escribió encima del humano: {textos}"
    )
    assert len(textos) == 1
    assert textos[0] in AVISOS_DE_ESCALAMIENTO


def test_una_carrera_no_pisa_el_resumen_del_primer_escalamiento(kapso_enviados, monkeypatch):
    """Si el mensaje lento también quería escalar, el resumen que queda
    guardado es el del escalamiento que llegó primero: pisarlo con el segundo
    le sacaría contexto a quien vaya a atender."""
    ya_escalo = threading.Event()
    entro_al_modelo = threading.Event()

    def modelo(historial, mensaje_nuevo):
        if mensaje_nuevo == "consulta lenta":
            entro_al_modelo.set()
            assert ya_escalo.wait(timeout=5)
            return RespuestaGenerada(texto=None, escalar=True, resumen="resumen tardío")
        return RespuestaGenerada(texto=None, escalar=True, resumen="resumen que llegó primero")

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    lento = threading.Thread(
        target=main_mod.procesar_mensaje_entrante,
        args=(TELEFONO_DE_PRUEBA, "wamid.lento2", "consulta lenta"),
    )
    lento.start()
    assert entro_al_modelo.wait(timeout=5)

    main_mod.procesar_mensaje_entrante(TELEFONO_DE_PRUEBA, "wamid.escala-rapido2", "quiero hablar con alguien")
    ya_escalo.set()
    lento.join(timeout=5)

    conversacion = _conversacion_de_prueba()
    assert conversacion.resumen_escalamiento == "resumen que llegó primero"
    # Un solo aviso, no dos.
    assert len(kapso_enviados) == 1
