"""Pausa automática del bot por intervención humana (ver
spec-pausa-por-intervencion-humana.md).

El número está en modo coexistencia: la secretaría responde desde la app de
WhatsApp Business sobre el mismo número que usa el bot. Kapso mandaba un
evento `whatsapp.message.sent` para cada mensaje saliente, del bot o de la
secretaría, distinguibles por `message.kapso.origin` — ese filtro vivía
inline en el `POST /webhook` de Kapso.

**Con la Cloud API de Meta ese disparador no existe** (spec-meta-cloud-api.md,
sección 5): no hay app de negocio, y el filtro por `direction`/`origin` de
Kapso no tiene equivalente en el payload de Meta. Por eso los tests de acá ya
no le pegan al endpoint con un evento `whatsapp.message.sent` — llaman
directo a `procesar_mensaje_saliente`, que es la función que ese filtro
terminaba disparando y que sigue viva, dormida, por si más adelante se
conecta un disparador nuevo (Coexistence u otra bandeja). Los tests que
probaban el filtro en sí (`direction`/`origin` inválidos) se sacaron: ese
código ya no existe en ningún lado del proyecto, así que no hay nada que
probar.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

from app import main as main_mod
from app.db import SessionLocal
from app.models import Conversacion, Mensaje, MotivoPausa, RolMensaje
from app.respuesta import RespuestaGenerada
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import AVISOS_DE_ESCALAMIENTO, firmar_meta, payload_meta_texto

SECRETO = "test-app-secret"


def _post_entrante(client, wa_message_id: str, texto: str, telefono: str = TELEFONO_DE_PRUEBA):
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


# --- procesar_mensaje_saliente, llamado directo (dormido bajo Cloud API) ---


def test_procesar_mensaje_saliente_prende_pausa(client, meta_enviados):
    _post_entrante(client, "wamid.previo1", "hola")  # crea la conversación

    main_mod.procesar_mensaje_saliente(TELEFONO_DE_PRUEBA, "wamid.secretaria1", "ya te contesto yo")

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is True
    assert conversacion.modo_humano_desde is not None

    db = SessionLocal()
    mensaje_humano = db.query(Mensaje).filter_by(wa_message_id="wamid.secretaria1").one()
    db.close()
    assert mensaje_humano.rol == RolMensaje.HUMANO
    assert mensaje_humano.contenido == "ya te contesto yo"


def test_evento_entrante_normal_no_toca_la_pausa(client, meta_enviados):
    """Un mensaje entrante común ni siquiera pasa cerca del código de pausa:
    el bot sigue respondiendo normalmente."""
    _post_entrante(client, "wamid.normal", "hola, una consulta")

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is False
    assert len(meta_enviados) == 1


# --- Expiración por tiempo ---------------------------------------------------


def test_ventana_vigente_el_bot_no_responde(client, meta_enviados, db):
    _post_entrante(client, "wamid.previo6", "hola")
    conversacion = _conversacion_de_prueba()
    db.query(Conversacion).filter_by(id=conversacion.id).update(
        {
            "modo_humano": True,
            "motivo_pausa": MotivoPausa.INTERVENCION_MANUAL,
            "modo_humano_desde": datetime.now(timezone.utc) - timedelta(minutes=1),
        }
    )
    db.commit()
    meta_enviados.clear()

    _post_entrante(client, "wamid.durante-pausa", "sigo esperando")

    assert meta_enviados == []


def test_ventana_expirada_el_bot_responde(client, meta_enviados, db):
    _post_entrante(client, "wamid.previo7", "hola")
    conversacion = _conversacion_de_prueba()
    # PAUSA_HUMANA_MINUTOS=120 en conftest: 121 minutos atrás ya venció.
    db.query(Conversacion).filter_by(id=conversacion.id).update(
        {
            "modo_humano": True,
            "motivo_pausa": MotivoPausa.INTERVENCION_MANUAL,
            "modo_humano_desde": datetime.now(timezone.utc) - timedelta(minutes=121),
        }
    )
    db.commit()
    meta_enviados.clear()

    _post_entrante(client, "wamid.tras-vencer", "ya volvió alguien a escribir")

    assert len(meta_enviados) == 1


def test_motivo_pausa_escalamiento_no_expira_aunque_pase_la_ventana(client, meta_enviados, db):
    """Symmetric del anterior: mismo tiempo transcurrido, pero motivo
    ESCALAMIENTO en vez de INTERVENCION_MANUAL. No tiene que expirar — es
    justo la distinción que motivo_pausa existe para hacer."""
    _post_entrante(client, "wamid.previo-escal-no-expira", "hola")
    conversacion = _conversacion_de_prueba()
    db.query(Conversacion).filter_by(id=conversacion.id).update(
        {
            "modo_humano": True,
            "motivo_pausa": MotivoPausa.ESCALAMIENTO,
            "modo_humano_desde": datetime.now(timezone.utc) - timedelta(minutes=121),
            "resumen_escalamiento": "algo que el modelo no supo resolver",
        }
    )
    db.commit()
    meta_enviados.clear()

    _post_entrante(client, "wamid.tras-vencer-pero-escalado", "sigo esperando")

    assert meta_enviados == []


def test_ventana_se_reinicia_con_cada_mensaje_de_la_secretaria(client, meta_enviados, db):
    _post_entrante(client, "wamid.previo8", "hola")
    conversacion = _conversacion_de_prueba()
    db.query(Conversacion).filter_by(id=conversacion.id).update(
        {
            "modo_humano": True,
            "motivo_pausa": MotivoPausa.INTERVENCION_MANUAL,
            "modo_humano_desde": datetime.now(timezone.utc) - timedelta(minutes=119),
        }
    )
    db.commit()

    main_mod.procesar_mensaje_saliente(TELEFONO_DE_PRUEBA, "wamid.secretaria2", "segundo mensaje de la secretaria")

    conversacion = _conversacion_de_prueba()
    minutos_desde_ahora = (datetime.now(timezone.utc) - conversacion.modo_humano_desde.replace(tzinfo=timezone.utc))
    assert minutos_desde_ahora < timedelta(minutes=1)


def test_reintento_del_mismo_evento_no_reinicia_la_ventana(client, meta_enviados, db):
    _post_entrante(client, "wamid.previo9", "hola")

    main_mod.procesar_mensaje_saliente(TELEFONO_DE_PRUEBA, "wamid.secretaria-dup", "ya te ayudo")
    conversacion = _conversacion_de_prueba()
    primera_fecha = conversacion.modo_humano_desde

    # Simula el paso del tiempo entre la entrega original y un reintento del
    # mismo evento: si la dedup fallara y el reintento reiniciara la ventana,
    # la fecha guardada pasaría a ser mucho más nueva que `vieja`.
    vieja = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.query(Conversacion).filter_by(id=conversacion.id).update({"modo_humano_desde": vieja})
    db.commit()

    main_mod.procesar_mensaje_saliente(TELEFONO_DE_PRUEBA, "wamid.secretaria-dup", "ya te ayudo")  # mismo wa_message_id

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano_desde.replace(tzinfo=timezone.utc) == vieja


# --- Interacción con el escalamiento del modelo (sección 8 del spec) -------


def test_secretaria_responde_una_conversacion_ya_escalada_no_le_pone_expiracion(client, meta_enviados, db):
    """Si el modelo ya escaló (motivo_pausa=ESCALAMIENTO, que no expira) y la
    secretaría responde después, la pausa sigue sin expirar: no se le toca
    ni el motivo ni la fecha."""
    _post_entrante(client, "wamid.previo10", "hola")
    conversacion = _conversacion_de_prueba()
    fecha_del_escalamiento = datetime.now(timezone.utc) - timedelta(minutes=200)
    db.query(Conversacion).filter_by(id=conversacion.id).update(
        {
            "modo_humano": True,
            "motivo_pausa": MotivoPausa.ESCALAMIENTO,
            "modo_humano_desde": fecha_del_escalamiento,
            "resumen_escalamiento": "ya escaló el modelo",
        }
    )
    db.commit()

    main_mod.procesar_mensaje_saliente(TELEFONO_DE_PRUEBA, "wamid.secretaria3", "dale, ya lo atiendo")

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is True
    assert conversacion.motivo_pausa == MotivoPausa.ESCALAMIENTO
    assert conversacion.modo_humano_desde.replace(tzinfo=timezone.utc) == fecha_del_escalamiento
    assert conversacion.resumen_escalamiento == "ya escaló el modelo"


def test_escalar_a_humano_no_se_pierde_si_habia_una_pausa_manual_vencida(client, meta_enviados, monkeypatch, db):
    """Si había una pausa manual vencida y ahora el modelo decide escalar, el
    escalamiento tiene que ganar: eleva motivo_pausa a ESCALAMIENTO (que no
    expira) con una fecha fresca, no la fecha vieja de la pausa manual — si
    la heredara, _pausa_vigente lo calcularía como ya expirado apenas
    escalado."""
    _post_entrante(client, "wamid.previo11", "hola")
    conversacion = _conversacion_de_prueba()
    db.query(Conversacion).filter_by(id=conversacion.id).update(
        {
            "modo_humano": True,
            "motivo_pausa": MotivoPausa.INTERVENCION_MANUAL,
            "modo_humano_desde": datetime.now(timezone.utc) - timedelta(minutes=200),
        }
    )
    db.commit()

    # La pausa manual venció: el bot vuelve a responder, y esta vez el
    # modelo decide escalar.
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=True, resumen="ahora escala el modelo"),
    )
    antes_de_escalar = datetime.now(timezone.utc)

    _post_entrante(client, "wamid.escala-tras-vencer", "necesito hablar con alguien")

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is True
    assert conversacion.motivo_pausa == MotivoPausa.ESCALAMIENTO
    assert conversacion.modo_humano_desde.replace(tzinfo=timezone.utc) >= antes_de_escalar
    assert conversacion.resumen_escalamiento == "ahora escala el modelo"


# --- La carrera del §7: la secretaría responde mientras el modelo genera ---
#
# Es el motivo de existir de toda la feature. Adaptado del patrón con hilos
# de test_escalamiento.py (test_el_bot_no_escribe_encima_de_un_humano):
# mientras el modelo "piensa" para un mensaje, se simula que llega el evento
# de la secretaría llamando a procesar_mensaje_saliente directamente — la
# misma función que un futuro disparador (Coexistence u otra bandeja)
# volvería a invocar.


def test_secretaria_responde_mientras_el_modelo_genera_el_bot_no_escribe_encima(client, meta_enviados, monkeypatch):
    ya_respondio_la_secretaria = threading.Event()
    entro_al_modelo = threading.Event()

    def modelo(historial, mensaje_nuevo):
        entro_al_modelo.set()
        assert ya_respondio_la_secretaria.wait(timeout=5), "la secretaria nunca 'respondió'"
        return RespuestaGenerada(texto="respuesta tardía del bot", escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    lento = threading.Thread(
        target=main_mod.procesar_mensaje_entrante,
        args=(TELEFONO_DE_PRUEBA, "wamid.race-lento", "consulta lenta"),
    )
    lento.start()
    assert entro_al_modelo.wait(timeout=5), "el mensaje lento nunca llegó al modelo"

    main_mod.procesar_mensaje_saliente(TELEFONO_DE_PRUEBA, "wamid.race-secretaria", "ya te atiendo yo")
    ya_respondio_la_secretaria.set()
    lento.join(timeout=5)
    assert not lento.is_alive()

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is True
    assert conversacion.motivo_pausa == MotivoPausa.INTERVENCION_MANUAL

    textos = [texto for _, texto in meta_enviados]
    assert "respuesta tardía del bot" not in textos, f"el bot escribió encima de la secretaria: {textos}"
    assert textos == []


def test_un_escalamiento_durante_la_pausa_manual_no_se_pierde(client, meta_enviados, monkeypatch):
    """El otro lado de la misma carrera: si lo que el modelo decide, mientras
    la secretaría responde, es escalar, ese escalamiento tiene que quedar
    registrado igual — resumen, escalada_en y el WARNING — y no perderse
    porque la conversación ya estaba en modo_humano por la pausa manual."""
    ya_respondio_la_secretaria = threading.Event()
    entro_al_modelo = threading.Event()

    def modelo(historial, mensaje_nuevo):
        entro_al_modelo.set()
        assert ya_respondio_la_secretaria.wait(timeout=5), "la secretaria nunca 'respondió'"
        return RespuestaGenerada(texto=None, escalar=True, resumen="quiere alquilar una sala")

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    lento = threading.Thread(
        target=main_mod.procesar_mensaje_entrante,
        args=(TELEFONO_DE_PRUEBA, "wamid.race-escala-lento", "quiero alquilar una sala"),
    )
    lento.start()
    assert entro_al_modelo.wait(timeout=5), "el mensaje lento nunca llegó al modelo"

    main_mod.procesar_mensaje_saliente(TELEFONO_DE_PRUEBA, "wamid.race-escala-secretaria", "ya te atiendo yo")
    ya_respondio_la_secretaria.set()
    lento.join(timeout=5)
    assert not lento.is_alive()

    conversacion = _conversacion_de_prueba()
    assert conversacion.modo_humano is True
    assert conversacion.motivo_pausa == MotivoPausa.ESCALAMIENTO
    assert conversacion.resumen_escalamiento == "quiere alquilar una sala"
    assert conversacion.escalada_en is not None

    textos = [texto for _, texto in meta_enviados]
    assert textos[0] in AVISOS_DE_ESCALAMIENTO
