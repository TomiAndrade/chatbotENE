"""Conexión y sesión de SQLAlchemy.

Cambiar de SQLite a Postgres es solo cuestión de cambiar DATABASE_URL en .env.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import config

# SQLite exige este flag cuando se comparte la conexión entre threads,
# como hace FastAPI. Postgres no lo necesita y create_engine lo ignora.
connect_args = {}
if config.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(config.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db() -> None:
    """Crea las tablas si no existen. Se llama al arrancar la app."""
    from app import models  # noqa: F401 — registra los modelos en Base antes de crear las tablas

    Base.metadata.create_all(bind=engine)
