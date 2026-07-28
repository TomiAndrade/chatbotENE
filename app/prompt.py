"""Arma el system prompt una sola vez al importar este módulo: lee
prompts/system-prompt.md y reemplaza {{KNOWLEDGE_BASE}} por el contenido de
prompts/knowledge-base.md. No se vuelve a leer del disco en cada mensaje.

Los bloques `[PENDIENTE]` del knowledge base se dejan tal cual: le indican al
modelo qué no sabe, que es lo que necesita para escalar en vez de inventar.
"""

from pathlib import Path

_DIR_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
_PLACEHOLDER = "{{KNOWLEDGE_BASE}}"


def _armar_system_prompt() -> str:
    system_prompt = (_DIR_PROMPTS / "system-prompt.md").read_text(encoding="utf-8")
    knowledge_base = (_DIR_PROMPTS / "knowledge-base.md").read_text(encoding="utf-8")
    return system_prompt.replace(_PLACEHOLDER, knowledge_base)


SYSTEM_PROMPT = _armar_system_prompt()
