"""Capa de generación de respuestas.

Todo el resto del proyecto llama únicamente a generar_respuesta(). Agregar un
proveedor nuevo es: crear una clase que implemente ProveedorRespuesta y
sumarla a _PROVEEDORES. No hay que tocar nada más.

Sobre el tipo de `historial`: son objetos Mensaje, no strings sueltos. Cada
uno trae su `rol`, y esa distinción importa: los mensajes con rol `humano`
(escritos por alguien del polo) son contexto válido para el modelo pero no
ejemplos de cómo debe responder el bot (ver app.historial.mapear_mensaje).
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, TypeVar

from app.config import config
from app.models import Mensaje

logger = logging.getLogger("respuesta")

T = TypeVar("T")

# Tool calling: no se implementa el bucle completo (ver spec-etapa2.md). Si el
# modelo llama a esta herramienta, se corta ahí — el servidor arma el aviso.
NOMBRE_HERRAMIENTA_ESCALAR = "escalar_a_humano"
DESCRIPCION_HERRAMIENTA_ESCALAR = (
    "Transfiere la conversación a una persona del equipo. Usar cuando la "
    "consulta no puede resolverse con la información disponible, cuando el "
    "usuario pide hablar con alguien, o ante reclamos."
)
PARAMETROS_HERRAMIENTA_ESCALAR = {
    "type": "object",
    "properties": {
        "resumen": {
            "type": "string",
            "description": "Resumen breve de qué necesita la persona, para dar contexto a quien atienda.",
        }
    },
    "required": ["resumen"],
}

# Timeout de la llamada al modelo y política de reintento (ver spec-etapa2.md,
# "Manejo de errores"): 20 segundos en total y un solo reintento.
#
# Los 20 segundos son el presupuesto completo, no el de cada intento: lo que
# importa es cuánto espera el usuario del otro lado de WhatsApp, y con dos
# intentos de 20s cada uno esa espera se iba a 40s. Se reparte entre los
# intentos, así que cada llamada tiene 10s.
PRESUPUESTO_TOTAL_SEGUNDOS = 20.0
MAX_INTENTOS = 2
TIMEOUT_SEGUNDOS = PRESUPUESTO_TOTAL_SEGUNDOS / MAX_INTENTOS


def con_un_reintento(func: Callable[[], T]) -> T:
    """Ejecuta func() y, si falla, la reintenta una única vez. Un reintento
    largo empeora la experiencia más de lo que la salva."""
    ultimo_error: Exception | None = None
    for intento in range(1, MAX_INTENTOS + 1):
        try:
            return func()
        except Exception as error:
            ultimo_error = error
            logger.warning("Intento %s/%s de llamar al modelo falló: %s", intento, MAX_INTENTOS, error)
    assert ultimo_error is not None
    raise ultimo_error


@dataclass
class RespuestaGenerada:
    texto: str | None
    escalar: bool
    resumen: str | None


class ErrorTransitorioProveedor(Exception):
    """Un proveedor de IA falló de forma transitoria (saturación, 429, 5xx, o
    un error de upstream que llegó dentro de un HTTP 200 — ver spec-etapa2.md,
    "Manejo de errores"). Distinto de una excepción común: en desarrollo no
    dispara el escalamiento a humano, solo avisa que se reintente."""


class ProveedorRespuesta(ABC):
    @abstractmethod
    def generar_respuesta(self, historial: list[Mensaje], mensaje_nuevo: str) -> RespuestaGenerada:
        ...


class ProveedorFijo(ProveedorRespuesta):
    """Etapa 1: sin IA, responde siempre el mismo texto. Se conserva para
    tests que no necesitan (ni deben) llamar a una API real."""

    RESPUESTA_FIJA = (
        "¡Hola! Somos ENE IA LAB. Recibimos tu mensaje, pronto te va a responder "
        "alguien del equipo."
    )

    def generar_respuesta(self, historial: list[Mensaje], mensaje_nuevo: str) -> RespuestaGenerada:
        return RespuestaGenerada(texto=self.RESPUESTA_FIJA, escalar=False, resumen=None)


def _proveedor_openai_compat() -> ProveedorRespuesta:
    from app.proveedor_openai_compat import ProveedorOpenAICompat

    return ProveedorOpenAICompat()


def _proveedor_claude() -> ProveedorRespuesta:
    from app.proveedor_claude import ProveedorClaude

    return ProveedorClaude()


# Los proveedores con IA se instancian recién al primer uso (necesitan la API
# key configurada; con "fijo" — el que usan los tests — ni siquiera hace falta).
_FABRICAS_PROVEEDORES: dict[str, Callable[[], ProveedorRespuesta]] = {
    "fijo": lambda: ProveedorFijo(),
    "openai_compat": _proveedor_openai_compat,
    "claude": _proveedor_claude,
}
_instancias: dict[str, ProveedorRespuesta] = {}


def _obtener_proveedor(nombre: str) -> ProveedorRespuesta:
    if nombre not in _instancias:
        fabrica = _FABRICAS_PROVEEDORES.get(nombre)
        if fabrica is None:
            raise ValueError(f"PROVEEDOR_IA desconocido: {nombre!r}")
        _instancias[nombre] = fabrica()
    return _instancias[nombre]


def generar_respuesta(historial: list[Mensaje], mensaje_nuevo: str) -> RespuestaGenerada:
    proveedor = _obtener_proveedor(config.proveedor_ia)
    return proveedor.generar_respuesta(historial, mensaje_nuevo)
