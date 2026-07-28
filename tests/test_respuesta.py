"""Capa de despacho de proveedores y política de reintento compartida
(ver app/respuesta.py)."""

import pytest

from app.config import config
from app.respuesta import RespuestaGenerada, con_un_reintento, generar_respuesta


def test_proveedor_desconocido_lanza_error(monkeypatch):
    monkeypatch.setattr(config, "proveedor_ia", "no-existe")

    with pytest.raises(ValueError):
        generar_respuesta(historial=[], mensaje_nuevo="hola")


def test_proveedor_fijo_devuelve_respuesta_generada():
    monkeypatch_proveedor = config.proveedor_ia
    assert monkeypatch_proveedor == "fijo"

    resultado = generar_respuesta(historial=[], mensaje_nuevo="hola")

    assert isinstance(resultado, RespuestaGenerada)
    assert resultado.escalar is False
    assert resultado.texto is not None


def test_con_un_reintento_reintenta_una_vez_y_despues_funciona():
    intentos = {"cantidad": 0}

    def funcion():
        intentos["cantidad"] += 1
        if intentos["cantidad"] == 1:
            raise RuntimeError("falla transitoria")
        return "ok"

    assert con_un_reintento(funcion) == "ok"
    assert intentos["cantidad"] == 2


def test_con_un_reintento_no_reintenta_mas_de_una_vez():
    intentos = {"cantidad": 0}

    def funcion_que_siempre_falla():
        intentos["cantidad"] += 1
        raise RuntimeError("falla persistente")

    with pytest.raises(RuntimeError):
        con_un_reintento(funcion_que_siempre_falla)

    assert intentos["cantidad"] == 2
