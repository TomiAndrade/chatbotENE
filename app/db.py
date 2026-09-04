"""Conexión y sesión de SQLAlchemy.

Cambiar de SQLite a Postgres es solo cuestión de cambiar DATABASE_URL en .env:
`normalizar_url` se encarga del prefijo que entrega Render.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import config


def normalizar_url(url: str) -> str:
    """Render entrega la connection string con el prefijo `postgres://`, un
    esquema que SQLAlchemy dejó de aceptar: falla al arrancar con
    `Can't load plugin: sqlalchemy.dialects:postgres`. Se corrige acá y no a
    mano en la variable de entorno porque Render regenera esa URL cuando rota
    credenciales, y volvería a venir con el prefijo viejo.

    Cualquier otra URL (SQLite, o un `postgresql://` que ya venga bien) vuelve
    igual. El driver (psycopg2, vía `requirements.txt`) lo resuelve
    SQLAlchemy solo, sin necesidad de un `+driver` explícito acá.
    """
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


database_url = normalizar_url(config.database_url)

# SQLite exige este flag cuando se comparte la conexión entre threads,
# como hace FastAPI. Postgres no lo necesita y create_engine lo rechaza,
# así que el condicional no es opcional.
connect_args = {}
if database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    database_url,
    connect_args=connect_args,
    # El servicio pasa horas sin tráfico y el proveedor corta las conexiones
    # ociosas. Sin pre_ping, el primer webhook después de un rato de silencio
    # falla contra una conexión ya muerta y el mensaje se pierde: pre_ping
    # hace un SELECT 1 antes de entregar la conexión y la reemplaza si está
    # caída. pool_recycle las descarta antes, a los 5 minutos, para no llegar
    # tan seguido a ese caso.
    pool_pre_ping=True,
    pool_recycle=300,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db() -> None:
    """Crea las tablas si no existen. Se llama al arrancar la app.

    No hay migraciones: `create_all` crea lo que falta pero no modifica nada
    existente. Cualquier cambio de esquema (o un valor nuevo en los enums)
    hay que aplicarlo a mano en Postgres — ver PENDIENTES.md.
    """
    from app import models  # noqa: F401 — registra los modelos en Base antes de crear las tablas

    Base.metadata.create_all(bind=engine)
