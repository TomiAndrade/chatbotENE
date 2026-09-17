"""Límite de intentos de ingreso al panel.

**Se cuenta contra la base, no en memoria del proceso** (`crm_intentos_login`).
Es la misma decisión que toma `app/limite.py` para el límite de mensajes por
hora, y por los mismos dos motivos: un contador en memoria se borra en cada
deploy, y con dos instancias del servidor detrás de un balanceador cada una
llevaría su propia cuenta, o sea que el límite real sería el doble (o el
triple, o el que dé la cantidad de instancias).

Dos límites, no uno:

- **por cuenta**, que es el que protege una contraseña de un ataque de
  diccionario;
- **por IP**, más alto, que es el que frena a alguien probando muchas cuentas
  distintas desde el mismo lado.

El límite por cuenta se cuenta **por el nombre que se intentó, exista o no
esa cuenta**. Es lo que hace que el bloqueo no sirva para averiguar qué
cuentas hay: un nombre inventado se bloquea igual que uno real.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crm.modelos import IntentoLoginCrm

logger = logging.getLogger("bot")

# Ventana deslizante: los intentos se cuentan sobre los últimos 15 minutos.
VENTANA = timedelta(minutes=15)

# Cuántos intentos fallidos seguidos se toleran por cuenta antes de cerrarle
# la puerta por lo que queda de la ventana. Cinco es lo que tolera una
# persona que se equivoca tipeando; un diccionario necesita órdenes de
# magnitud más.
MAXIMO_POR_USUARIO = 5

# El de la IP es más alto: en una oficina entra todo el equipo desde la misma
# salida a internet, y un par de personas que se equivocan no pueden dejar
# afuera al resto.
MAXIMO_POR_IP = 20

# Cuántas filas viejas se borran como mucho por pasada. Acotado a propósito:
# la limpieza corre dentro del request del login, y un DELETE sin límite
# sobre una tabla grande lo dejaría esperando. Mismo criterio que
# `sesiones.limpiar_vencidas`.
MAXIMO_A_LIMPIAR = 500


def _desde(ahora: datetime) -> datetime:
    return ahora - VENTANA


def _fallidos(db: Session, ahora: datetime, **filtros) -> int:
    consulta = db.query(func.count(IntentoLoginCrm.id)).filter(
        IntentoLoginCrm.exitoso.is_(False),
        IntentoLoginCrm.creado_en > _desde(ahora),
    )
    return consulta.filter_by(**filtros).scalar() or 0


def esta_bloqueado(db: Session, usuario: str, ip: str | None, ahora: datetime | None = None) -> bool:
    """Si este intento hay que rechazarlo sin siquiera mirar la contraseña.

    Se consulta **antes** de verificar la contraseña: con la cuenta
    bloqueada, una contraseña correcta tampoco entra. Si entrara, el bloqueo
    no frenaría a quien acertó en el intento número seis.
    """
    ahora = ahora or datetime.now(timezone.utc)

    if _fallidos(db, ahora, usuario=usuario) >= MAXIMO_POR_USUARIO:
        return True
    if ip and _fallidos(db, ahora, ip=ip) >= MAXIMO_POR_IP:
        return True
    return False


def registrar(db: Session, usuario: str, ip: str | None, exitoso: bool) -> None:
    """Deja el intento anotado.

    Un login correcto **borra los fallidos de esa cuenta**: la persona
    demostró que es ella, así que los intentos de antes no tienen que seguir
    contando para el próximo bloqueo. Los de la IP no se borran — el límite
    de la IP existe justamente para el caso de alguien probando muchas
    cuentas, y una que acierta no lo absuelve.
    """
    if exitoso:
        db.query(IntentoLoginCrm).filter(
            IntentoLoginCrm.usuario == usuario, IntentoLoginCrm.exitoso.is_(False)
        ).delete(synchronize_session=False)

    db.add(
        IntentoLoginCrm(
            usuario=usuario,
            ip=ip,
            exitoso=exitoso,
            creado_en=datetime.now(timezone.utc),
        )
    )
    db.commit()


def limpiar_viejos(db: Session, maximo: int = MAXIMO_A_LIMPIAR) -> int:
    """Borra hasta `maximo` intentos anteriores a la ventana: ya no cuentan
    para nada y solo ocupan lugar. Corre al intentar un login, que es el
    único momento en que la tabla crece."""
    limite = _desde(datetime.now(timezone.utc))
    viejos = (
        db.query(IntentoLoginCrm.id)
        .filter(IntentoLoginCrm.creado_en <= limite)
        .limit(maximo)
        .all()
    )
    if not viejos:
        return 0

    ids = [fila[0] for fila in viejos]
    borrados = (
        db.query(IntentoLoginCrm)
        .filter(IntentoLoginCrm.id.in_(ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    logger.debug("CRM: se limpiaron %s intentos de login viejos", borrados)
    return borrados


def ip_del_request(request) -> str | None:
    """De qué IP viene el request.

    `request.client.host` y **no** `X-Forwarded-For`: ese header lo pone
    quien manda el request y, sin un proxy de confianza que lo reescriba,
    cualquiera puede inventarse una IP distinta en cada intento y saltarse el
    límite por IP entero. Detrás de un proxy que no pase la IP real, todos
    los intentos caen en la misma "IP" (la del proxy) — el límite por IP se
    vuelve inútil pero el de la cuenta, que es el que protege la contraseña,
    sigue funcionando igual.

    Si el día que haya un proxy adelante se quiere usar el header, hay que
    configurarlo explícitamente (uvicorn `--proxy-headers` con
    `--forwarded-allow-ips`), no confiar en él desde acá.
    """
    if request.client is None:
        return None
    return request.client.host
