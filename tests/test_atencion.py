"""Atención humana en el CRM: abrir (al derivar o desde el panel), tomar y
resolver (ver app/atencion.py).

Lo que tiene que valer siempre:

- Con una atención abierta, el bot está pausado con una pausa que no vence.
- Solo una persona puede quedarse con una atención, aunque tomen a la vez.
- Resolver no manda nada por WhatsApp, conserva motivo y resumen en la
  atención, y el bot vuelve recién con el próximo mensaje entrante.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app import atencion as atencion_mod
from app import main as main_mod
from app.crm import usuarios
from app.crm.auth import HEADER_CSRF
from app.crm.rutas import (
    CODIGO_ATENCION_AJENA,
    CODIGO_ATENCION_NO_DISPONIBLE,
    CODIGO_ATENCION_SIN_TOMAR,
    CODIGO_ATENCION_YA_ABIERTA,
    CODIGO_SIN_ATENCION_ABIERTA,
    ERROR_ATENCION_AJENA,
    ERROR_ATENCION_NO_DISPONIBLE,
    ERROR_ATENCION_SIN_TOMAR,
    ERROR_ATENCION_YA_ABIERTA,
    ERROR_SIN_ATENCION_ABIERTA,
    MENSAJE_BOT_REACTIVADO,
)
from app.db import SessionLocal
from app.main import app
from app.models import (
    Atencion,
    Conversacion,
    EstadoAtencion,
    Mensaje,
    MotivoAtencion,
    MotivoPausa,
    RolMensaje,
)
from app.pausa import pausa_vigente
from app.respuesta import RespuestaGenerada
from tests.conftest import PASSWORD_DE_PRUEBA, TELEFONO_DE_PRUEBA, hacer_login, login_crm
from tests.helpers import AVISOS_DE_ESCALAMIENTO, crear_conversacion, firmar_meta, payload_meta_texto

SECRETO_META = "test-app-secret"
RESPUESTA_DEL_BOT = "Hola, te cuento cómo funciona la membresía."
RESUMEN = "Quiere reservar el auditorio para un evento"


# --- Helpers ---------------------------------------------------------------


def _entrante(client, wa_message_id: str, texto: str):
    payload = payload_meta_texto(wa_message_id, TELEFONO_DE_PRUEBA, texto)
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO_META)},
    )


def _modelo_que_escala(monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto="", escalar=True, resumen=RESUMEN),
    )


def _modelo_que_responde(monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=RESPUESTA_DEL_BOT, escalar=False, resumen=None),
    )


def _conversacion(db) -> Conversacion:
    db.expire_all()
    return db.query(Conversacion).filter_by(canal="whatsapp", identificador_externo=TELEFONO_DE_PRUEBA).one()


def _atenciones(db, conversacion_id: int) -> list[Atencion]:
    db.expire_all()
    return db.query(Atencion).filter_by(conversacion_id=conversacion_id).order_by(Atencion.id).all()


def _detalle(respuesta) -> dict:
    """El `detail` de un error del flujo de atención tiene que ser siempre
    `{code, message}` — nunca un string suelto (ver app/crm/rutas.py,
    `_error_estructurado`)."""
    detalle = respuesta.json()["detail"]
    assert isinstance(detalle, dict) and set(detalle) == {"code", "message"}
    return detalle


def _url(conversacion_id: int, accion: str = "") -> str:
    base = f"/crm/api/conversaciones/{conversacion_id}/atencion"
    return f"{base}/{accion}" if accion else base


def _otra_cuenta(db, nombre: str = "secretaria-dos"):
    """Una segunda cuenta del panel con su propio cliente y su CSRF."""
    cuenta = usuarios.crear(db, nombre, PASSWORD_DE_PRUEBA)
    cliente = TestClient(app, base_url="https://testserver")
    assert hacer_login(cliente, nombre, PASSWORD_DE_PRUEBA).status_code == 200
    csrf = cliente.get("/crm/api/sesion").json()["csrf"]
    return cuenta, cliente, csrf


def _sin_pausa(db) -> Conversacion:
    return crear_conversacion(
        db, TELEFONO_DE_PRUEBA, [(RolMensaje.USUARIO, "hola", 5), (RolMensaje.BOT, "¡hola!", 4)]
    )


# --- Abrir por escalamiento ------------------------------------------------


def test_escalar_abre_una_atencion_pendiente_sin_responsable(escalamiento_activo, client, meta_enviados, monkeypatch, db):
    _modelo_que_escala(monkeypatch)

    _entrante(client, "wamid.at.esc.1", "quiero reservar el auditorio")

    conversacion = _conversacion(db)
    assert conversacion.modo_humano is True
    assert conversacion.motivo_pausa == MotivoPausa.ESCALAMIENTO

    [abierta] = _atenciones(db, conversacion.id)
    assert abierta.estado == EstadoAtencion.PENDIENTE.value
    assert abierta.motivo == MotivoAtencion.ESCALAMIENTO.value
    assert abierta.resumen == RESUMEN
    assert abierta.responsable_id is None
    # La abrió el bot, no una persona.
    assert abierta.iniciada_por_id is None

    # El escalamiento sigue avisándole al usuario como siempre.
    assert [texto for _, texto in meta_enviados] in ([aviso] for aviso in AVISOS_DE_ESCALAMIENTO)


def test_con_el_escalamiento_apagado_no_se_abre_ninguna_atencion(client, meta_enviados, monkeypatch, db):
    _modelo_que_escala(monkeypatch)

    _entrante(client, "wamid.at.esc.off", "quiero reservar el auditorio")

    assert _atenciones(db, _conversacion(db).id) == []


# --- Iniciar desde el CRM --------------------------------------------------


def test_iniciar_desde_el_crm_pausa_al_bot_en_el_acto(cliente_crm, usuario_crm, db, meta_enviados):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)

    respuesta = cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["atencion"]["estado"] == "pendiente"
    assert cuerpo["atencion"]["motivo"] == "sin_clasificar"
    assert cuerpo["atencion"]["iniciada_por"] == usuario_crm.usuario
    assert cuerpo["atencion"]["responsable"] is None
    assert cuerpo["conversacion"]["pausado"] is True
    assert cuerpo["conversacion"]["motivo_pausa"] == "atencion_crm"
    assert cuerpo["conversacion"]["atencion"]["id"] == cuerpo["atencion"]["id"]

    db.refresh(conversacion)
    assert conversacion.modo_humano is True
    assert conversacion.motivo_pausa == MotivoPausa.ATENCION_CRM
    # Iniciar no le escribe nada al contacto.
    assert meta_enviados == []


def test_la_pausa_de_una_atencion_del_crm_no_vence(cliente_crm, usuario_crm, db):
    """La pausa manual vieja vence a los PAUSA_HUMANA_MINUTOS (120 en la
    suite). La de una atención no: dura lo que dure la atención."""
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})

    db.refresh(conversacion)
    dentro_de_un_dia = datetime.now(timezone.utc) + timedelta(days=1)
    assert pausa_vigente(conversacion, dentro_de_un_dia) is True


def test_iniciar_sobre_una_pausa_manual_la_convierte_en_una_que_no_vence(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = crear_conversacion(
        db,
        TELEFONO_DE_PRUEBA,
        modo_humano=True,
        motivo_pausa=MotivoPausa.INTERVENCION_MANUAL,
        modo_humano_desde=datetime.now(timezone.utc) - timedelta(minutes=100),
    )

    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})

    db.refresh(conversacion)
    assert conversacion.motivo_pausa == MotivoPausa.ATENCION_CRM
    assert pausa_vigente(conversacion, datetime.now(timezone.utc) + timedelta(hours=3)) is True


def test_iniciar_sobre_una_conversacion_ya_escalada_hereda_motivo_y_resumen(cliente_crm, usuario_crm, db):
    """El caso de las conversaciones escaladas antes de que existieran las
    atenciones: tienen la pausa pero no la tarjeta."""
    csrf = login_crm(cliente_crm, usuario_crm)
    hace_un_rato = datetime.now(timezone.utc) - timedelta(hours=5)
    conversacion = crear_conversacion(
        db,
        TELEFONO_DE_PRUEBA,
        modo_humano=True,
        motivo_pausa=MotivoPausa.ESCALAMIENTO,
        modo_humano_desde=hace_un_rato,
        resumen_escalamiento=RESUMEN,
        escalada_en=hace_un_rato,
    )

    respuesta = cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 201
    assert respuesta.json()["atencion"]["motivo"] == "escalamiento"
    assert respuesta.json()["atencion"]["resumen"] == RESUMEN
    db.refresh(conversacion)
    # La pausa del escalamiento queda como estaba: tampoco vence.
    assert conversacion.motivo_pausa == MotivoPausa.ESCALAMIENTO
    assert conversacion.resumen_escalamiento == RESUMEN


def test_no_se_puede_iniciar_dos_veces(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)

    primera = cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    segunda = cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})

    assert primera.status_code == 201
    assert segunda.status_code == 409
    assert _detalle(segunda) == {"code": CODIGO_ATENCION_YA_ABIERTA, "message": ERROR_ATENCION_YA_ABIERTA}
    assert len(_atenciones(db, conversacion.id)) == 1


def test_la_base_no_admite_dos_atenciones_abiertas_en_la_misma_conversacion(db):
    """El índice único parcial es la última barrera: aunque el código se
    salteara el chequeo, la base rechaza la segunda abierta. Una resuelta
    más una abierta sí conviven."""
    conversacion = _sin_pausa(db)
    db.add(Atencion(conversacion_id=conversacion.id, estado=EstadoAtencion.RESUELTA.value))
    db.add(Atencion(conversacion_id=conversacion.id, estado=EstadoAtencion.PENDIENTE.value))
    db.commit()

    db.add(Atencion(conversacion_id=conversacion.id, estado=EstadoAtencion.EN_ATENCION.value))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# --- Tomar -----------------------------------------------------------------


def test_tomar_asigna_responsable_y_pasa_a_en_atencion(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})

    respuesta = cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["atencion"]["estado"] == "en_atencion"
    assert respuesta.json()["atencion"]["responsable"] == usuario_crm.usuario
    assert respuesta.json()["atencion"]["tomada_en"] is not None
    # Tomar no cambia la pausa: ya estaba puesta desde que se abrió.
    db.refresh(conversacion)
    assert conversacion.modo_humano is True


def test_si_otra_persona_ya_la_tomo_da_409_y_no_cambia_el_responsable(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    _, otro_cliente, otro_csrf = _otra_cuenta(db)
    respuesta = otro_cliente.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: otro_csrf})

    assert respuesta.status_code == 409
    assert _detalle(respuesta) == {"code": CODIGO_ATENCION_NO_DISPONIBLE, "message": ERROR_ATENCION_NO_DISPONIBLE}
    [abierta] = _atenciones(db, conversacion.id)
    assert abierta.responsable_id == usuario_crm.id


def test_tomar_de_nuevo_la_propia_no_es_un_error(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    respuesta = cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 200
    assert respuesta.json()["atencion"]["responsable"] == usuario_crm.usuario


def test_toma_concurrente_solo_una_persona_se_queda_con_la_atencion(db):
    """Varias personas toman la misma atención a la vez, cada una con su
    propia sesión de base (como dos requests reales). Exactamente una gana."""
    conversacion = _sin_pausa(db)
    cuentas = [usuarios.crear(db, f"secretaria-{i}", PASSWORD_DE_PRUEBA) for i in range(6)]
    atencion = atencion_mod.iniciar_desde_crm(db, conversacion, cuentas[0].id)

    largada = threading.Barrier(len(cuentas))
    ganadores, perdedores, errores = [], [], []

    def tomar_como(usuario_id: int):
        sesion = SessionLocal()
        try:
            propia = sesion.query(Atencion).filter_by(id=atencion.id).one()
            largada.wait()
            atencion_mod.tomar(sesion, propia, usuario_id)
            ganadores.append(usuario_id)
        except atencion_mod.AtencionNoDisponible:
            perdedores.append(usuario_id)
        except Exception as error:  # noqa: BLE001 — cualquier otra cosa hace fallar el test
            errores.append(error)
        finally:
            sesion.close()

    hilos = [threading.Thread(target=tomar_como, args=(cuenta.id,)) for cuenta in cuentas]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(timeout=30)

    assert errores == []
    assert len(ganadores) == 1
    assert len(perdedores) == len(cuentas) - 1
    [abierta] = _atenciones(db, conversacion.id)
    assert abierta.estado == EstadoAtencion.EN_ATENCION.value
    assert abierta.responsable_id == ganadores[0]


# --- Resolver --------------------------------------------------------------


def test_resolver_cierra_reactiva_y_conserva_motivo_y_resumen(
    escalamiento_activo, client, cliente_crm, usuario_crm, meta_enviados, monkeypatch, db
):
    csrf = login_crm(cliente_crm, usuario_crm)
    _modelo_que_escala(monkeypatch)
    _entrante(client, "wamid.at.res.1", "quiero reservar el auditorio")
    conversacion = _conversacion(db)
    enviados_antes = list(meta_enviados)
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    respuesta = cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["atencion"]["estado"] == "resuelta"
    assert cuerpo["atencion"]["resuelta_por"] == usuario_crm.usuario
    assert cuerpo["conversacion"]["pausado"] is False
    assert cuerpo["conversacion"]["atencion"] is None
    assert cuerpo["mensaje"] == MENSAJE_BOT_REACTIVADO

    [resuelta] = _atenciones(db, conversacion.id)
    assert resuelta.estado == EstadoAtencion.RESUELTA.value
    assert resuelta.motivo == MotivoAtencion.ESCALAMIENTO.value
    assert resuelta.resumen == RESUMEN
    assert resuelta.resuelta_en is not None

    conversacion = _conversacion(db)
    assert conversacion.modo_humano is False
    # reactivar_bot limpia el resumen de la conversación; el histórico
    # quedó en la atención (arriba).
    assert conversacion.resumen_escalamiento is None

    # Resolver no le escribe nada al contacto.
    assert meta_enviados == enviados_antes


def test_resolver_dos_veces_la_segunda_no_encuentra_atencion_abierta(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    primera = cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf})
    segunda = cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf})

    assert primera.status_code == 200
    assert segunda.status_code == 404


def test_resolver_una_atencion_ya_resuelta_no_vuelve_a_tocar_la_pausa(db, usuario_crm):
    """Dos personas que resuelven casi a la vez: la segunda llega con la fila
    ya leída. No tiene que reactivar el bot de nuevo — si entre las dos se
    abrió otra atención, la estaría pisando."""
    conversacion = _sin_pausa(db)
    abierta = atencion_mod.iniciar_desde_crm(db, conversacion, usuario_crm.id)
    atencion_mod.tomar(db, abierta, usuario_crm.id)
    atencion_mod.resolver(db, abierta, usuario_crm.id)
    nueva = atencion_mod.iniciar_desde_crm(db, conversacion, usuario_crm.id)
    atencion_mod.tomar(db, nueva, usuario_crm.id)

    with pytest.raises(atencion_mod.AtencionNoDisponible):
        atencion_mod.resolver(db, abierta, usuario_crm.id)

    db.refresh(conversacion)
    assert conversacion.modo_humano is True
    assert atencion_mod.atencion_abierta(db, conversacion.id).id == nueva.id


def test_una_atencion_pendiente_no_se_puede_resolver_sin_tomarla(cliente_crm, usuario_crm, db, meta_enviados):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})

    respuesta = cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 409
    assert _detalle(respuesta) == {"code": CODIGO_ATENCION_SIN_TOMAR, "message": ERROR_ATENCION_SIN_TOMAR}
    [abierta] = _atenciones(db, conversacion.id)
    assert abierta.estado == EstadoAtencion.PENDIENTE.value
    assert abierta.resuelta_por_id is None
    # El bot sigue pausado.
    assert _conversacion(db).modo_humano is True
    assert meta_enviados == []


def test_el_responsable_puede_resolver_y_queda_registrado(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    respuesta = cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 200
    assert respuesta.json()["atencion"]["resuelta_por"] == usuario_crm.usuario
    [resuelta] = _atenciones(db, conversacion.id)
    assert resuelta.estado == EstadoAtencion.RESUELTA.value
    assert resuelta.resuelta_por_id == usuario_crm.id


def test_otra_sesion_no_puede_resolver_una_atencion_ajena(cliente_crm, usuario_crm, db, meta_enviados):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    _, otro_cliente, otro_csrf = _otra_cuenta(db)
    respuesta = otro_cliente.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: otro_csrf})

    assert respuesta.status_code == 409
    assert _detalle(respuesta) == {"code": CODIGO_ATENCION_AJENA, "message": ERROR_ATENCION_AJENA}
    [abierta] = _atenciones(db, conversacion.id)
    assert abierta.estado == EstadoAtencion.EN_ATENCION.value
    assert abierta.responsable_id == usuario_crm.id
    assert abierta.resuelta_por_id is None
    assert _conversacion(db).modo_humano is True
    assert meta_enviados == []


def test_resolver_directo_en_el_dominio_tambien_exige_ser_el_responsable(db, usuario_crm):
    """La regla vive en app/atencion.py, no solo en la ruta: cualquier
    camino futuro que llame a `resolver` la hereda."""
    otra = usuarios.crear(db, "secretaria-dos", PASSWORD_DE_PRUEBA)
    conversacion = _sin_pausa(db)
    abierta = atencion_mod.iniciar_desde_crm(db, conversacion, usuario_crm.id)

    with pytest.raises(atencion_mod.AtencionSinTomar):
        atencion_mod.resolver(db, abierta, usuario_crm.id)

    atencion_mod.tomar(db, abierta, usuario_crm.id)
    with pytest.raises(atencion_mod.AtencionAjena):
        atencion_mod.resolver(db, abierta, otra.id)

    db.refresh(conversacion)
    assert conversacion.modo_humano is True


# --- El ciclo completo -----------------------------------------------------


def test_ciclo_iniciar_tomar_resolver_y_el_bot_vuelve_con_el_siguiente_mensaje(
    client, cliente_crm, usuario_crm, meta_enviados, monkeypatch, db
):
    csrf = login_crm(cliente_crm, usuario_crm)
    _modelo_que_responde(monkeypatch)

    # Una conversación normal: el bot contesta.
    _entrante(client, "wamid.ciclo.1", "hola")
    assert [texto for _, texto in meta_enviados] == [RESPUESTA_DEL_BOT]
    conversacion = _conversacion(db)

    # Iniciar: el bot se calla en el acto, antes de que nadie la tome.
    assert cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf}).status_code == 201
    _entrante(client, "wamid.ciclo.2", "¿siguen ahí?")
    assert len(meta_enviados) == 1

    # Tomar: sigue callado.
    assert cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf}).status_code == 200
    _entrante(client, "wamid.ciclo.3", "necesito hablar con alguien")
    assert len(meta_enviados) == 1

    # Resolver: no sale nada, y los mensajes de la atención no se responden
    # a destiempo.
    assert cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf}).status_code == 200
    assert len(meta_enviados) == 1

    # El siguiente mensaje entrante sí lo contesta el bot.
    _entrante(client, "wamid.ciclo.4", "gracias, otra consulta")
    assert [texto for _, texto in meta_enviados] == [RESPUESTA_DEL_BOT, RESPUESTA_DEL_BOT]

    # Los mensajes que llegaron durante la atención quedaron guardados.
    contenidos = [m.contenido for m in db.query(Mensaje).filter_by(conversacion_id=conversacion.id)]
    assert "¿siguen ahí?" in contenidos
    assert "necesito hablar con alguien" in contenidos


# --- Los mensajes recibidos durante una atención no le vuelven al bot -----
#
# Regla de producto: todo mensaje del usuario recibido mientras hay una
# atención humana abierta le pertenece a esa atención, no al bot. Al
# resolver, `avanzar_hasta_el_ultimo_entrante` (app/agrupamiento.py) mueve
# la marca de agrupado hasta el último mensaje de texto que exista en ese
# momento, en el mismo commit que reactiva el bot — así el próximo lote
# arranca limpio. Los cuatro tests de acá abajo afirman el contenido exacto
# de lo que ve el modelo, no solo cuántos mensajes salieron: un
# `RESPUESTA_DEL_BOT` fijo (como en `_modelo_que_responde`) pasaría igual
# aunque el lote llevara mensajes viejos de la atención — que es
# exactamente lo que pasaba antes de este fix sin que ningún test lo
# detectara.


def _modelo_que_registra(monkeypatch, lotes: list[str]):
    def generar(historial, mensaje_nuevo):
        lotes.append(mensaje_nuevo)
        return RespuestaGenerada(texto=RESPUESTA_DEL_BOT, escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", generar)


def test_mensaje_durante_la_atencion_no_reaparece_tras_resolver(
    cliente_crm, usuario_crm, client, meta_enviados, monkeypatch, db
):
    lotes: list[str] = []
    _modelo_que_registra(monkeypatch, lotes)
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    _entrante(client, "wamid.due.1", "¿cuánto sale la sala grande?")
    assert cliente_crm.post(
        f"/crm/api/conversaciones/{conversacion.id}/atencion/responder",
        json={"texto": "Sale $50.000 por día"},
        headers={HEADER_CSRF: csrf},
    ).status_code == 201

    assert cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf}).status_code == 200
    assert lotes == [], "resolver no manda nada ni dispara al modelo"

    _entrante(client, "wamid.due.2", "gracias!")

    assert lotes == ["gracias!"], "el bot solo tiene que ver el mensaje nuevo, no el de antes de resolver"


def test_varios_mensajes_durante_la_atencion_no_reaparecen_tras_resolver(
    cliente_crm, usuario_crm, client, meta_enviados, monkeypatch, db
):
    lotes: list[str] = []
    _modelo_que_registra(monkeypatch, lotes)
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    _entrante(client, "wamid.varios.1", "hola?")
    _entrante(client, "wamid.varios.2", "¿están ahí?")
    _entrante(client, "wamid.varios.3", "necesito una respuesta")

    assert cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf}).status_code == 200
    assert lotes == []

    _entrante(client, "wamid.varios.4", "mensaje nuevo de verdad")

    assert lotes == ["mensaje nuevo de verdad"], "ninguno de los tres viejos tiene que colarse en el lote"


def test_recuperacion_al_reiniciar_no_retoma_mensajes_de_una_atencion_resuelta(
    cliente_crm, usuario_crm, client, meta_enviados, monkeypatch, db
):
    """`_recuperar_lotes_pendientes` (app/main.py) es lo que retoma un lote a
    medio camino tras un reinicio del proceso. Sin el avance de la marca al
    resolver, encontraría estos mensajes como "pendientes" y haría responder
    al bot sin que hubiera entrado ningún mensaje nuevo — justo lo que el
    requisito 8 prohíbe."""
    lotes: list[str] = []
    _modelo_que_registra(monkeypatch, lotes)
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})
    _entrante(client, "wamid.reinicio.1", "¿hay lugar el sábado?")
    assert cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf}).status_code == 200

    enviados_antes = list(meta_enviados)
    for hilo in main_mod._recuperar_lotes_pendientes():
        hilo.join(timeout=5)

    assert lotes == [], "sin mensaje nuevo, el reinicio no tiene que llamar al modelo"
    assert meta_enviados == enviados_antes


def test_mensaje_despues_de_resolver_si_le_llega_al_bot(
    cliente_crm, usuario_crm, client, meta_enviados, monkeypatch, db
):
    """Contraparte de los tres tests de arriba: la regla es sobre mensajes
    recibidos *mientras* la atención está abierta, no una desactivación
    general del bot después de resolver."""
    _modelo_que_responde(monkeypatch)
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})
    assert cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf}).status_code == 200

    _entrante(client, "wamid.despues.1", "una consulta nueva, sin relación")

    assert [texto for _, texto in meta_enviados] == [RESPUESTA_DEL_BOT]


def test_despues_de_resolver_un_escalamiento_nuevo_abre_otra_atencion(
    escalamiento_activo, client, cliente_crm, usuario_crm, meta_enviados, monkeypatch, db
):
    csrf = login_crm(cliente_crm, usuario_crm)
    _modelo_que_escala(monkeypatch)
    _entrante(client, "wamid.reabre.1", "quiero reservar")
    conversacion = _conversacion(db)
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf})

    _entrante(client, "wamid.reabre.2", "otra vez yo, quiero reservar")

    atenciones = _atenciones(db, conversacion.id)
    assert [a.estado for a in atenciones] == ["resuelta", "pendiente"]


def test_un_escalamiento_durante_una_atencion_del_crm_no_abre_otra_ni_avisa(
    escalamiento_activo, cliente_crm, usuario_crm, meta_enviados, db
):
    """Si el modelo estaba generando cuando alguien inició la atención y
    termina escalando, ya hay una persona a cargo: ni segunda tarjeta ni el
    aviso de "te pasamos con alguien" en medio de la atención."""
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})

    main_mod.escalar_a_humano(db, conversacion, "resumen del modelo")

    [abierta] = _atenciones(db, conversacion.id)
    assert abierta.motivo == MotivoAtencion.SIN_CLASIFICAR.value
    db.refresh(conversacion)
    assert conversacion.motivo_pausa == MotivoPausa.ATENCION_CRM
    assert meta_enviados == []


# --- Otros caminos que reactivan ------------------------------------------


def test_reactivar_bot_con_la_atencion_propia_la_resuelve(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    cliente_crm.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf})

    respuesta = cliente_crm.post(
        f"/crm/api/conversaciones/{conversacion.id}/reactivar", headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 200
    [cerrada] = _atenciones(db, conversacion.id)
    assert cerrada.estado == EstadoAtencion.RESUELTA.value
    assert cerrada.resuelta_por_id == usuario_crm.id
    assert _conversacion(db).modo_humano is False


def test_reactivar_bot_no_sirve_para_saltearse_la_regla_de_resolver(cliente_crm, usuario_crm, db):
    """Con una atención abierta, "Reactivar bot" es resolver: una pendiente
    hay que tomarla primero, y una ajena no se toca."""
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    url_reactivar = f"/crm/api/conversaciones/{conversacion.id}/reactivar"

    pendiente = cliente_crm.post(url_reactivar, headers={HEADER_CSRF: csrf})
    assert pendiente.status_code == 409
    assert _detalle(pendiente) == {"code": CODIGO_ATENCION_SIN_TOMAR, "message": ERROR_ATENCION_SIN_TOMAR}

    _, otro_cliente, otro_csrf = _otra_cuenta(db)
    otro_cliente.post(_url(conversacion.id, "tomar"), headers={HEADER_CSRF: otro_csrf})
    ajena = cliente_crm.post(url_reactivar, headers={HEADER_CSRF: csrf})
    assert ajena.status_code == 409
    assert _detalle(ajena) == {"code": CODIGO_ATENCION_AJENA, "message": ERROR_ATENCION_AJENA}

    [abierta] = _atenciones(db, conversacion.id)
    assert abierta.estado == EstadoAtencion.EN_ATENCION.value
    assert _conversacion(db).modo_humano is True


def test_el_script_de_reseteo_cierra_la_atencion_abierta(db, usuario_crm):
    from scripts.resetear_modo_humano import resetear_modo_humano

    conversacion = _sin_pausa(db)
    atencion_mod.iniciar_desde_crm(db, conversacion, usuario_crm.id)
    db.add(Mensaje(conversacion_id=conversacion.id, rol=RolMensaje.USUARIO, contenido="durante", tipo="text"))
    db.commit()

    assert resetear_modo_humano(TELEFONO_DE_PRUEBA) is True

    [cerrada] = _atenciones(db, conversacion.id)
    assert cerrada.estado == EstadoAtencion.RESUELTA.value
    assert cerrada.resuelta_por_id is None
    conversacion = _conversacion(db)
    assert conversacion.modo_humano is False
    # Mismo helper que usa "Resolver" desde el panel: los mensajes que
    # llegaron durante la atención tampoco le vuelven al bot cuando quien
    # cierra es el script de consola.
    ultimo = (
        db.query(Mensaje)
        .filter_by(conversacion_id=conversacion.id, rol=RolMensaje.USUARIO)
        .order_by(Mensaje.id.desc())
        .first()
    )
    assert conversacion.ultimo_mensaje_agrupado_id == ultimo.id


def test_resolver_si_hay_abierta_no_toca_la_marca_si_no_habia_nada_que_cerrar(db):
    """`resolver_si_hay_abierta` no avanza la marca de agrupado cuando no
    encuentra ninguna atención abierta: no hay atención de por medio que
    justifique tocarle nada al agrupamiento normal del bot."""
    from app.atencion import resolver_si_hay_abierta

    conversacion = crear_conversacion(db, TELEFONO_DE_PRUEBA, [(RolMensaje.USUARIO, "hola", 5)])
    assert conversacion.ultimo_mensaje_agrupado_id is None

    resolver_si_hay_abierta(db, conversacion, usuario_id=None)
    db.commit()

    db.refresh(conversacion)
    assert conversacion.ultimo_mensaje_agrupado_id is None


# --- Detalle y protección -------------------------------------------------


def test_el_detalle_de_la_conversacion_trae_la_atencion_abierta(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)

    sin = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/mensajes").json()
    assert sin["conversacion"]["atencion"] is None

    cliente_crm.post(_url(conversacion.id), headers={HEADER_CSRF: csrf})
    con = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/mensajes").json()
    assert con["conversacion"]["atencion"]["estado"] == "pendiente"


@pytest.mark.parametrize("accion", ["", "tomar", "resolver"])
def test_sin_csrf_no_se_escribe_nada(cliente_crm, usuario_crm, db, accion):
    login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)
    if accion:
        atencion_mod.iniciar_desde_crm(db, conversacion, usuario_crm.id)
    antes = [(a.estado, a.responsable_id) for a in _atenciones(db, conversacion.id)]

    respuesta = cliente_crm.post(_url(conversacion.id, accion))

    assert respuesta.status_code == 403
    assert [(a.estado, a.responsable_id) for a in _atenciones(db, conversacion.id)] == antes


@pytest.mark.parametrize("accion", ["", "tomar", "resolver"])
def test_sin_sesion_no_se_escribe_nada(cliente_crm, db, accion):
    conversacion = _sin_pausa(db)

    respuesta = cliente_crm.post(_url(conversacion.id, accion))

    assert respuesta.status_code == 401
    assert _atenciones(db, conversacion.id) == []


@pytest.mark.parametrize("accion", ["tomar", "resolver"])
def test_tomar_o_resolver_sin_atencion_abierta_da_404(cliente_crm, usuario_crm, db, accion):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _sin_pausa(db)

    respuesta = cliente_crm.post(_url(conversacion.id, accion), headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 404
    assert _detalle(respuesta) == {"code": CODIGO_SIN_ATENCION_ABIERTA, "message": ERROR_SIN_ATENCION_ABIERTA}


def test_iniciar_en_una_conversacion_que_no_existe_da_404(cliente_crm, usuario_crm):
    csrf = login_crm(cliente_crm, usuario_crm)

    respuesta = cliente_crm.post(_url(9999), headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 404
