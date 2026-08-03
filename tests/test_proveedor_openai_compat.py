"""Proveedor openai_compat: parseo de tool calls y detección de errores (ver
spec-etapa2.md, "Manejo de errores", y PENDIENTES.md, sección 2 — hallazgos
del spike real contra OpenRouter). No le pega a ninguna API real: se
reemplaza el cliente HTTP por uno con httpx.MockTransport."""

import httpx
import pytest

from app.config import config
from app.proveedor_openai_compat import ProveedorOpenAICompat, _interpretar_respuesta
from app.respuesta import ErrorTransitorioProveedor


@pytest.fixture(autouse=True)
def _config_de_test(monkeypatch):
    monkeypatch.setattr(config, "base_url", "https://api.example.test/v1")
    monkeypatch.setattr(config, "openai_compat_api_key", "test-key")
    monkeypatch.setattr(config, "modelo", "modelo-de-test")


def _proveedor_con_transporte(handler) -> ProveedorOpenAICompat:
    proveedor = ProveedorOpenAICompat()
    proveedor._http = httpx.Client(transport=httpx.MockTransport(handler))
    return proveedor


# --- Parseo de la respuesta (choices[0].message), sin red -------------------


def test_tool_call_bien_formado_devuelve_escalar_y_resumen():
    cuerpo = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "function": {
                                "name": "escalar_a_humano",
                                "arguments": '{"resumen": "quiere alquilar una sala"}',
                            }
                        }
                    ],
                }
            }
        ]
    }

    resultado = _interpretar_respuesta(cuerpo)

    assert resultado.texto is None
    assert resultado.escalar is True
    assert resultado.resumen == "quiere alquilar una sala"


def test_arguments_mal_formado_escala_igual_con_resumen_none():
    cuerpo = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"function": {"name": "escalar_a_humano", "arguments": "esto no es json"}}
                    ],
                }
            }
        ]
    }

    resultado = _interpretar_respuesta(cuerpo)

    assert resultado.escalar is True
    assert resultado.resumen is None


def test_solo_texto_sin_tool_call():
    cuerpo = {"choices": [{"message": {"content": "el precio es $85.000", "tool_calls": []}}]}

    resultado = _interpretar_respuesta(cuerpo)

    assert resultado.texto == "el precio es $85.000"
    assert resultado.escalar is False
    assert resultado.resumen is None


# --- Detección de errores en la llamada HTTP --------------------------------


def test_error_adentro_de_un_200_levanta_error_transitorio():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "Internal error", "code": 502}})

    proveedor = _proveedor_con_transporte(handler)

    with pytest.raises(ErrorTransitorioProveedor):
        proveedor._llamar([{"role": "user", "content": "hola"}])


def test_429_levanta_error_transitorio():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limit"})

    proveedor = _proveedor_con_transporte(handler)

    with pytest.raises(ErrorTransitorioProveedor):
        proveedor._llamar([{"role": "user", "content": "hola"}])


def test_5xx_levanta_error_transitorio():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "no disponible"})

    proveedor = _proveedor_con_transporte(handler)

    with pytest.raises(ErrorTransitorioProveedor):
        proveedor._llamar([{"role": "user", "content": "hola"}])


def test_4xx_que_no_es_429_no_es_error_transitorio():
    """Un 401 (api key inválida) o similar no es transitorio: no se lo
    disfraza de "reintentá más tarde", sigue el camino de error genérico."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "api key inválida"})

    proveedor = _proveedor_con_transporte(handler)

    with pytest.raises(httpx.HTTPStatusError):
        proveedor._llamar([{"role": "user", "content": "hola"}])
