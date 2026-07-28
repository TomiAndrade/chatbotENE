"""Política de reintentos del cliente de Kapso, con httpx.MockTransport (ver
spec-etapa1.md, "Cliente de Kapso": reintentos con backoff en errores de
red, 429 y 5xx; el resto de los 4xx falla al primer intento)."""

import httpx
import pytest

from app import kapso
from app.kapso import KapsoClient


@pytest.fixture(autouse=True)
def _sin_espera_real(monkeypatch):
    """El backoff real (1s, 2s) haría los tests lentos sin aportar nada:
    lo que se verifica es cuántas veces se reintenta, no cuánto se espera."""
    monkeypatch.setattr(kapso.time, "sleep", lambda segundos: None)


def _cliente_con_transporte(handler) -> KapsoClient:
    cliente = KapsoClient()
    cliente._http = httpx.Client(transport=httpx.MockTransport(handler))
    return cliente


def test_reintenta_en_error_de_red_y_despues_funciona():
    llamadas = {"cantidad": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas["cantidad"] += 1
        if llamadas["cantidad"] < 3:
            raise httpx.ConnectError("no se pudo conectar", request=request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.ok"}]})

    cliente = _cliente_con_transporte(handler)
    resultado = cliente.enviar_mensaje_texto("5491100000000", "hola")

    assert resultado["messages"][0]["id"] == "wamid.ok"
    assert llamadas["cantidad"] == 3


def test_reintenta_en_429():
    llamadas = {"cantidad": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas["cantidad"] += 1
        if llamadas["cantidad"] == 1:
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.ok"}]})

    cliente = _cliente_con_transporte(handler)
    cliente.enviar_mensaje_texto("5491100000000", "hola")

    assert llamadas["cantidad"] == 2


def test_reintenta_en_5xx():
    llamadas = {"cantidad": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas["cantidad"] += 1
        if llamadas["cantidad"] == 1:
            return httpx.Response(503, json={"error": "no disponible"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.ok"}]})

    cliente = _cliente_con_transporte(handler)
    cliente.enviar_mensaje_texto("5491100000000", "hola")

    assert llamadas["cantidad"] == 2


def test_no_reintenta_en_4xx_que_no_es_429():
    llamadas = {"cantidad": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas["cantidad"] += 1
        return httpx.Response(401, json={"error": "api key inválida"})

    cliente = _cliente_con_transporte(handler)
    with pytest.raises(httpx.HTTPStatusError):
        cliente.enviar_mensaje_texto("5491100000000", "hola")

    assert llamadas["cantidad"] == 1


def test_agota_los_reintentos_y_lanza():
    llamadas = {"cantidad": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas["cantidad"] += 1
        return httpx.Response(500, json={"error": "error del servidor"})

    cliente = _cliente_con_transporte(handler)
    with pytest.raises(httpx.HTTPStatusError):
        cliente.enviar_mensaje_texto("5491100000000", "hola")

    assert llamadas["cantidad"] == kapso.MAX_REINTENTOS
