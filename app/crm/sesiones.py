"""Sesiones del panel: crearlas, leerlas, revocarlas y limpiar las vencidas.

La cookie del navegador lleva **solo un identificador aleatorio** y de la
base solo se puede sacar su SHA-256, así que lo que se guarda no sirve para
entrar. Toda la información de la sesión (de quién es, hasta cuándo vale, su
token CSRF) vive en Postgres, en `crm_sesiones`.

Vencimiento **absoluto**: se fija al crear la sesión y no se estira con el
uso. Una sesión que quedó abierta en una máquina compartida se muere sola.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.crm.modelos import SesionCrm, UsuarioCrm

logger = logging.getLogger("bot")

# Una jornada de trabajo. Al vencer hay que volver a entrar con usuario y
# contraseña.
DURACION_SESION = timedelta(hours=12)

# Cuántas filas vencidas se borran como mucho por pasada. Acotado a propósito:
# la limpieza corre dentro de un request (el login), y un DELETE sin límite
# sobre una tabla grande lo dejaría esperando.
MAXIMO_A_LIMPIAR = 200

# Separador de dominio del hash del token. Va adelante del valor de la cookie
# antes de hashearlo, así que un `token_hash` calculado por el mecanismo
# anterior (el login con Auth0, que hasheaba el token pelado) no coincide con
# ninguna búsqueda de ahora: **una cookie de aquel sistema no da acceso**,
# aunque sus filas siguieran en la base. El arranque además no deja pasar una
# base con el esquema viejo (`modelos.verificar_esquema`); esto es el
# cinturón, aquello los tiradores.
SEPARADOR_DE_DOMINIO = "crm-sesion-local:"


def _hashear(token: str) -> str:
    """SHA-256 y no Argon2: el token es aleatorio de 256 bits, no una
    contraseña que alguien pueda adivinar por diccionario. Lo que hace falta
    acá es que el valor guardado no sirva para entrar, no encarecer un ataque
    de fuerza bruta que no existe. Para las contraseñas, que sí se adivinan,
    está `app/crm/passwords.py`."""
    return hashlib.sha256((SEPARADOR_DE_DOMINIO + token).encode("utf-8")).hexdigest()


def _a_utc(fecha: datetime | None) -> datetime | None:
    """SQLite devuelve los DateTime(timezone=True) sin tzinfo aunque se hayan
    guardado en UTC (mismo caso que documenta `app/pausa.py`)."""
    if fecha is None:
        return None
    if fecha.tzinfo is None:
        return fecha.replace(tzinfo=timezone.utc)
    return fecha


def crear_sesion(db: Session, usuario: UsuarioCrm) -> tuple[str, SesionCrm]:
    """Crea una sesión nueva y devuelve `(token_para_la_cookie, fila)`.

    El token se genera acá y se devuelve una sola vez: después de este
    momento nadie —ni el panel, ni la base— puede volver a calcularlo.
    """
    token = secrets.token_urlsafe(32)
    ahora = datetime.now(timezone.utc)
    sesion = SesionCrm(
        token_hash=_hashear(token),
        usuario_id=usuario.id,
        csrf=secrets.token_urlsafe(24),
        creada_en=ahora,
        expira_en=ahora + DURACION_SESION,
    )
    db.add(sesion)
    db.commit()
    db.refresh(sesion)
    return token, sesion


def buscar_sesion_valida(db: Session, token: str | None) -> SesionCrm | None:
    """La sesión de ese token, si sirve para entrar ahora mismo.

    Cinco condiciones, todas por el mismo precio de una consulta:

    1. el token está en la base (uno inventado, o de una sesión ya borrada,
       no encuentra nada);
    2. la sesión no está revocada (logout, o revocación a mano);
    3. no venció;
    4. la cuenta sigue **activa**;
    5. la sesión es **posterior** al último cambio de credenciales de esa
       cuenta. Es lo que hace que cambiar la contraseña o desactivar a
       alguien deje afuera a las sesiones que ya estaban abiertas, incluso a
       una que haya creado otra instancia del servidor mientras tanto.

    Desde afuera los cinco casos son iguales: devuelve None y el panel manda
    a la pantalla de entrar. No se distingue por qué falló.
    """
    if not token:
        return None

    sesion = db.query(SesionCrm).filter_by(token_hash=_hashear(token)).first()
    if sesion is None:
        return None
    if sesion.revocada_en is not None:
        return None
    if _a_utc(sesion.expira_en) <= datetime.now(timezone.utc):
        return None

    usuario = sesion.usuario
    if usuario is None or not usuario.activo:
        return None
    if _a_utc(usuario.credenciales_cambiadas_en) > _a_utc(sesion.creada_en):
        return None

    return sesion


def revocar_sesion(db: Session, sesion: SesionCrm) -> None:
    """Marca la sesión como cerrada. A partir de acá la cookie no sirve más,
    aunque alguien tenga una copia guardada."""
    sesion.revocada_en = datetime.now(timezone.utc)
    db.commit()


def revocar_token(db: Session, token: str | None) -> None:
    """Revoca la sesión de ese token si sigue viva. Se usa en el logout y al
    completar un login nuevo."""
    sesion = buscar_sesion_valida(db, token)
    if sesion is not None:
        revocar_sesion(db, sesion)


def limpiar_vencidas(db: Session, maximo: int = MAXIMO_A_LIMPIAR) -> int:
    """Borra hasta `maximo` sesiones vencidas o revocadas.

    Se llama al intentar un login, que es el momento natural: si nadie usa el
    panel, tampoco hay filas nuevas que limpiar. No hay tarea de fondo ni
    cron — una fila vencida no hace nada, solo ocupa lugar.
    """
    ahora = datetime.now(timezone.utc)

    viejas = (
        db.query(SesionCrm.id)
        .filter((SesionCrm.expira_en <= ahora) | (SesionCrm.revocada_en.isnot(None)))
        .limit(maximo)
        .all()
    )
    if not viejas:
        return 0

    ids = [fila[0] for fila in viejas]
    borradas = db.query(SesionCrm).filter(SesionCrm.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    logger.debug("CRM: se limpiaron %s sesiones vencidas o revocadas", borradas)
    return borradas
