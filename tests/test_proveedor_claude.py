"""Proveedor claude: parseo de la respuesta y armado del request.

No le pega a la API real: se reemplaza `self._client` por un doble que
registra con qué argumentos lo llamaron y devuelve una respuesta armada a
mano con la forma que documenta Anthropic (`response.content` como lista de
bloques con `.type`, y `.text` o `.name`/`.input` según el bloque).

Es el proveedor de producción, así que lo que más importa acá es que el
bloque de sistema siga llevando `cache_control` — el spec-etapa2.md lo exige
explícitamente y es lo que hace que el system prompt (10k+ tokens) no se
pague entero en cada mensaje.
"""

from types import SimpleNamespace

import pytest

from app.config import config
from app.models import Mensaje, RolMensaje
from app.prompt import SYSTEM_PROMPT
from app.respuesta import RespuestaGenerada


@pytest.fixture(autouse=True)
def _config_de_test(monkeypatch):
    monkeypatch.setattr(config, "anthropic_api_key", "test-key")
    monkeypatch.setattr(config, "modelo", "modelo-de-test")


def _bloque_texto(texto: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=texto)


def _bloque_tool_use(nombre: str, entrada: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=nombre, input=entrada)


def _respuesta(bloques) -> SimpleNamespace:
    return SimpleNamespace(content=bloques)


# --- Parseo de la respuesta, sin red ----------------------------------------


def test_un_solo_bloque_de_texto():
    from app.proveedor_claude import _interpretar_respuesta

    resultado = _interpretar_respuesta(_respuesta([_bloque_texto("el precio es $85.000")]))

    assert resultado == RespuestaGenerada(texto="el precio es $85.000", escalar=False, resumen=None)


def test_varios_bloques_de_texto_se_concatenan():
    from app.proveedor_claude import _interpretar_respuesta

    resultado = _interpretar_respuesta(
        _respuesta([_bloque_texto("hola, "), _bloque_texto("¿en qué te ayudo?")])
    )

    assert resultado.texto == "hola, ¿en qué te ayudo?"
    assert resultado.escalar is False


def test_tool_use_prende_escalar_y_trae_el_resumen():
    from app.proveedor_claude import _interpretar_respuesta

    resultado = _interpretar_respuesta(
        _respuesta([_bloque_tool_use("escalar_a_humano", {"resumen": "quiere alquilar una sala"})])
    )

    assert resultado.texto is None
    assert resultado.escalar is True
    assert resultado.resumen == "quiere alquilar una sala"


def test_texto_y_tool_use_juntos_devuelven_los_dos():
    """spec-etapa2.md: si el modelo devuelve texto y además llama a la
    herramienta, se envían ambos — primero su texto, después el aviso."""
    from app.proveedor_claude import _interpretar_respuesta

    resultado = _interpretar_respuesta(
        _respuesta(
            [
                _bloque_texto("Dale, te paso con alguien."),
                _bloque_tool_use("escalar_a_humano", {"resumen": "pidió hablar con una persona"}),
            ]
        )
    )

    assert resultado.texto == "Dale, te paso con alguien."
    assert resultado.escalar is True
    assert resultado.resumen == "pidió hablar con una persona"


def test_tool_use_de_otra_herramienta_no_escala():
    from app.proveedor_claude import _interpretar_respuesta

    resultado = _interpretar_respuesta(
        _respuesta([_bloque_tool_use("otra_herramienta", {"algo": "x"})])
    )

    assert resultado.escalar is False
    assert resultado.resumen is None


def test_tool_use_sin_resumen_escala_igual_con_resumen_none():
    """Perder el resumen es peor para quien atienda, pero perder el
    escalamiento es peor todavía (ver spec-etapa2.md)."""
    from app.proveedor_claude import _interpretar_respuesta

    resultado = _interpretar_respuesta(_respuesta([_bloque_tool_use("escalar_a_humano", {})]))

    assert resultado.escalar is True
    assert resultado.resumen is None


def test_respuesta_sin_bloques_queda_vacia_sin_escalar():
    """main.responder trata este caso como error y escala por su cuenta."""
    from app.proveedor_claude import _interpretar_respuesta

    resultado = _interpretar_respuesta(_respuesta([]))

    assert resultado == RespuestaGenerada(texto=None, escalar=False, resumen=None)


# --- Armado del request -----------------------------------------------------


class _ClienteFalso:
    """Doble de anthropic.Anthropic: registra el kwargs de messages.create y
    devuelve una respuesta fija."""

    def __init__(self, respuesta):
        self.llamadas = []
        self.messages = SimpleNamespace(create=self._create)
        self._respuesta = respuesta

    def _create(self, **kwargs):
        self.llamadas.append(kwargs)
        return self._respuesta


def _proveedor_con_cliente_falso(respuesta):
    from app.proveedor_claude import ProveedorClaude

    proveedor = ProveedorClaude()
    cliente = _ClienteFalso(respuesta)
    proveedor._client = cliente
    return proveedor, cliente


def _mensaje(rol: RolMensaje, contenido: str) -> Mensaje:
    """Un Mensaje suelto, sin base: mapear_mensaje solo mira rol y contenido."""
    return Mensaje(rol=rol, contenido=contenido)


def test_el_bloque_de_sistema_lleva_cache_control():
    """spec-etapa2.md, "Prompt": en claude el caché va explícito, con
    cache_control sobre el bloque de sistema. Sin esto se paga el system
    prompt entero (10k+ tokens) en cada mensaje."""
    proveedor, cliente = _proveedor_con_cliente_falso(_respuesta([_bloque_texto("ok")]))

    proveedor.generar_respuesta(historial=[], mensaje_nuevo="hola")

    system = cliente.llamadas[0]["system"]
    assert isinstance(system, list), "el system tiene que ir como lista de bloques para poder cachear"
    assert system[0]["text"] == SYSTEM_PROMPT
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_el_request_lleva_la_herramienta_y_el_modelo_configurado():
    proveedor, cliente = _proveedor_con_cliente_falso(_respuesta([_bloque_texto("ok")]))

    proveedor.generar_respuesta(historial=[], mensaje_nuevo="hola")

    kwargs = cliente.llamadas[0]
    assert kwargs["model"] == "modelo-de-test"
    assert [herramienta["name"] for herramienta in kwargs["tools"]] == ["escalar_a_humano"]
    assert "resumen" in kwargs["tools"][0]["input_schema"]["properties"]


def test_el_historial_se_mapea_a_roles_de_anthropic_con_el_marcador_humano():
    from app.historial import MARCADOR_HUMANO

    proveedor, cliente = _proveedor_con_cliente_falso(_respuesta([_bloque_texto("ok")]))
    historial = [
        _mensaje(RolMensaje.USUARIO, "hola"),
        _mensaje(RolMensaje.BOT, "¡hola! ¿en qué te ayudo?"),
        _mensaje(RolMensaje.HUMANO, "te confirmo la sala mañana"),
    ]

    proveedor.generar_respuesta(historial=historial, mensaje_nuevo="¿alguna novedad?")

    assert cliente.llamadas[0]["messages"] == [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "¡hola! ¿en qué te ayudo?"},
        {"role": "assistant", "content": MARCADOR_HUMANO},
        {"role": "user", "content": "¿alguna novedad?"},
    ]


def test_generar_respuesta_devuelve_la_respuesta_interpretada():
    proveedor, _ = _proveedor_con_cliente_falso(
        _respuesta([_bloque_tool_use("escalar_a_humano", {"resumen": "un reclamo"})])
    )

    resultado = proveedor.generar_respuesta(historial=[], mensaje_nuevo="tengo un problema")

    assert resultado.escalar is True
    assert resultado.resumen == "un reclamo"
