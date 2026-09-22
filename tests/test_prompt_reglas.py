"""Regresiones sobre el contenido de prompts/system-prompt.md y
prompts/knowledge-base.md (app/prompt.py los concatena en SYSTEM_PROMPT).

No hay forma de probar cómo responde el modelo sin pegarle a una API real,
así que estos tests no verifican comportamiento del bot: verifican que el
texto que le mandamos como instrucciones siga teniendo las reglas y los
datos que se corrigieron en la revisión de 2026-09-22 (ver AGENTS.md/
CLAUDE.md de esa fecha), para que un cambio futuro del prompt no las borre
sin que nadie se dé cuenta.

- Un "ok"/"gracias"/"dale" no debe abrir un tema nuevo si no había una
  pregunta concreta pendiente (antes, un "ok" después de la info del CV
  hacía que el bot ofreciera el formulario de postulación).
- El bot no redacta mails ni CVs por el usuario (regla ya vigente, se
  protege acá para que no se pierda).
- El knowledge base no promete una confirmación de recepción de CV que no
  existe (antes decía "confirmamos su recepción").
- La cantidad de verticales del encabezado tiene que coincidir con la
  lista real (antes decía "(12)" con una lista de 11).
"""

import re
from pathlib import Path

_DIR_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def _texto_system_prompt() -> str:
    return (_DIR_PROMPTS / "system-prompt.md").read_text(encoding="utf-8")


def _texto_knowledge_base() -> str:
    return (_DIR_PROMPTS / "knowledge-base.md").read_text(encoding="utf-8")


def test_acuse_de_recibo_no_abre_tema_nuevo():
    texto = _texto_system_prompt().lower()

    for palabra in ("ok", "gracias", "dale", "perfecto", "entendido"):
        assert palabra in texto, f"falta '{palabra}' entre los acuses de recibo cubiertos"

    assert "acuse de recibo" in texto
    assert "no abre un tema nuevo" in texto
    # La excepción de continuidad (pregunta pendiente del bot) tiene que seguir explícita.
    assert "pregunta concreta" in texto


def test_no_redacta_mails_ni_cvs_por_el_usuario():
    texto = _texto_system_prompt()
    assert "No ofrezcas ni hagas trabajos por el usuario" in texto
    assert "redactar mails" in texto


def test_no_promete_confirmacion_de_recepcion_de_cv_no_respaldada():
    texto = _texto_knowledge_base()
    assert "confirmamos su recepción" not in texto
    assert "confirmamos recepción" not in texto
    assert "no hay una confirmación automática de que el cv llegó" in texto.lower()


def test_cantidad_de_verticales_coincide_con_la_lista():
    texto = _texto_knowledge_base()

    encabezado = re.search(r"## 3\. Verticales del laboratorio \((\d+)\)", texto)
    assert encabezado, "no encontré el encabezado de la sección de verticales"
    cantidad_declarada = int(encabezado.group(1))

    lista_match = re.search(
        r"## 3\. Verticales del laboratorio.*?\n\n(.+?)\n\n", texto, re.DOTALL
    )
    assert lista_match, "no encontré la lista de verticales debajo del encabezado"
    verticales = [v.strip() for v in lista_match.group(1).split("·") if v.strip()]

    assert cantidad_declarada == len(verticales), (
        f"el encabezado dice {cantidad_declarada} verticales pero la lista tiene "
        f"{len(verticales)}: {verticales}"
    )
