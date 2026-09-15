"""Medición de cuánto tarda cada etapa del procesamiento de un mensaje.

Es instrumentación, nada más: acá no se optimiza nada ni se cambia ninguna
decisión de diseño. Sirve para poder mirar el log y saber en qué parte se va
el tiempo entre que llega un mensaje y sale la respuesta.

`time.perf_counter()` y no `time.time()`: es monotónico, así que un ajuste
del reloj del sistema (NTP, cambio de horario) no puede dar una duración
negativa o inflada. No sirve para saber "qué hora era" — para eso están los
timestamps del log —, solo para medir cuánto pasó entre dos puntos.
"""

import time


class Cronometro:
    """Arranca al construirse; `ms()` devuelve los milisegundos transcurridos
    desde entonces y se puede llamar las veces que haga falta."""

    def __init__(self) -> None:
        self._inicio = time.perf_counter()

    def ms(self) -> float:
        return (time.perf_counter() - self._inicio) * 1000
