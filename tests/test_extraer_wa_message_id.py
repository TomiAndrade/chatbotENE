"""app.meta.extraer_wa_message_id: tiene que devolver el wa_message_id real
de una respuesta bien formada de Meta, y `None` (nunca una excepción) ante
cualquier forma inesperada — la respuesta de un servicio externo no es algo
en lo que se pueda confiar a ciegas, aunque el contrato documentado diga que
siempre trae `messages[0].id` en un 2xx.
"""

import pytest

from app.meta import extraer_wa_message_id


def test_respuesta_bien_formada_devuelve_el_id():
    assert extraer_wa_message_id({"messages": [{"id": "wamid.123"}]}) == "wamid.123"


def test_respuesta_con_mas_campos_igual_devuelve_el_id():
    """El contrato real de Meta trae más campos (`messaging_product`,
    `contacts`, etc.) — no hay que exigir una forma exacta, solo que estén
    los que se usan."""
    respuesta = {
        "messaging_product": "whatsapp",
        "contacts": [{"input": "5491100000000", "wa_id": "5491100000000"}],
        "messages": [{"id": "wamid.456"}],
    }
    assert extraer_wa_message_id(respuesta) == "wamid.456"


@pytest.mark.parametrize(
    "respuesta",
    [
        pytest.param({}, id="sin_messages"),
        pytest.param({"messages": None}, id="messages_none"),
        pytest.param({"messages": "wamid.123"}, id="messages_string"),
        pytest.param({"messages": {"id": "wamid.123"}}, id="messages_dict"),
        pytest.param({"messages": 123}, id="messages_numero"),
        pytest.param({"messages": []}, id="messages_vacia"),
        pytest.param({"messages": ["wamid.123"]}, id="primer_elemento_string"),
        pytest.param({"messages": [None]}, id="primer_elemento_none"),
        pytest.param({"messages": [["wamid.123"]]}, id="primer_elemento_lista"),
        pytest.param({"messages": [{}]}, id="sin_id"),
        pytest.param({"messages": [{"id": None}]}, id="id_none"),
        pytest.param({"messages": [{"id": 123}]}, id="id_numero"),
        pytest.param({"messages": [{"id": ""}]}, id="id_vacio"),
        pytest.param(None, id="respuesta_none"),
        pytest.param([], id="respuesta_lista"),
        pytest.param("wamid.123", id="respuesta_string"),
    ],
)
def test_respuesta_malformada_devuelve_none_sin_excepcion(respuesta):
    assert extraer_wa_message_id(respuesta) is None
