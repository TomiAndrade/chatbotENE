"""Configuración compartida de los tests.

Las variables de entorno se fijan ANTES de importar cualquier módulo de
`app`, porque `app.config.config` es un singleton que se arma una sola vez al
importarlo (ver app/config.py). Fijarlas acá, y no en `.env`, evita que los
tests dependan de las claves reales del desarrollador o pisen su base de
desarrollo.
"""

import itertools
import os
import secrets
import tempfile
from pathlib import Path

_DIR_TEMP = Path(tempfile.mkdtemp(prefix="chatbot_polo_tests_"))
_DB_PATH = _DIR_TEMP / "test.db"

os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["KAPSO_API_KEY"] = "test-kapso-api-key"
os.environ["KAPSO_PHONE_NUMBER_ID"] = "000000000000"
os.environ["KAPSO_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["META_PHONE_NUMBER_ID"] = "000000000000"
os.environ["META_ACCESS_TOKEN"] = "test-meta-access-token"
os.environ["META_APP_SECRET"] = "test-app-secret"
os.environ["META_VERIFY_TOKEN"] = "test-verify-token"
os.environ["META_API_VERSION"] = "v23.0"
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
# Agrupamiento de mensajes consecutivos (ver specs/spec-agrupamiento-mensajes.md).
# Chicos a propósito: la mayoría de los tests manda un mensaje de texto y
# espera la respuesta, y con esto la espera de agrupamiento no se nota. Los
# tests que necesitan controlar la ventana con precisión la pisan con
# monkeypatch sobre `config`, no dependen de estos valores.
os.environ["AGRUPAR_VENTANA_SEGUNDOS"] = "0.02"
os.environ["AGRUPAR_ESPERA_MAXIMA_SEGUNDOS"] = "0.08"
os.environ["AGRUPAR_ABANDONO_SEGUNDOS"] = "5"
# CRM prendido en la suite: sin CRM_HABILITADO el router ni siquiera se
# registra (app/main.py) y los tests del panel darían 404 por el motivo
# equivocado. No hay ninguna credencial del panel acá — las cuentas viven en
# la base y las crea cada test con la fixture `usuario_crm`.
os.environ["CRM_HABILITADO"] = "true"
os.environ["CRM_BASE_URL"] = "https://testserver"

import pytest
from fastapi.testclient import TestClient

from app import models
from app.config import config
from app.crm import modelos as crm_modelos
from app.db import SessionLocal, crear_engine, init_db
from app.main import app, meta_client

TELEFONO_DE_PRUEBA = "5492995551234"


@pytest.fixture(scope="session", autouse=True)
def _tablas():
    """Esta suite no pasa por al_iniciar() (ver fixture `client` más abajo),
    así que nadie más llama a crear_engine(): sin esto, init_db() y
    cualquier SessionLocal() de los tests revientan con el RuntimeError de
    app/db.py."""
    crear_engine()
    init_db()
    yield


@pytest.fixture(autouse=True)
def _base_limpia():
    """Cada test arranca con las tablas vacías: se limpian al final del
    test anterior, así un test que falla deja rastro para inspeccionar."""
    yield
    db = SessionLocal()
    try:
        # Las dos son hijas de Conversacion por FK (conversacion_id): tienen
        # que borrarse antes que la tabla padre, si no una base con las
        # constraints activas (Postgres real) fallaría; SQLite no las hace
        # cumplir por default, pero el orden tiene que ser correcto igual.
        # Sin borrar LlamadaIA acá, sus filas se acumulaban entre tests
        # (nunca se limpiaban) y contaminaban los conteos/costos agregados
        # de test_metricas.py y las búsquedas puntuales de
        # test_llamada_ia.py (MultipleResultsFound). Mismo motivo para
        # EnvioWhatsapp (FK a mensaje_id: tiene que borrarse antes que
        # Mensaje) — si no, contaminaría test_costo_whatsapp.py.
        db.query(models.EnvioWhatsapp).delete()
        db.query(models.LlamadaIA).delete()
        db.query(models.Mensaje).delete()
        db.query(models.Conversacion).delete()
        # También las del CRM: si una sesión sobreviviera al test, el
        # siguiente podría entrar al panel sin haberse logueado; si
        # sobrevivieran los intentos fallidos, un test dejaría bloqueado al
        # usuario del siguiente.
        db.query(crm_modelos.SesionCrm).delete()
        db.query(crm_modelos.IntentoLoginCrm).delete()
        db.query(crm_modelos.UsuarioCrm).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def escalamiento_activo(monkeypatch):
    """Prende ESCALAMIENTO_HABILITADO para un test.

    El default de la suite es `false`, igual que producción (ver
    spec-derivacion.md): hoy no hay bandeja de entrada y el bot no escala.
    Los tests que ejercitan el escalamiento lo piden explícito con esta
    fixture — sin ella pasaban igual, porque `main.py` escalaba sin mirar el
    flag, y ese agujero es justamente el que arreglamos.
    """
    monkeypatch.setattr(config, "escalamiento_habilitado", True)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def meta_enviados(monkeypatch):
    """Reemplaza el envío real por uno que solo registra qué se mandó. Los
    tests no deben pegarle a la API real de Meta.

    Cada llamada devuelve un `messages[0].id` distinto, como el real: desde
    que `enviar_y_guardar` (app/main.py) persiste ese id en
    `Mensaje.wa_message_id` (columna `unique=True`), un test que mande más de
    un mensaje del bot con un id fijo repetido violaría esa constraint — no es
    un detalle de implementación, un mismo wa_message_id dos veces sería un
    bug real de Meta, nunca algo que el bot tenga que tolerar en un test.
    """
    enviados = []
    contador_ids = itertools.count(1)

    def envio_falso(telefono: str, texto: str) -> dict:
        enviados.append((telefono, texto))
        return {"messages": [{"id": f"wamid.falso.{next(contador_ids)}"}]}

    monkeypatch.setattr(meta_client, "enviar_mensaje_texto", envio_falso)
    return enviados


@pytest.fixture
def client(meta_enviados):
    """Sin `with TestClient(app) as ...`: ese context manager dispara el
    evento startup real de la app, que desde spec-validacion-config-arranque.md
    también corre validar_config() — y esta suite fija DATABASE_URL a SQLite
    a propósito (ver arriba), que la validación rechaza sin excepción, sin
    importar el entorno. Las tablas ya las crea el fixture `_tablas` llamando
    init_db() directo, así que no hace falta pasar por el ciclo de vida
    completo de FastAPI solo para pegarle al webhook."""
    return TestClient(app)


# Las credenciales de prueba del panel. Son de mentira y solo existen dentro
# de la suite: la contraseña se genera al azar en cada corrida para que no
# haya ninguna escrita en el repo, ni siquiera una de juguete.
USUARIO_DE_PRUEBA = "equipo-ene"
PASSWORD_DE_PRUEBA = "prueba-" + secrets.token_urlsafe(16)


@pytest.fixture
def cliente_crm(meta_enviados):
    """Cliente para los tests del CRM, sobre https.

    El esquema importa: la cookie de sesión sale con el flag `Secure` salvo
    en DEBUG (ver `cookie_segura` en app/crm/auth.py), y esta suite corre con
    DEBUG=false, igual que producción. Un cliente HTTP plano descartaría la
    cookie en silencio y los tests fallarían por el motivo equivocado.

    Pide `meta_enviados` aunque el CRM no mande nada por WhatsApp: justamente
    por eso. Si alguna vez el panel empezara a enviar algo, el mock está
    puesto y el test lo ve, en vez de pegarle a la API real de Meta.
    """
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def usuario_crm(db):
    """Una cuenta del panel, creada como la crearía el comando de consola
    (misma función, `usuarios.crear`). Devuelve la fila."""
    from app.crm import usuarios

    return usuarios.crear(db, USUARIO_DE_PRUEBA, PASSWORD_DE_PRUEBA)


def hacer_login(
    cliente: TestClient,
    usuario: str = USUARIO_DE_PRUEBA,
    password: str = PASSWORD_DE_PRUEBA,
):
    """El POST del login, tal cual lo manda la pantalla de entrar. No afirma
    nada: hay tests que lo usan esperando que falle."""
    return cliente.post("/crm/api/login", json={"usuario": usuario, "password": password})


def login_crm(cliente: TestClient, usuario_crm) -> str:
    """Loguea al cliente y devuelve el token CSRF que necesitan las acciones
    que escriben (reactivar y salir).

    Toma `usuario_crm` (la fixture) para que el test tenga que pedirla: sin
    la cuenta creada no hay con qué loguearse, y así el orden queda explícito
    en la firma en vez de depender de un efecto de importación.
    """
    respuesta = hacer_login(cliente, usuario_crm.usuario, PASSWORD_DE_PRUEBA)
    assert respuesta.status_code == 200, respuesta.text
    return cliente.get("/crm/api/sesion").json()["csrf"]
