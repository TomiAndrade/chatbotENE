"""Que responder() (app/main.py) registre una fila en LlamadaIA por cada
llamada a generar_respuesta(), éxito o fracaso — es la fuente del dashboard
de costos y actividad del CRM (specs/spec-dashboard-metricas.md).

Mismo patrón que tests/test_fallo_modelo.py y tests/test_error_transitorio.py:
un POST de webhook con generar_respuesta mockeado, y después se mira lo que
quedó en la base.
"""

import json

import pytest

from app import main as main_mod
from app.config import config
from app.db import SessionLocal
from app.models import Conversacion, LlamadaIA, ResultadoLlamadaIA
from app.respuesta import ErrorTransitorioProveedor, RespuestaGenerada
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import firmar_meta, payload_meta_texto

SECRETO = "test-app-secret"


def _post_mensaje(client, wa_message_id: str, texto: str):
    payload = payload_meta_texto(wa_message_id, TELEFONO_DE_PRUEBA, texto)
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)}
    )


def _unica_llamada_ia() -> LlamadaIA:
    db = SessionLocal()
    try:
        conversacion = db.query(Conversacion).filter_by(identificador_externo=TELEFONO_DE_PRUEBA).one()
        return db.query(LlamadaIA).filter_by(conversacion_id=conversacion.id).one()
    finally:
        db.close()


def test_respuesta_exitosa_registra_proveedor_modelo_y_tokens(client, meta_enviados, monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(
            texto="el precio es $85.000", escalar=False, resumen=None,
            tokens_entrada=120, tokens_salida=40,
        ),
    )

    _post_mensaje(client, "wamid.llamada-ok", "hola")

    llamada = _unica_llamada_ia()
    assert llamada.proveedor == "fijo"  # config.proveedor_ia en la suite (ver tests/conftest.py)
    assert llamada.modelo == "modelo-de-test"
    assert llamada.resultado == ResultadoLlamadaIA.OK
    assert llamada.escalo is False
    assert llamada.tokens_entrada == 120
    assert llamada.tokens_salida == 40
    assert llamada.duracion_ms is not None


def test_respuesta_sin_tokens_informados_queda_en_null(client, meta_enviados, monkeypatch):
    """ProveedorFijo (y cualquier openai_compat sin "usage") no informa uso:
    el dashboard tiene que poder distinguir "cero tokens" de "no sabemos"."""
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto="ok", escalar=False, resumen=None),
    )

    _post_mensaje(client, "wamid.llamada-sin-tokens", "hola")

    llamada = _unica_llamada_ia()
    assert llamada.tokens_entrada is None
    assert llamada.tokens_salida is None


def test_respuesta_que_escala_marca_escalo_true(escalamiento_activo, client, meta_enviados, monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(
            texto="te paso con alguien", escalar=True, resumen="quiere reservar",
        ),
    )

    _post_mensaje(client, "wamid.llamada-escala", "hola")

    llamada = _unica_llamada_ia()
    assert llamada.resultado == ResultadoLlamadaIA.OK
    assert llamada.escalo is True


def test_fallo_del_modelo_registra_resultado_error_y_escalo(escalamiento_activo, client, meta_enviados, monkeypatch):
    def _reventar(historial, mensaje_nuevo):
        raise TimeoutError("el modelo tardó demasiado")

    monkeypatch.setattr(main_mod, "generar_respuesta", _reventar)

    _post_mensaje(client, "wamid.llamada-error", "hola")

    llamada = _unica_llamada_ia()
    assert llamada.resultado == ResultadoLlamadaIA.ERROR
    assert llamada.escalo is True
    assert llamada.tokens_entrada is None


def test_respuesta_vacia_sin_escalar_registra_resultado_vacio(escalamiento_activo, client, meta_enviados, monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "generar_respuesta",
        lambda historial, mensaje_nuevo: RespuestaGenerada(texto=None, escalar=False, resumen=None),
    )

    _post_mensaje(client, "wamid.llamada-vacia", "hola")

    llamada = _unica_llamada_ia()
    assert llamada.resultado == ResultadoLlamadaIA.VACIO
    assert llamada.escalo is True


@pytest.mark.parametrize("debug,escala_esperado", [(True, False), (False, True)])
def test_error_transitorio_escala_solo_fuera_de_debug(
    client, meta_enviados, monkeypatch, debug, escala_esperado
):
    """Mismo criterio que responder() (ver spec-etapa2.md): en DEBUG un error
    transitorio del proveedor solo pide reintentar, no escala. La fila de
    LlamadaIA tiene que reflejar lo que realmente pasó en cada caso."""
    monkeypatch.setattr(config, "debug", debug)
    monkeypatch.setattr(config, "escalamiento_habilitado", not debug)

    def _reventar_transitorio(historial, mensaje_nuevo):
        raise ErrorTransitorioProveedor("529 overloaded")

    monkeypatch.setattr(main_mod, "generar_respuesta", _reventar_transitorio)

    _post_mensaje(client, f"wamid.llamada-transitorio-{debug}", "hola")

    llamada = _unica_llamada_ia()
    assert llamada.resultado == ResultadoLlamadaIA.ERROR_TRANSITORIO
    assert llamada.escalo is escala_esperado
