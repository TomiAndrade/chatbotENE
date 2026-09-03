"""Configuración compartida de los tests.

Las variables de entorno se fijan ANTES de importar cualquier módulo de
`app`, porque `app.config.config` es un singleton que se arma una sola vez al
importarlo (ver app/config.py). Fijarlas acá, y no en `.env`, evita que los
tests dependan de las claves reales del desarrollador o pisen su base de
desarrollo.
"""

import os
import tempfile
from pathlib import Path

_DIR_TEMP = Path(tempfile.mkdtemp(prefix="chatbot_polo_tests_"))
_DB_PATH = _DIR_TEMP / "test.db"

os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["KAPSO_API_KEY"] = "test-kapso-api-key"
os.environ["KAPSO_PHONE_NUMBER_ID"] = "000000000000"
os.environ["KAPSO_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["DEBUG"] = "false"
os.environ["PROVEEDOR_IA"] = "fijo"
os.environ["MODELO"] = "modelo-de-test"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["BASE_URL"] = ""
os.environ["OPENAI_COMPAT_API_KEY"] = ""
os.environ["TIMEZONE"] = "America/Argentina/Buenos_Aires"
os.environ["HISTORIAL_MAX_MENSAJES"] = "20"
os.environ["HISTORIAL_DIAS_VALIDEZ"] = "7"
os.environ["LIMITE_MENSAJES_HORA"] = "30"
os.environ["PAUSA_HUMANA_MINUTOS"] = "120"
os.environ["ESCALAMIENTO_HABILITADO"] = "false"

import pytest
from fastapi.testclient import TestClient

from app import models
from app.db import SessionLocal, init_db
from app.main import app, kapso_client

TELEFONO_DE_PRUEBA = "5492995551234"


@pytest.fixture(scope="session", autouse=True)
def _tablas():
    init_db()
    yield


@pytest.fixture(autouse=True)
def _base_limpia():
    """Cada test arranca con las tablas vacías: se limpian al final del
    test anterior, así un test que falla deja rastro para inspeccionar."""
    yield
    db = SessionLocal()
    try:
        db.query(models.Mensaje).delete()
        db.query(models.Conversacion).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def kapso_enviados(monkeypatch):
    """Reemplaza el envío real por uno que solo registra qué se mandó. Los
    tests no deben pegarle a la API real de Kapso."""
    enviados = []

    def envio_falso(telefono: str, texto: str) -> dict:
        enviados.append((telefono, texto))
        return {"messages": [{"id": "wamid.falso"}]}

    monkeypatch.setattr(kapso_client, "enviar_mensaje_texto", envio_falso)
    return enviados


@pytest.fixture
def client(kapso_enviados):
    with TestClient(app) as test_client:
        yield test_client
