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
import threading
import time

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

# Techo de seguridad, no el mecanismo para acortar respuestas: de la brevedad
# se encarga el system prompt (spec-respuestas-cortas.md, regla 3, que pide
# "margen suficiente para no cortar una respuesta legítima").
#
# El margen tiene que contar los tokens de RAZONAMIENTO, que salen del mismo
# presupuesto que el texto. Medido contra el prompt real (~18.000 tokens) con
# openai/gpt-5-mini: 448 tokens de razonamiento antes de escribir una sola
# palabra. Con el techo en 500 la respuesta volvía cortada a mitad de frase
# (finish_reason "length"), y con nvidia/nemotron-3-ultra-550b-a55b:free
# volvía directamente vacía.
MAX_TOKENS_RESPUESTA = 2000

# Instrumentación: separar el tiempo hasta los headers del tiempo hasta tener
# el cuerpo entero. Importa porque la línea "HTTP Request: ..." que loguea
# httpx NO marca el final de la llamada: httpx la escribe apenas vuelven los
# headers (Client._send_single_request, httpx 0.28.1) y recién después lee el
# cuerpo, adentro del mismo post(). Un proveedor que manda los headers
# enseguida y después se toma diez segundos para generar el texto deja todo
# ese tiempo escondido entre esa línea y la siguiente del log.
#
# El hook de "response" de httpx corre justo en ese punto intermedio
# (Client._send_handling_redirects, antes del response.read() de send()), así
# que alcanza para marcarlo sin cambiar a una lectura en streaming.
#
# El instante va en un threading.local y no en una variable del módulo porque
# el httpx.Client es uno solo y compartido: las background tasks de Starlette
# corren en paralelo en un threadpool, y dos mensajes simultáneos se pisarían
# la marca.
_medicion = threading.local()


def _marcar_llegada_de_headers(respuesta: httpx.Response) -> None:
    _medicion.headers_en = time.perf_counter()


def _loguear_tiempos_http(inicio: float, fin: float) -> None:
    ms_total = (fin - inicio) * 1000
    headers_en = getattr(_medicion, "headers_en", None)
    if headers_en is None:
        # Sin hook (pasa en los tests, que reemplazan el cliente por uno con
        # MockTransport): se loguea el total igual, sin el desglose.
        logger.info("TIEMPOS modelo | HTTP completo: %.0f ms", ms_total)
        return
    ms_headers = (headers_en - inicio) * 1000
    logger.info(
        "TIEMPOS modelo | headers: %.0f ms | cuerpo: %.0f ms | HTTP completo: %.0f ms",
        ms_headers, ms_total - ms_headers, ms_total,
    )


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
        self._http = httpx.Client(
            timeout=TIMEOUT_SEGUNDOS,
            event_hooks={"response": [_marcar_llegada_de_headers]},
        )
        self._url = f"{config.base_url.rstrip('/')}/chat/completions"

    def generar_respuesta(self, historial: list[Mensaje], mensaje_nuevo: str) -> RespuestaGenerada:
        mensajes = [{"role": "system", "content": SYSTEM_PROMPT}]
        mensajes.extend(_a_mensaje_openai(mensaje) for mensaje in historial)
        mensajes.append({"role": "user", "content": mensaje_nuevo})

        cuerpo = con_un_reintento(lambda: self._llamar(mensajes))
        return _interpretar_respuesta(cuerpo)

    def _llamar(self, mensajes: list[dict]) -> dict:
        # Se limpia antes de cada intento: si este post() falla sin llegar a
        # los headers, no queremos medir contra la marca del intento anterior.
        _medicion.headers_en = None
        inicio = time.perf_counter()
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
                "max_tokens": MAX_TOKENS_RESPUESTA,
            },
        )
        # Antes del raise_for_status: un 429 o un 5xx también tardan, y ese
        # tiempo cuenta igual para el presupuesto.
        _loguear_tiempos_http(inicio, time.perf_counter())

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


def _tokens_de_uso(cuerpo: dict) -> tuple[int | None, int | None]:
    """Tokens de entrada/salida del bloque "usage" del formato OpenAI, para
    el dashboard de costos (specs/spec-dashboard-metricas.md). No todo
    proveedor detrás de BASE_URL lo devuelve — sin "usage", queda None y el
    dashboard lo muestra como N/D en vez de asumir cero."""
    uso = cuerpo.get("usage")
    if not isinstance(uso, dict):
        return None, None
    return uso.get("prompt_tokens"), uso.get("completion_tokens")


def _interpretar_respuesta(cuerpo: dict) -> RespuestaGenerada:
    """Junta el texto y detecta si hay un tool call de escalar_a_humano.
    Si el tool call llegó pero function.arguments no parsea como JSON (o
    parsea pero no trae "resumen"), se escala igual: un JSONDecodeError no
    puede tumbar el request, y perder el escalamiento es peor que perder
    el resumen."""
    choices = cuerpo.get("choices") or []
    if not choices:
        return RespuestaGenerada(texto=None, escalar=False, resumen=None)

    # "length" significa que el modelo se quedó sin presupuesto en la mitad:
    # el texto que sigue abajo está cortado, muchas veces a mitad de frase.
    # No se descarta (media respuesta es mejor que ninguna, y el usuario puede
    # repreguntar), pero tiene que quedar en el log: sin esto el bot manda una
    # frase incompleta y no hay ningún rastro de que pasó algo raro.
    if choices[0].get("finish_reason") == "length":
        logger.warning(
            "La respuesta se cortó por max_tokens (finish_reason=length): el texto va "
            "incompleto. Revisar MAX_TOKENS_RESPUESTA contra los tokens de razonamiento "
            "del modelo configurado."
        )

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

    tokens_entrada, tokens_salida = _tokens_de_uso(cuerpo)
    return RespuestaGenerada(
        texto=texto, escalar=escalar, resumen=resumen,
        tokens_entrada=tokens_entrada, tokens_salida=tokens_salida,
    )
