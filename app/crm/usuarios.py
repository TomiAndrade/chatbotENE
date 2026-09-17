"""Las cuentas del panel: crearlas, cambiarles la contraseña, desactivarlas.

Lo usan dos lados y por eso vive acá y no dentro del comando de consola:

- `scripts/crm_usuario.py`, que es **la única forma de crear una cuenta** —
  no hay registro público ni alta desde el panel;
- `app/crm/rutas.py`, para el login.

Todo lo que cambia una credencial (la contraseña o el estado de la cuenta)
hace dos cosas juntas: mueve `credenciales_cambiadas_en` y revoca las
sesiones abiertas de esa persona. Las dos, no una — ver `cerrar_sesiones`.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.crm import passwords
from app.crm.modelos import SesionCrm, UsuarioCrm

logger = logging.getLogger("bot")


class UsuarioYaExiste(ValueError):
    pass


class UsuarioNoExiste(ValueError):
    pass


def normalizar(nombre: str) -> str:
    """El nombre de usuario, tal como se guarda y se busca.

    Minúsculas y sin espacios alrededor: si no, "Tomi" y "tomi" serían dos
    cuentas distintas y quien intenta entrar no entiende por qué su
    contraseña "no anda".
    """
    return nombre.strip().lower()


def buscar(db: Session, nombre: str) -> UsuarioCrm | None:
    return db.query(UsuarioCrm).filter_by(usuario=normalizar(nombre)).first()


def cantidad(db: Session) -> int:
    """Cuántas cuentas hay, activas o no. Lo usa el arranque para avisar que
    el panel está prendido y todavía no hay con qué entrar."""
    return db.query(UsuarioCrm).count()


def listar(db: Session) -> list[UsuarioCrm]:
    return db.query(UsuarioCrm).order_by(UsuarioCrm.usuario).all()


def cerrar_sesiones(db: Session, usuario: UsuarioCrm) -> int:
    """Revoca todas las sesiones abiertas de esa cuenta. Devuelve cuántas.

    Es la **segunda** barrera, no la única: `buscar_sesion_valida` también
    descarta cualquier sesión anterior a `credenciales_cambiadas_en` (ver
    `app/crm/sesiones.py`). Se hacen las dos cosas a propósito. La marca de
    tiempo cubre una sesión que se hubiera creado en el mismo instante en
    otra instancia del servidor; la revocación deja el motivo visible en la
    base, que es lo que se mira cuando hay que explicar por qué alguien se
    quedó afuera.
    """
    ahora = datetime.now(timezone.utc)
    revocadas = (
        db.query(SesionCrm)
        .filter(SesionCrm.usuario_id == usuario.id, SesionCrm.revocada_en.is_(None))
        .update({SesionCrm.revocada_en: ahora}, synchronize_session=False)
    )
    return revocadas


def crear(db: Session, nombre: str, password: str) -> UsuarioCrm:
    """Crea una cuenta. La contraseña se valida y se hashea acá: en claro no
    sale de esta función.

    Esta es la puerta de alta de la primera cuenta administradora y de
    cualquier otra. La llama el comando de consola, nunca un endpoint.
    """
    nombre = normalizar(nombre)
    if not nombre:
        raise ValueError("El nombre de usuario no puede estar vacío.")
    if buscar(db, nombre) is not None:
        raise UsuarioYaExiste(f"Ya existe una cuenta llamada {nombre!r}.")

    passwords.validar(password)

    ahora = datetime.now(timezone.utc)
    usuario = UsuarioCrm(
        usuario=nombre,
        hash_password=passwords.hashear(password),
        activo=True,
        creado_en=ahora,
        credenciales_cambiadas_en=ahora,
    )
    db.add(usuario)
    db.commit()
    db.refresh(usuario)
    logger.info("CRM: se creó la cuenta %s", usuario.usuario)
    return usuario


def cambiar_password(db: Session, nombre: str, password: str) -> UsuarioCrm:
    """Cambia la contraseña y deja afuera a las sesiones que ya estaban
    abiertas: si se cambia porque la anterior se filtró, dejar viva la sesión
    de quien la tenía no cambia nada."""
    usuario = _obligatorio(db, nombre)
    passwords.validar(password)

    usuario.hash_password = passwords.hashear(password)
    usuario.credenciales_cambiadas_en = datetime.now(timezone.utc)
    revocadas = cerrar_sesiones(db, usuario)
    db.commit()
    logger.info(
        "CRM: cambió la contraseña de %s (se cerraron %s sesiones)", usuario.usuario, revocadas
    )
    return usuario


def desactivar(db: Session, nombre: str) -> UsuarioCrm:
    """Deja la cuenta sin acceso, sin borrarla, y le cierra las sesiones
    abiertas en el acto."""
    usuario = _obligatorio(db, nombre)

    usuario.activo = False
    usuario.credenciales_cambiadas_en = datetime.now(timezone.utc)
    revocadas = cerrar_sesiones(db, usuario)
    db.commit()
    logger.info("CRM: se desactivó %s (se cerraron %s sesiones)", usuario.usuario, revocadas)
    return usuario


def activar(db: Session, nombre: str) -> UsuarioCrm:
    """Vuelve a habilitar una cuenta desactivada. No toca la contraseña: si
    también hay que cambiarla, es el otro comando."""
    usuario = _obligatorio(db, nombre)

    usuario.activo = True
    db.commit()
    logger.info("CRM: se reactivó la cuenta %s", usuario.usuario)
    return usuario


def _obligatorio(db: Session, nombre: str) -> UsuarioCrm:
    usuario = buscar(db, nombre)
    if usuario is None:
        raise UsuarioNoExiste(f"No existe ninguna cuenta llamada {normalizar(nombre)!r}.")
    return usuario


def autenticar(db: Session, nombre: str, password: str) -> UsuarioCrm | None:
    """La cuenta, si el nombre existe, está activa y la contraseña coincide.
    None en cualquier otro caso, **sin decir cuál**.

    Los tres fallos se tratan igual a propósito: quien llama no puede
    distinguir "no existe" de "está desactivada" de "la contraseña está
    mal", así que tampoco puede usar el login para averiguar qué cuentas
    hay. Cuando la cuenta no sirve se verifica igual contra el hash de
    relleno (`passwords.quemar_tiempo`) para que la respuesta tarde lo
    mismo: si no, el reloj cuenta lo que el mensaje calla.
    """
    usuario = buscar(db, nombre)
    if usuario is None or not usuario.activo:
        passwords.quemar_tiempo()
        return None

    if not passwords.verificar(usuario.hash_password, password):
        return None

    # Único momento en que la contraseña en claro está a mano para volver a
    # hashearla con los parámetros de hoy.
    if passwords.hay_que_rehashear(usuario.hash_password):
        usuario.hash_password = passwords.hashear(password)
        db.commit()
        logger.info("CRM: se rehasheó la contraseña de %s con los parámetros nuevos", usuario.usuario)

    return usuario
