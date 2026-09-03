"""Proveedor de respuestas para cualquier endpoint compatible con el formato
de la API de OpenAI (chat completions + tool calling en ese formato):
OpenRouter, DeepSeek, el endpoint gratuito de NVIDIA, un modelo local, etc.
Parametrizado por BASE_URL — no está atado a un servicio concreto.

Referencias consultadas (formato "OpenAI chat completions", el mismo que
implementan todos los servicios de arriba):
- Chat completions: POST {BASE_URL}/chat/completions, header
  "Authorization: Bearer {api_key}", payload con "model", "messages"
  (roles "system"/"user"/"assistant") y, para tool calling, "tools" como
  lista de {"type": "function", "function": {...}}.
- La respuesta trae la llamada a la herramienta en
  choices[0].message.tool_calls[i].function, con "arguments" como **string**
  con JSON adentro (no un objeto) — a diferencia de Claude, cuyo SDK ya
  entrega un dict en bloque.input, acá hace falta json.loads. Confirmado
  contra un spike real (ver PENDIENTES.md, sección 2).
- Un servicio detrás de BASE_URL puede devolver un error de upstream dentro
  de un HTTP 200, con la clave "error" en el body (confirmado con OpenRouter,
  ver PENDIENTES.md). Por eso se chequea esa clave antes de asumir que la
  respuesta es válida.

Caché de prompt: depende del proveedor detrás de BASE_URL (algunos lo hacen
automático, otros no lo exponen) y queda opcional acá — no se implementa
nada específico para activarlo.
"""

import json
import logging

import httpx

from app.config import config
from app.historial import mapear_mensaje
from app.models import Mensaje
from app.prompt import SYSTEM_PROMPT
from app.respuesta import (
    DESCRIPCION_HERRAMIENTA_ESCALAR,
    ErrorTransitorioProveedor,
    NOMBRE_HERRAMIENTA_ESCALAR,
    PARAMETROS_HERRAMIENTA_ESCALAR,
    ProveedorRespuesta,
    RespuestaGenerada,
    TIMEOUT_SEGUNDOS,
    con_un_reintento,
)

logger = logging.getLogger("proveedor_openai_compat")

def _herramientas() -> list[dict]:
    """Vacía si ESCALAMIENTO_HABILITADO=false (ver specs/spec-derivacion.md):
    sin bandeja de entrada, no hay quién reciba un escalamiento."""
    if not config.escalamiento_habilitado:
        return []
    return [
        {
            "type": "function",
            "function": {
                "name": NOMBRE_HERRAMIENTA_ESCALAR,
                "description": DESCRIPCION_HERRAMIENTA_ESCALAR,
                "parameters": PARAMETROS_HERRAMIENTA_ESCALAR,
            },
        }
    ]


class ProveedorOpenAICompat(ProveedorRespuesta):
    def __init__(self) -> None:
        self._http = httpx.Client(timeout=TIMEOUT_SEGUNDOS)
        self._url = f"{config.base_url.rstrip('/')}/chat/completions"

    def generar_respuesta(self, historial: list[Mensaje], mensaje_nuevo: str) -> RespuestaGenerada:
        mensajes = [{"role": "system", "content": SYSTEM_PROMPT}]
        mensajes.extend(_a_mensaje_openai(mensaje) for mensaje in historial)
        mensajes.append({"role": "user", "content": mensaje_nuevo})

        cuerpo = con_un_reintento(lambda: self._llamar(mensajes))
        return _interpretar_respuesta(cuerpo)

    def _llamar(self, mensajes: list[dict]) -> dict:
        respuesta = self._http.post(
            self._url,
            headers={
                "Authorization": f"Bearer {config.openai_compat_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.modelo,
                "messages": mensajes,
                "tools": _herramientas(),
            },
        )

        try:
            respuesta.raise_for_status()
        except httpx.HTTPStatusError as error:
            codigo = error.response.status_code
            if codigo == 429 or codigo >= 500:
                raise ErrorTransitorioProveedor(str(error)) from error
            raise

        cuerpo = respuesta.json()

        # El proveedor detrás de BASE_URL puede devolver el error adentro de
        # un 200 (ver PENDIENTES.md, sección 2): sin este chequeo, el parseo
        # de más abajo no encuentra "choices" y esto se trataría como
        # respuesta vacía en vez de como el error que es.
        if "error" in cuerpo:
            raise ErrorTransitorioProveedor(f"Proveedor devolvió error en el body: {cuerpo['error']}")

        return cuerpo


def _a_mensaje_openai(mensaje: Mensaje) -> dict:
    rol_logico, texto = mapear_mensaje(mensaje)
    rol_openai = "user" if rol_logico == "usuario" else "assistant"
    return {"role": rol_openai, "content": texto}


def _interpretar_respuesta(cuerpo: dict) -> RespuestaGenerada:
    """Junta el texto y detecta si hay un tool call de escalar_a_humano.
    Si el tool call llegó pero function.arguments no parsea como JSON (o
    parsea pero no trae "resumen"), se escala igual: un JSONDecodeError no
    puede tumbar el request, y perder el escalamiento es peor que perder
    el resumen."""
    choices = cuerpo.get("choices") or []
    if not choices:
        return RespuestaGenerada(texto=None, escalar=False, resumen=None)

    mensaje = choices[0].get("message") or {}
    texto = mensaje.get("content")
    escalar = False
    resumen = None

    for llamada in mensaje.get("tool_calls") or []:
        funcion = llamada.get("function") or {}
        if funcion.get("name") != NOMBRE_HERRAMIENTA_ESCALAR:
            continue
        if not config.escalamiento_habilitado:
            logger.warning(
                "El modelo llamó a escalar_a_humano con ESCALAMIENTO_HABILITADO=false; se ignora."
            )
            continue
        escalar = True
        try:
            argumentos = json.loads(funcion.get("arguments") or "{}")
            resumen = argumentos.get("resumen")
        except json.JSONDecodeError:
            logger.warning(
                "No se pudo parsear function.arguments de escalar_a_humano: %r",
                funcion.get("arguments"),
            )
            resumen = None

    return RespuestaGenerada(texto=texto, escalar=escalar, resumen=resumen)
