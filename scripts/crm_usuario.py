"""Las cuentas del panel de conversaciones, desde la consola.

**Esta es la única forma de crear una cuenta del CRM**: no hay registro
público ni alta desde el panel. Sirve tanto para la primera cuenta —la del
administrador, la que se crea antes de que el panel sea usable— como para las
del resto del equipo después.

Uso, desde `chatbot-polo/` y con el `.env` cargado (necesita `DATABASE_URL`):

    python scripts/crm_usuario.py crear <usuario>
    python scripts/crm_usuario.py cambiar-password <usuario>
    python scripts/crm_usuario.py desactivar <usuario>
    python scripts/crm_usuario.py activar <usuario>
    python scripts/crm_usuario.py listar

**La contraseña nunca es un argumento.** Se pide con `getpass`, que no la
muestra mientras se tipea, y se pide dos veces para no dejar a alguien afuera
por un error de tipeo. Un argumento quedaría en el historial de la consola
(`~/.bash_history`, el historial de PowerShell), en la lista de procesos
mientras el comando corre y en cualquier log de auditoría del sistema.
Tampoco se imprime ni se loguea en ningún momento: de la contraseña solo
queda su hash Argon2id en la base (ver `app/crm/passwords.py`).

Cambiar la contraseña o desactivar una cuenta **cierra las sesiones abiertas
de esa persona** (ver `app/crm/usuarios.py`): si se cambia porque la anterior
se filtró, dejar viva la sesión de quien la tenía no cambiaría nada.
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import config
from app.crm import passwords, usuarios
from app.crm.modelos import verificar_esquema
from app.db import SessionLocal, crear_engine, init_db, obtener_engine
from app.validacion_config import ConfigInvalida, validar_config

# Cuántas veces se vuelve a preguntar si las dos contraseñas no coinciden o
# la propuesta no cumple el mínimo. Acotado para que un script que se llame
# sin consola interactiva no quede preguntando para siempre.
INTENTOS_DE_TIPEO = 3


def pedir_password(para: str) -> str:
    """Pide la contraseña dos veces, sin mostrarla, y la devuelve.

    No se imprime en ningún caso: los mensajes de error dicen qué está mal
    (que no coinciden, que es corta), nunca qué se tipeó.
    """
    for intento in range(INTENTOS_DE_TIPEO):
        primera = getpass.getpass(f"Contraseña para {para}: ")
        segunda = getpass.getpass("Repetila: ")

        if primera != segunda:
            print("Las dos contraseñas no coinciden.")
            continue

        try:
            passwords.validar(primera)
        except passwords.PasswordInvalida as error:
            print(str(error))
            continue

        return primera

    raise SystemExit("No se pudo leer una contraseña válida. No se cambió nada.")


def _preparar_base() -> None:
    """Valida la config, abre la base y se asegura de que las tablas existan.

    Es el mismo orden que hace `al_iniciar()` en la app (ver
    spec-validacion-config-arranque.md): validar, crear el engine, crear las
    tablas. Así este comando sirve también para el primer arranque, cuando la
    base todavía no tiene las tablas del panel.
    """
    try:
        validar_config(config)
    except ConfigInvalida as error:
        raise SystemExit(str(error))

    crear_engine()
    init_db()
    verificar_esquema(obtener_engine())


def comando_crear(nombre: str) -> None:
    db = SessionLocal()
    try:
        if usuarios.buscar(db, nombre) is not None:
            raise SystemExit(f"Ya existe una cuenta llamada {usuarios.normalizar(nombre)!r}.")

        primera_cuenta = usuarios.cantidad(db) == 0
        password = pedir_password(usuarios.normalizar(nombre))
        usuario = usuarios.crear(db, nombre, password)

        print(f"Cuenta {usuario.usuario!r} creada.")
        if primera_cuenta:
            print("Es la primera cuenta del panel: con esta se entra a /crm.")
    finally:
        db.close()


def comando_cambiar_password(nombre: str) -> None:
    db = SessionLocal()
    try:
        usuario = _obligatorio(db, nombre)
        password = pedir_password(usuario.usuario)
        usuarios.cambiar_password(db, usuario.usuario, password)
        print(f"Contraseña de {usuario.usuario!r} cambiada. Sus sesiones abiertas se cerraron.")
    finally:
        db.close()


def comando_desactivar(nombre: str) -> None:
    db = SessionLocal()
    try:
        usuario = _obligatorio(db, nombre)
        usuarios.desactivar(db, usuario.usuario)
        print(f"Cuenta {usuario.usuario!r} desactivada. Sus sesiones abiertas se cerraron.")
    finally:
        db.close()


def comando_activar(nombre: str) -> None:
    db = SessionLocal()
    try:
        usuario = _obligatorio(db, nombre)
        usuarios.activar(db, usuario.usuario)
        print(
            f"Cuenta {usuario.usuario!r} activada. La contraseña es la misma de antes; "
            "si no se sabe, cambiala con cambiar-password."
        )
    finally:
        db.close()


def comando_listar() -> None:
    """Qué cuentas hay y cuáles están activas. **No muestra ningún hash**: no
    hace falta para nada y es material para un ataque de diccionario offline
    si la pantalla queda a la vista o el comando se corre en un log."""
    db = SessionLocal()
    try:
        cuentas = usuarios.listar(db)
        if not cuentas:
            print("No hay ninguna cuenta. Creá la primera con: crear <usuario>")
            return

        print(f"{'usuario':<24} {'estado':<12} creada")
        for cuenta in cuentas:
            estado = "activa" if cuenta.activo else "desactivada"
            print(f"{cuenta.usuario:<24} {estado:<12} {cuenta.creado_en:%Y-%m-%d}")
    finally:
        db.close()


def _obligatorio(db, nombre: str):
    usuario = usuarios.buscar(db, nombre)
    if usuario is None:
        raise SystemExit(f"No existe ninguna cuenta llamada {usuarios.normalizar(nombre)!r}.")
    return usuario


# Cada comando, con cuántos argumentos propios espera además del nombre del
# comando. Una tabla en vez de argparse: son cinco comandos con un argumento
# como mucho, y así el uso que se imprime es el mismo texto del docstring.
COMANDOS = {
    "crear": (comando_crear, 1),
    "cambiar-password": (comando_cambiar_password, 1),
    "desactivar": (comando_desactivar, 1),
    "activar": (comando_activar, 1),
    "listar": (comando_listar, 0),
}

USO = """Uso, desde chatbot-polo/:

    python scripts/crm_usuario.py crear <usuario>
    python scripts/crm_usuario.py cambiar-password <usuario>
    python scripts/crm_usuario.py desactivar <usuario>
    python scripts/crm_usuario.py activar <usuario>
    python scripts/crm_usuario.py listar

La contraseña se pide aparte, sin mostrarla: nunca va como argumento."""


def main(argumentos: list[str]) -> None:
    if not argumentos or argumentos[0] not in COMANDOS:
        raise SystemExit(USO)

    comando, cantidad_esperada = COMANDOS[argumentos[0]]
    resto = argumentos[1:]
    if len(resto) != cantidad_esperada:
        raise SystemExit(USO)

    _preparar_base()
    comando(*resto)


if __name__ == "__main__":
    main(sys.argv[1:])
