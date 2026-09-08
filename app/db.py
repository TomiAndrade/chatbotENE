"""Conexión y sesión de SQLAlchemy.

La URL se usa tal cual viene en DATABASE_URL, sin reescribirla: Render
entrega el esquema `postgresql://` que SQLAlchemy espera (verificado contra
la base real, `ene-bot-db`). El driver (psycopg2, vía `requirements.txt`) lo
resuelve SQLAlchemy solo, sin necesidad de un `+driver` explícito.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import config

Base = declarative_base()

# El engine NO se crea acá arriba, ni "on demand" en el primer uso: se crea
# una sola vez, explícitamente, llamando a crear_engine() — que hace
# al_iniciar() en app/main.py, después de validar_config() y antes de
# init_db(). Dos razones, no una:
#
# 1. Import-time: con DATABASE_URL ausente o vacía (ver
#    spec-validacion-config-arranque.md, ya no hay default a SQLite),
#    create_engine() revienta apenas se lo llama con una URL sin parsear.
#    Crearlo al importar este módulo adelantaría ese crash antes de que
#    validar_config() tenga chance de correr y loguear un error legible.
# 2. Concurrencia: crearlo "on demand" en la primera sesión (creación
#    perezosa con un `if engine is None`) tiene una carrera de verdad, no
#    teórica — con AnyIO corriendo las background tasks en un threadpool de
#    hasta 40 hilos, dos llamados a SessionLocal() que lleguen antes de que
#    el primero termine de crear el engine pueden ver los dos "no hay
#    engine todavía" y terminar creando dos engines (y dos pools de
#    conexiones) distintos. al_iniciar() corre una sola vez, sincrónico, en
#    el hilo principal, antes de que el server acepte una sola conexión: no
#    hay ventana en la que dos hilos puedan pisarse.
_engine = None
_session_factory = None


def crear_engine() -> None:
    """Crea el engine y el sessionmaker. Se llama una sola vez, desde
    al_iniciar(), después de validar_config() (así la URL ya está validada
    como Postgres) y antes de init_db()."""
    global _engine, _session_factory

    # SQLite exige este flag cuando se comparte la conexión entre threads,
    # como hace FastAPI. Postgres no lo necesita y create_engine lo rechaza,
    # así que el condicional no es opcional. Sigue acá por los tests (y
    # cualquier script) que corren contra SQLite a propósito — en la app
    # real, validar_config() ya garantizó que database_url es Postgres.
    connect_args = {}
    if config.database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}

    _engine = create_engine(
        config.database_url,
        connect_args=connect_args,
        # El servicio pasa horas sin tráfico y el proveedor corta las
        # conexiones ociosas. Sin pre_ping, el primer webhook después de un
        # rato de silencio falla contra una conexión ya muerta y el mensaje
        # se pierde: pre_ping hace un SELECT 1 antes de entregar la conexión
        # y la reemplaza si está caída. pool_recycle las descarta antes, a
        # los 5 minutos, para no llegar tan seguido a ese caso.
        pool_pre_ping=True,
        pool_recycle=300,
    )
    _session_factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def SessionLocal(*args, **kwargs):
    """Mismo uso de siempre (`SessionLocal()` para abrir una sesión).

    Sigue siendo una función y no el sessionmaker directo porque varios
    módulos hacen `from app.db import SessionLocal` al importar (main.py,
    limite.py, historial.py, scripts, tests) — y todos esos imports pasan
    antes de que al_iniciar() corra crear_engine(), así que el nombre tiene
    que quedar estable acá y resolver el sessionmaker real recién en cada
    llamado, no capturarlo al importar.
    """
    if _session_factory is None:
        raise RuntimeError(
            "SessionLocal() llamado antes de crear_engine(). Cualquier "
            "entry point (app real, script, test) tiene que llamar a "
            "crear_engine() antes del primer acceso a la base."
        )
    return _session_factory(*args, **kwargs)


def init_db() -> None:
    """Crea las tablas si no existen. Se llama al arrancar la app, después
    de crear_engine().

    No hay migraciones: `create_all` crea lo que falta pero no modifica nada
    existente. Cualquier cambio de esquema (o un valor nuevo en los enums)
    hay que aplicarlo a mano en Postgres — ver PENDIENTES.md.
    """
    from app import models  # noqa: F401 — registra los modelos en Base antes de crear las tablas

    if _engine is None:
        raise RuntimeError("init_db() llamado antes de crear_engine().")
    Base.metadata.create_all(bind=_engine)
