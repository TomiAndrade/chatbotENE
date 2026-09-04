"""Proveedor de respuestas usando la API de Claude (Anthropic). Pensado para
producción, con un modelo chico y rápido (Haiku) definido por MODELO.

Referencias consultadas (docs.anthropic.com, redirige a platform.claude.com):
- Tool use: /docs/en/agents-and-tools/tool-use/overview — Claude devuelve un
  bloque `tool_use` en `response.content` con `name` e `input`.
- Prompt caching: /docs/en/docs/build-with-claude/prompt-caching — el bloque
  de `system` va como lista, con `cache_control: {"type": "ephemeral"}` en el
  bloque que se quiere cachear.
"""

import logging

import anthropic

from app.config import config
from app.historial import mapear_mensaje
from app.models import Mensaje
from app.prompt import SYSTEM_PROMPT
from app.respuesta import (
    DESCRIPCION_HERRAMIENTA_ESCALAR,
    NOMBRE_HERRAMIENTA_ESCALAR,
    PARAMETROS_HERRAMIENTA_ESCALAR,
    ProveedorRespuesta,
    RespuestaGenerada,
    TIMEOUT_SEGUNDOS,
    con_un_reintento,
)

logger = logging.getLogger("proveedor_claude")

MAX_TOKENS_RESPUESTA = 1024


def _herramientas() -> list[dict]:
    """Vacía si ESCALAMIENTO_HABILITADO=false (ver specs/spec-derivacion.md):
    sin bandeja de entrada, no hay quién reciba un escalamiento."""
    if not config.escalamiento_habilitado:
        return []
    return [
        {
            "name": NOMBRE_HERRAMIENTA_ESCALAR,
            "description": DESCRIPCION_HERRAMIENTA_ESCALAR,
            "input_schema": PARAMETROS_HERRAMIENTA_ESCALAR,
        }
    ]


class ProveedorClaude(ProveedorRespuesta):
    def __init__(self) -> None:
        # max_retries=0: el reintento propio (con_un_reintento) ya cubre esto;
        # no queremos que el SDK reintente por su cuenta encima.
        self._client = anthropic.Anthropic(
            api_key=config.anthropic_api_key,
            timeout=TIMEOUT_SEGUNDOS,
            max_retries=0,
        )

    def generar_respuesta(self, historial: list[Mensaje], mensaje_nuevo: str) -> RespuestaGenerada:
        mensajes = [_a_mensaje_anthropic(mensaje) for mensaje in historial]
        mensajes.append({"role": "user", "content": mensaje_nuevo})

        respuesta = con_un_reintento(
            lambda: self._client.messages.create(
                model=config.modelo,
                max_tokens=MAX_TOKENS_RESPUESTA,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=_herramientas(),
                messages=mensajes,
            )
        )

        return _interpretar_respuesta(respuesta)


def _a_mensaje_anthropic(mensaje: Mensaje) -> dict:
    rol_logico, texto = mapear_mensaje(mensaje)
    rol_anthropic = "user" if rol_logico == "usuario" else "assistant"
    return {"role": rol_anthropic, "content": texto}


def _interpretar_respuesta(respuesta) -> RespuestaGenerada:
    """Junta los bloques `text` en un solo string y detecta si hay un bloque
    `tool_use` de escalar_a_humano. Si hay texto y además se llamó a la
    herramienta, se devuelven ambos (ver spec-etapa2.md)."""
    texto: str | None = None
    escalar = False
    resumen = None

    for bloque in respuesta.content:
        if bloque.type == "text" and bloque.text:
            texto = (texto or "") + bloque.text
        elif bloque.type == "tool_use" and bloque.name == NOMBRE_HERRAMIENTA_ESCALAR:
            if not config.escalamiento_habilitado:
                logger.warning(
                    "El modelo llamó a escalar_a_humano con ESCALAMIENTO_HABILITADO=false; se ignora."
                )
                continue
            escalar = True
            resumen = bloque.input.get("resumen")

    return RespuestaGenerada(texto=texto, escalar=escalar, resumen=resumen)
