"""Cómo se guardan y se verifican las contraseñas del panel.

**Argon2id**, con `argon2-cffi` — el binding mantenido de la implementación
de referencia, ganadora de la Password Hashing Competition y lo que
recomienda hoy OWASP para contraseñas. No se escribe nada de criptografía
acá: este módulo elige los parámetros y le pasa el trabajo a la librería.

Por qué Argon2id y no SHA-256 (que sí se usa, a propósito, para el token de
sesión en `app/crm/sesiones.py`): una contraseña la elige una persona y se
puede adivinar por diccionario, así que el hash tiene que ser **caro** —
memoria y tiempo por intento. Un token de sesión es aleatorio de 256 bits y
no se adivina; ahí lo único que hace falta es que el valor guardado no sirva
para entrar.

El hash que devuelve la librería es una cadena con todo adentro (algoritmo,
versión, parámetros y sal aleatoria por contraseña), así que en la base va
una sola columna y no hace falta guardar la sal aparte.
"""

import logging

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError

logger = logging.getLogger("bot")

# Los parámetros por default de argon2-cffi: Argon2id, 64 MiB de memoria,
# 3 pasadas, 4 hilos — la segunda opción recomendada por la RFC 9106. Da
# alrededor de 0,1 s por verificación en una máquina común, que es el orden
# que se busca: imperceptible para quien entra, carísimo para quien prueba
# un diccionario.
#
# Se deja explícito en vez de heredarlo en silencio: si mañana la librería
# cambia sus defaults, que se vea acá y no en un cambio de comportamiento.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

# Largo mínimo. No hay reglas de "una mayúscula y un símbolo": esas empujan a
# contraseñas cortas y previsibles. Lo que sirve es el largo, y el panel se
# usa con un gestor de contraseñas o con una frase.
LARGO_MINIMO = 12


class PasswordInvalida(ValueError):
    """La contraseña propuesta no cumple el mínimo. El mensaje no incluye la
    contraseña."""


def validar(password: str) -> None:
    """Corta con `PasswordInvalida` si la contraseña no sirve. Lo usa el
    comando de consola antes de hashear."""
    if len(password) < LARGO_MINIMO:
        raise PasswordInvalida(
            f"La contraseña tiene que tener al menos {LARGO_MINIMO} caracteres."
        )
    if password.strip() != password:
        raise PasswordInvalida(
            "La contraseña no puede empezar ni terminar con espacios: es muy fácil "
            "perderlos al copiarla y después no poder entrar."
        )


def hashear(password: str) -> str:
    """El hash Argon2id de esa contraseña, con sal aleatoria propia."""
    return _hasher.hash(password)


def verificar(hash_guardado: str, password: str) -> bool:
    """Si la contraseña corresponde a ese hash.

    Cualquier fallo es `False` y no una excepción: un hash corrupto en la
    base (editado a mano, o de una versión anterior) tiene que dejar afuera a
    quien intenta entrar, no reventar el endpoint con un 500 que además
    diría que esa cuenta existe.
    """
    try:
        return _hasher.verify(hash_guardado, password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError):
        logger.error(
            "CRM: hash de contraseña ilegible en la base. La cuenta no puede entrar "
            "hasta que se le cambie la contraseña con scripts/crm_usuario.py."
        )
        return False


def hay_que_rehashear(hash_guardado: str) -> bool:
    """Si ese hash se hizo con parámetros más flojos que los de ahora.

    Se consulta después de un login correcto: es el único momento en que la
    contraseña en claro está a mano para volver a hashearla.
    """
    try:
        return _hasher.check_needs_rehash(hash_guardado)
    except InvalidHashError:
        return False


# Un hash de una contraseña aleatoria que nadie conoce, calculado una sola
# vez al importar. Se verifica contra este cuando el usuario **no existe**,
# para que el login tarde lo mismo exista o no la cuenta: sin esto, un "no
# existe" contesta en microsegundos y un "contraseña incorrecta" en 100 ms, y
# esa diferencia alcanza para ir descubriendo qué cuentas hay.
#
# Se calcula con `secrets.token_urlsafe`, no con un literal: una contraseña
# escrita en el código es una contraseña escrita en el código, aunque sea
# para esto.
def _hash_de_relleno() -> str:
    import secrets

    return hashear(secrets.token_urlsafe(32))


HASH_DE_RELLENO = _hash_de_relleno()


def quemar_tiempo() -> None:
    """Hace el mismo trabajo que una verificación real, pero contra el hash
    de relleno. Se llama cuando la cuenta no existe o está desactivada."""
    verificar(HASH_DE_RELLENO, "no-importa-lo-que-diga-esto")
