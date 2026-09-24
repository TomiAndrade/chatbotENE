"""Respuesta manual desde una atención tomada del CRM (Tarea 3A)."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from app import atencion as atencion_mod
from app import envio as envio_mod
from app import main as main_mod
from app.config import config
from app.costo_meta import mes_actual
from app.crm import usuarios
from app.crm.auth import HEADER_CSRF
from app.crm.rutas import (
    CODIGO_ATENCION_AJENA,
    CODIGO_ATENCION_NO_DISPONIBLE,
    CODIGO_ATENCION_SIN_TOMAR,
    CODIGO_FALLO_META,
    CODIGO_FUERA_DE_VENTANA,
    CODIGO_PRESUPUESTO_AGOTADO,
    CODIGO_TEXTO_INVALIDO,
)
from app.db import SessionLocal
from app.main import app
from app.models import (
    Atencion,
    Conversacion,
    EnvioWhatsapp,
    Mensaje,
    MotivoPausa,
    PresupuestoMetaMensual,
    RolMensaje,
)
from app.respuesta import RespuestaGenerada
from tests.conftest import (
    PASSWORD_DE_PRUEBA,
    TELEFONO_DE_PRUEBA,
    hacer_login,
    login_crm,
)
from tests.helpers import crear_conversacion, firmar_meta, payload_meta_texto


SECRETO_META = "test-app-secret"


def _url(conversacion_id: int, accion: str = "responder") -> str:
    return f"/crm/api/conversaciones/{conversacion_id}/atencion/{accion}"


def _preparar_atencion(
    cliente_crm,
    usuario_crm,
    db,
    *,
    mensajes=None,
    tomar: bool = True,
):
    csrf = login_crm(cliente_crm, usuario_crm)
    if mensajes is None:
        mensajes = [(RolMensaje.USUARIO, "hola, necesito ayuda", 5)]
    conversacion = crear_conversacion(db, TELEFONO_DE_PRUEBA, mensajes)
    assert cliente_crm.post(
        f"/crm/api/conversaciones/{conversacion.id}/atencion",
        headers={HEADER_CSRF: csrf},
    ).status_code == 201
    if tomar:
        assert cliente_crm.post(
            _url(conversacion.id, "tomar"), headers={HEADER_CSRF: csrf}
        ).status_code == 200
    return conversacion, csrf


def _codigo(respuesta) -> str:
    return respuesta.json()["detail"]["code"]


def _otra_cuenta(db, nombre: str = "secretaria-dos"):
    cuenta = usuarios.crear(db, nombre, PASSWORD_DE_PRUEBA)
    cliente = TestClient(app, base_url="https://testserver")
    assert hacer_login(cliente, nombre, PASSWORD_DE_PRUEBA).status_code == 200
    csrf = cliente.get("/crm/api/sesion").json()["csrf"]
    return cuenta, cliente, csrf


def test_responsable_envia_y_guarda_humano_autor_costo_y_pausa(
    cliente_crm, usuario_crm, db, meta_enviados
):
    conversacion, csrf = _preparar_atencion(cliente_crm, usuario_crm, db)

    respuesta = cliente_crm.post(
        _url(conversacion.id),
        json={"texto": "  Te confirmo que recibimos tu consulta.  "},
        headers={HEADER_CSRF: csrf},
    )

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["status"] == "aceptado_por_meta"
    assert respuesta.json()["mensaje"]["rol"] == "humano"
    assert respuesta.json()["mensaje"]["autor"] == usuario_crm.usuario
    assert respuesta.json()["mensaje"]["contenido"] == "Te confirmo que recibimos tu consulta."
    assert meta_enviados == [(TELEFONO_DE_PRUEBA, "Te confirmo que recibimos tu consulta.")]

    db.expire_all()
    humano = db.query(Mensaje).filter_by(conversacion_id=conversacion.id, rol=RolMensaje.HUMANO).one()
    assert humano.autor_crm_id == usuario_crm.id
    assert db.query(EnvioWhatsapp).filter_by(mensaje_id=humano.id).count() == 1

    abierta = atencion_mod.atencion_abierta(db, conversacion.id)
    assert abierta is not None
    assert abierta.responsable_id == usuario_crm.id
    conversacion = db.query(Conversacion).filter_by(id=conversacion.id).one()
    assert conversacion.modo_humano is True
    assert conversacion.motivo_pausa == MotivoPausa.ATENCION_CRM


def test_pendiente_sin_tomar_no_puede_responder(cliente_crm, usuario_crm, db, meta_enviados):
    conversacion, csrf = _preparar_atencion(cliente_crm, usuario_crm, db, tomar=False)

    respuesta = cliente_crm.post(
        _url(conversacion.id), json={"texto": "hola"}, headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 409
    assert _codigo(respuesta) == CODIGO_ATENCION_SIN_TOMAR
    assert meta_enviados == []


def test_otra_cuenta_no_puede_responder(cliente_crm, usuario_crm, db, meta_enviados):
    conversacion, _ = _preparar_atencion(cliente_crm, usuario_crm, db)
    _, otro_cliente, otro_csrf = _otra_cuenta(db)

    respuesta = otro_cliente.post(
        _url(conversacion.id), json={"texto": "me meto"}, headers={HEADER_CSRF: otro_csrf}
    )

    assert respuesta.status_code == 409
    assert _codigo(respuesta) == CODIGO_ATENCION_AJENA
    assert meta_enviados == []


def test_atencion_resuelta_no_puede_responder(cliente_crm, usuario_crm, db, meta_enviados):
    conversacion, csrf = _preparar_atencion(cliente_crm, usuario_crm, db)
    assert cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf}).status_code == 200

    respuesta = cliente_crm.post(
        _url(conversacion.id), json={"texto": "llegué tarde"}, headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 409
    assert _codigo(respuesta) == CODIGO_ATENCION_NO_DISPONIBLE
    assert meta_enviados == []


def test_responder_exige_sesion_y_csrf(cliente_crm, usuario_crm, db):
    conversacion = crear_conversacion(
        db, TELEFONO_DE_PRUEBA, [(RolMensaje.USUARIO, "hola", 1)]
    )
    sin_sesion = cliente_crm.post(_url(conversacion.id), json={"texto": "hola"})
    assert sin_sesion.status_code == 401

    login_crm(cliente_crm, usuario_crm)
    sin_csrf = cliente_crm.post(_url(conversacion.id), json={"texto": "hola"})
    assert sin_csrf.status_code == 403


def test_presupuesto_agotado_no_llama_meta_ni_guarda_mensaje_o_costo(
    cliente_crm, usuario_crm, db, meta_enviados, monkeypatch
):
    monkeypatch.setattr(config, "meta_tope_duro_habilitado", True)
    monkeypatch.setattr(config, "meta_tarifa_service_ars", Decimal("50"))
    monkeypatch.setattr(config, "meta_presupuesto_mensual_ars", Decimal("10"))
    conversacion, csrf = _preparar_atencion(cliente_crm, usuario_crm, db)

    respuesta = cliente_crm.post(
        _url(conversacion.id), json={"texto": "respuesta"}, headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 409
    assert _codigo(respuesta) == CODIGO_PRESUPUESTO_AGOTADO
    assert meta_enviados == []
    assert db.query(Mensaje).filter_by(conversacion_id=conversacion.id, rol=RolMensaje.HUMANO).count() == 0
    assert db.query(EnvioWhatsapp).count() == 0


def test_fallo_meta_no_guarda_y_libera_reserva(cliente_crm, usuario_crm, db, monkeypatch):
    monkeypatch.setattr(config, "meta_tope_duro_habilitado", True)
    monkeypatch.setattr(config, "meta_tarifa_service_ars", Decimal("50"))
    monkeypatch.setattr(config, "meta_presupuesto_mensual_ars", Decimal("1000"))
    conversacion, csrf = _preparar_atencion(cliente_crm, usuario_crm, db)

    def meta_caido(telefono, texto):
        raise RuntimeError("Meta no responde")

    monkeypatch.setattr(envio_mod.meta_client, "enviar_mensaje_texto", meta_caido)
    respuesta = cliente_crm.post(
        _url(conversacion.id), json={"texto": "respuesta"}, headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 502
    assert _codigo(respuesta) == CODIGO_FALLO_META
    assert db.query(Mensaje).filter_by(conversacion_id=conversacion.id, rol=RolMensaje.HUMANO).count() == 0
    fila = db.query(PresupuestoMetaMensual).filter_by(mes=mes_actual(datetime.now(timezone.utc))).one()
    db.refresh(fila)
    assert fila.costo_comprometido_ars == Decimal("0")


def test_sin_entrante_del_usuario_bloquea_ventana(cliente_crm, usuario_crm, db, meta_enviados):
    conversacion, csrf = _preparar_atencion(
        cliente_crm,
        usuario_crm,
        db,
        mensajes=[(RolMensaje.BOT, "mensaje saliente", 1)],
    )

    respuesta = cliente_crm.post(
        _url(conversacion.id), json={"texto": "respuesta"}, headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 409
    assert _codigo(respuesta) == CODIGO_FUERA_DE_VENTANA
    assert meta_enviados == []


def test_entrante_de_24_horas_o_mas_bloquea_sin_meta(cliente_crm, usuario_crm, db, meta_enviados):
    conversacion, csrf = _preparar_atencion(
        cliente_crm,
        usuario_crm,
        db,
        mensajes=[(RolMensaje.USUARIO, "mensaje viejo", 24 * 60)],
    )

    respuesta = cliente_crm.post(
        _url(conversacion.id), json={"texto": "respuesta"}, headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 409
    assert _codigo(respuesta) == CODIGO_FUERA_DE_VENTANA
    assert meta_enviados == []


def test_bot_o_humano_reciente_no_reabre_ventana(cliente_crm, usuario_crm, db, meta_enviados):
    conversacion, csrf = _preparar_atencion(
        cliente_crm,
        usuario_crm,
        db,
        mensajes=[
            (RolMensaje.USUARIO, "usuario viejo", 25 * 60),
            (RolMensaje.BOT, "bot reciente", 2),
            (RolMensaje.HUMANO, "humano reciente", 1),
        ],
    )

    respuesta = cliente_crm.post(
        _url(conversacion.id), json={"texto": "respuesta"}, headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 409
    assert _codigo(respuesta) == CODIGO_FUERA_DE_VENTANA
    assert meta_enviados == []


def test_adjunto_entrante_reciente_reabre_ventana(cliente_crm, usuario_crm, db, meta_enviados):
    conversacion, csrf = _preparar_atencion(cliente_crm, usuario_crm, db, mensajes=[])
    db.add(
        Mensaje(
            conversacion_id=conversacion.id,
            rol=RolMensaje.USUARIO,
            contenido="[imagen]",
            tipo="image",
            creado_en=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    db.commit()

    respuesta = cliente_crm.post(
        _url(conversacion.id), json={"texto": "vi la imagen"}, headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 201
    assert len(meta_enviados) == 1


def test_resolver_gana_antes_de_revalidar_y_no_se_llama_meta(
    cliente_crm, usuario_crm, db, meta_enviados, monkeypatch
):
    conversacion, csrf = _preparar_atencion(cliente_crm, usuario_crm, db)
    original = atencion_mod.bloquear_para_responder

    def resolver_antes_de_bloquear(sesion, conversacion_id, usuario_id):
        abierta = atencion_mod.atencion_abierta(sesion, conversacion_id)
        atencion_mod.resolver(sesion, abierta, usuario_id)
        return original(sesion, conversacion_id, usuario_id)

    monkeypatch.setattr(atencion_mod, "bloquear_para_responder", resolver_antes_de_bloquear)

    respuesta = cliente_crm.post(
        _url(conversacion.id), json={"texto": "respuesta tardía"}, headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 409
    assert _codigo(respuesta) == CODIGO_ATENCION_NO_DISPONIBLE
    assert meta_enviados == []


def test_revalidacion_bloqueante_ocurre_antes_de_meta(
    cliente_crm, usuario_crm, db, monkeypatch
):
    conversacion, csrf = _preparar_atencion(cliente_crm, usuario_crm, db)
    orden = []
    original = atencion_mod.bloquear_para_responder

    def bloquear(sesion, conversacion_id, usuario_id):
        orden.append("bloqueo")
        return original(sesion, conversacion_id, usuario_id)

    def meta_acepta(telefono, texto):
        orden.append("meta")
        return {"messages": [{"id": "wamid.orden-humano"}]}

    monkeypatch.setattr(atencion_mod, "bloquear_para_responder", bloquear)
    monkeypatch.setattr(envio_mod.meta_client, "enviar_mensaje_texto", meta_acepta)

    respuesta = cliente_crm.post(
        _url(conversacion.id), json={"texto": "respuesta"}, headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 201
    assert orden == ["bloqueo", "meta"]


def test_ciclo_responder_dos_veces_resolver_y_bot_vuelve_con_el_siguiente_mensaje(
    cliente_crm, usuario_crm, db, meta_enviados, monkeypatch
):
    conversacion, csrf = _preparar_atencion(cliente_crm, usuario_crm, db)

    for texto in ("primera respuesta", "segunda respuesta"):
        assert cliente_crm.post(
            _url(conversacion.id), json={"texto": texto}, headers={HEADER_CSRF: csrf}
        ).status_code == 201
        db.expire_all()
        assert db.query(Conversacion).filter_by(id=conversacion.id).one().modo_humano is True

    enviados_antes = list(meta_enviados)
    assert cliente_crm.post(_url(conversacion.id, "resolver"), headers={HEADER_CSRF: csrf}).status_code == 200
    assert meta_enviados == enviados_antes

    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(
            texto="respuesta automática posterior", escalar=False, resumen=None
        ),
    )
    payload = payload_meta_texto("wamid.despues-resolver", TELEFONO_DE_PRUEBA, "otra consulta")
    cuerpo = json.dumps(payload).encode("utf-8")
    webhook = cliente_crm.post(
        "/webhook",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO_META)},
    )

    assert webhook.status_code == 200
    assert [texto for _, texto in meta_enviados] == [
        "primera respuesta",
        "segunda respuesta",
        "respuesta automática posterior",
    ]


def test_texto_vacio_o_demasiado_largo_da_422(cliente_crm, usuario_crm, db, meta_enviados):
    conversacion, csrf = _preparar_atencion(cliente_crm, usuario_crm, db)

    vacio_del_todo = cliente_crm.post(
        _url(conversacion.id), json={"texto": ""}, headers={HEADER_CSRF: csrf}
    )
    solo_espacios = cliente_crm.post(
        _url(conversacion.id), json={"texto": "   "}, headers={HEADER_CSRF: csrf}
    )
    largo = cliente_crm.post(
        _url(conversacion.id), json={"texto": "x" * 4097}, headers={HEADER_CSRF: csrf}
    )

    # Los tres dan 422 con el mismo {code, message} — un `Field(min_length=1)`
    # en el modelo Pydantic haría que `{"texto": ""}` rechace en la
    # validación del request, antes de correr el cuerpo del endpoint, con el
    # formato genérico de FastAPI (una lista de errores) en vez de
    # `detail.code`. Por eso `DatosRespuestaHumana` no lo usa: la validación
    # de contenido es toda manual, para que el contrato de error sea el
    # mismo sin importar qué hace inválido al texto.
    for respuesta in (vacio_del_todo, solo_espacios, largo):
        assert respuesta.status_code == 422, respuesta.text
        assert _codigo(respuesta) == CODIGO_TEXTO_INVALIDO
        assert isinstance(respuesta.json()["detail"], dict) and set(respuesta.json()["detail"]) == {
            "code", "message",
        }
    assert meta_enviados == []
