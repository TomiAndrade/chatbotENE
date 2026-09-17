"""El arranque no deja pasar una base con el esquema del CRM anterior
(el login con Auth0) — `verificar_esquema` en app/crm/modelos.py.

Por qué hace falta un chequeo y no alcanza con `create_all`: este proyecto no
tiene migraciones. `create_all` crea las tablas que faltan y **no modifica**
las que ya están, así que una `crm_sesiones` de aquella época quedaría con su
esquema viejo (`sub NOT NULL`, sin `usuario_id`) y con sus filas viejas
adentro. Mejor no arrancar y decir qué hacer.

Los tests arman bases SQLite en memoria, aparte de la de la suite: lo que se
prueba es cómo reacciona el chequeo ante un esquema, no el flujo del panel.
"""

import pytest
from sqlalchemy import create_engine, text

from app.crm.modelos import EsquemaCrmViejo, verificar_esquema


def _engine_con(*sentencias: str):
    engine = create_engine("sqlite://")
    with engine.begin() as conexion:
        for sentencia in sentencias:
            conexion.execute(text(sentencia))
    return engine


CREAR_SESIONES_VIEJA = """
CREATE TABLE crm_sesiones (
    id INTEGER PRIMARY KEY,
    token_hash VARCHAR NOT NULL,
    sub VARCHAR NOT NULL,
    email VARCHAR,
    nombre VARCHAR,
    csrf VARCHAR NOT NULL,
    creada_en DATETIME NOT NULL,
    expira_en DATETIME NOT NULL,
    revocada_en DATETIME
)
"""

CREAR_TRANSACCIONES_OIDC = """
CREATE TABLE crm_transacciones_oidc (
    id INTEGER PRIMARY KEY,
    clave VARCHAR NOT NULL,
    datos TEXT NOT NULL,
    expira_en DATETIME NOT NULL
)
"""

CREAR_SESIONES_NUEVA = """
CREATE TABLE crm_sesiones (
    id INTEGER PRIMARY KEY,
    token_hash VARCHAR NOT NULL,
    usuario_id INTEGER NOT NULL,
    csrf VARCHAR NOT NULL,
    creada_en DATETIME NOT NULL,
    expira_en DATETIME NOT NULL,
    revocada_en DATETIME
)
"""


def test_una_base_limpia_pasa():
    """Sin ninguna tabla del CRM todavía: es el caso de una base nueva, y
    `init_db()` las crea después."""
    verificar_esquema(_engine_con())


def test_el_esquema_nuevo_pasa():
    verificar_esquema(_engine_con(CREAR_SESIONES_NUEVA))


def test_la_tabla_de_transacciones_oidc_corta_el_arranque():
    """Era exclusiva del flujo con Auth0: si está, la base es de antes."""
    with pytest.raises(EsquemaCrmViejo) as error:
        verificar_esquema(_engine_con(CREAR_SESIONES_NUEVA, CREAR_TRANSACCIONES_OIDC))

    assert "crm_transacciones_oidc" in str(error.value)
    assert "DROP TABLE" in str(error.value)


def test_una_tabla_de_sesiones_con_la_columna_sub_corta_el_arranque():
    """`sub` era el identificador que daba Auth0. Con esa columna, la tabla
    es la vieja: no se puede ni insertar una sesión nueva (falta `usuario_id`
    y `sub` es NOT NULL), y sus filas viejas seguirían ahí."""
    with pytest.raises(EsquemaCrmViejo) as error:
        verificar_esquema(_engine_con(CREAR_SESIONES_VIEJA))

    assert "crm_sesiones" in str(error.value)


def test_el_mensaje_dice_exactamente_qué_hacer():
    """Es lo que alguien va a leer en el log de un deploy que no levantó: si
    no dice el comando, hay que ir a buscarlo al código."""
    with pytest.raises(EsquemaCrmViejo) as error:
        verificar_esquema(_engine_con(CREAR_SESIONES_VIEJA, CREAR_TRANSACCIONES_OIDC))

    mensaje = str(error.value)
    assert "DROP TABLE IF EXISTS crm_transacciones_oidc;" in mensaje
    assert "DROP TABLE IF EXISTS crm_sesiones;" in mensaje


def test_la_base_de_la_suite_tiene_el_esquema_nuevo(db):
    """La que usan el resto de los tests: si esto fallara, todos los demás
    estarían corriendo contra un esquema que la app rechazaría al arrancar."""
    from app.db import obtener_engine

    verificar_esquema(obtener_engine())
