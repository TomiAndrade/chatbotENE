"""Proveedor de respuestas usando la API de Gemini (Google). Pensado para
desarrollo: free tier, sin tarjeta.

Referencias consultadas (ai.google.dev, vía el SDK google-genai):
- Function calling: /gemini-api/docs/generate-content/function-calling — la
  herramienta se declara como dict dentro de `types.Tool(function_declarations=
  [...])`, se pasa por `GenerateContentConfig.tools`, y hay que desactivar
  `automatic_function_calling` para que el SDK no intente resolver la llamada
  él solo (acá no implementamos el bucle completo, ver spec-etapa2.md). La
  respuesta trae la llamada en `candidates[0].content.parts[i].function_call`.
- Caché de prompt: /gemini-api/docs/caching — la caché implícita está
  habilitada por default recién desde Gemini 2.5 en adelante; gemini-2.0-flash
  (el modelo por defecto de esta etapa, ver .env.example) no entra en esa
  lista, así que no hay nada que activar acá para ese modelo.
"""

from google import genai
from google.genai import types

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

_HERRAMIENTA_ESCALAR = {
    "name": NOMBRE_HERRAMIENTA_ESCALAR,
    "description": DESCRIPCION_HERRAMIENTA_ESCALAR,
    "parameters": PARAMETROS_HERRAMIENTA_ESCALAR,
}


class ProveedorGemini(ProveedorRespuesta):
    def __init__(self) -> None:
        self._client = genai.Client(
            api_key=config.gemini_api_key,
            http_options=types.HttpOptions(timeout=int(TIMEOUT_SEGUNDOS * 1000)),
        )

    def generar_respuesta(self, historial: list[Mensaje], mensaje_nuevo: str) -> RespuestaGenerada:
        contenidos = [_a_contenido_gemini(mensaje) for mensaje in historial]
        contenidos.append(types.Content(role="user", parts=[types.Part(text=mensaje_nuevo)]))

        respuesta = con_un_reintento(
            lambda: self._client.models.generate_content(
                model=config.modelo,
                contents=contenidos,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=[types.Tool(function_declarations=[_HERRAMIENTA_ESCALAR])],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
        )

        return _interpretar_respuesta(respuesta)


def _a_contenido_gemini(mensaje: Mensaje) -> "types.Content":
    rol_logico, texto = mapear_mensaje(mensaje)
    rol_gemini = "user" if rol_logico == "usuario" else "model"
    return types.Content(role=rol_gemini, parts=[types.Part(text=texto)])


def _interpretar_respuesta(respuesta) -> RespuestaGenerada:
    """Junta los parts de texto y detecta si hay un function_call de
    escalar_a_humano, igual que en el proveedor de Claude. Si Gemini bloqueó
    la respuesta por seguridad, `candidates` viene vacío: se trata como error
    (texto vacío y sin escalar) en la capa de arriba."""
    texto: str | None = None
    escalar = False
    resumen = None

    if not respuesta.candidates:
        return RespuestaGenerada(texto=None, escalar=False, resumen=None)

    parts = respuesta.candidates[0].content.parts or []
    for part in parts:
        llamada = getattr(part, "function_call", None)
        if llamada and llamada.name == NOMBRE_HERRAMIENTA_ESCALAR:
            escalar = True
            resumen = llamada.args.get("resumen")
        elif getattr(part, "text", None):
            texto = (texto or "") + part.text

    return RespuestaGenerada(texto=texto, escalar=escalar, resumen=resumen)
