"""Tablas propias del CRM: las cuentas del panel, sus sesiones y el registro
de intentos de ingreso.

Viven en su módulo y no en `app/models.py` porque no son del dominio del bot
—no hay nada acá que el webhook mire— pero comparten la misma `Base`, así que
`init_db()` las crea junto con el resto (ver `app/db.py`, que importa este
módulo a propósito). En una base que ya existe, `create_all` agrega las tablas
nuevas y **no toca `conversaciones` ni `mensajes`**.

`crm_usuarios` es la única que **no** es descartable: son las cuentas. Las
otras dos sí — vaciarlas solo obliga a volver a entrar y borra el historial
de intentos.
"""

import logging

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, inspect
from sqlalchemy.orm import relationship

from app.db import Base
from app.models import ahora_utc

logger = logging.getLogger("bot")


class UsuarioCrm(Base):
    """Una cuenta del panel.

    **De la contraseña solo se guarda su hash Argon2id** (ver
    `app/crm/passwords.py`): la contraseña en claro existe nada más que en el
    momento en que alguien la tipea, ni en la base ni en el log.

    Las cuentas se crean **solo** desde la consola
    (`scripts/crm_usuario.py`): no hay registro público ni alta desde el
    panel.
    """

    __tablename__ = "crm_usuarios"

    id = Column(Integer, primary_key=True)
    # El nombre con el que se entra. Se guarda normalizado (minúsculas, sin
    # espacios alrededor, ver `app/crm/usuarios.py:normalizar`) para que
    # "Tomi" y "tomi" no sean dos cuentas distintas.
    usuario = Column(String, unique=True, nullable=False, index=True)
    hash_password = Column(String, nullable=False)
    # Desactivar en vez de borrar: la cuenta deja de entrar pero el rastro de
    # quién reactivó qué conversación sigue teniendo a quién apuntar.
    activo = Column(Boolean, default=True, nullable=False)
    creado_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False)
    # Cuándo cambió por última vez algo que invalida las sesiones abiertas:
    # la contraseña o el estado de la cuenta. `buscar_sesion_valida` compara
    # contra `creada_en` de la sesión, así que una sesión anterior a este
    # instante deja de servir sola, sin depender de que alguien se haya
    # acordado de revocarla (ver `app/crm/sesiones.py`).
    credenciales_cambiadas_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False)

    sesiones = relationship("SesionCrm", back_populates="usuario")


class SesionCrm(Base):
    """Una sesión abierta del panel.

    **No se guarda el valor de la cookie**, solo su SHA-256: si alguien lee
    la base (un dump, un backup, una consulta de diagnóstico) no se lleva
    nada con lo que pueda entrar. El valor real existe una sola vez, en el
    navegador de quien se logueó.
    """

    __tablename__ = "crm_sesiones"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String, unique=True, nullable=False, index=True)
    usuario_id = Column(Integer, ForeignKey("crm_usuarios.id"), nullable=False, index=True)
    # Token anti-CSRF de esta sesión: viaja en el header X-CRM-CSRF de las
    # acciones que escriben. Vive en la base, no en la cookie.
    csrf = Column(String, nullable=False)
    creada_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False)
    # Vencimiento absoluto, fijado al crearla: no se renueva con el uso. Una
    # sesión olvidada abierta se muere sola a las DURACION_SESION horas.
    expira_en = Column(DateTime(timezone=True), nullable=False)
    # Cuándo se cerró sesión (o se revocó). Se marca en vez de borrar la fila
    # para que quede el rastro hasta que la limpieza se la lleve.
    revocada_en = Column(DateTime(timezone=True), nullable=True)

    usuario = relationship("UsuarioCrm", back_populates="sesiones")


class IntentoLoginCrm(Base):
    """Un intento de ingreso, exitoso o no.

    Existe para poder limitar los intentos **contando contra la base y no en
    memoria del proceso**: con dos instancias del servidor detrás de un
    balanceador, un contador en memoria deja pasar el doble de intentos y se
    borra en cada deploy. Es la misma decisión que ya toma `app/limite.py`
    para el límite de mensajes por hora.

    El `usuario` que se guarda es **el que se intentó**, exista o no esa
    cuenta: contar también los nombres inventados es lo que evita que se
    pueda usar el bloqueo para averiguar qué cuentas existen.
    """

    __tablename__ = "crm_intentos_login"

    id = Column(Integer, primary_key=True)
    usuario = Column(String, nullable=False, index=True)
    # De qué IP vino. Puede ser None si el servidor está detrás de algo que
    # no la pasa: en ese caso queda el límite por usuario, que es el que
    # protege la cuenta.
    ip = Column(String, nullable=True, index=True)
    exitoso = Column(Boolean, nullable=False)
    creado_en = Column(DateTime(timezone=True), default=ahora_utc, nullable=False, index=True)


# --- Restos del login anterior (Auth0) -----------------------------------

# Las tablas que usaba el login con Auth0. Ya no existen como modelos: se
# borraron junto con el resto de ese mecanismo.
TABLAS_DEL_LOGIN_VIEJO = ("crm_transacciones_oidc",)

# La columna que tenía `crm_sesiones` cuando la identidad la daba Auth0. Si
# está, la tabla es la vieja y `create_all` no la migra (no hay migraciones
# en este proyecto, ver `app/db.py:init_db`).
COLUMNA_DEL_LOGIN_VIEJO = "sub"


class EsquemaCrmViejo(RuntimeError):
    """La base todavía tiene el esquema del CRM con Auth0."""


def verificar_esquema(engine) -> None:
    """Corta el arranque si quedaron tablas del login con Auth0.

    Este proyecto no tiene migraciones: `create_all` crea lo que falta y no
    modifica nada existente. Una `crm_sesiones` de la época de Auth0 tiene
    `sub NOT NULL` y ninguna columna `usuario_id`, así que el panel no
    podría ni crear una sesión — y, peor, las filas viejas seguirían ahí.
    Mejor no arrancar y decir qué hacer que arrancar roto.

    Se llama desde `al_iniciar()` (`app/main.py`), después de `init_db()`.
    """
    inspector = inspect(engine)
    tablas = set(inspector.get_table_names())

    restos = [tabla for tabla in TABLAS_DEL_LOGIN_VIEJO if tabla in tablas]
    if SesionCrm.__tablename__ in tablas:
        columnas = {columna["name"] for columna in inspector.get_columns(SesionCrm.__tablename__)}
        if COLUMNA_DEL_LOGIN_VIEJO in columnas:
            restos.append(SesionCrm.__tablename__)

    if not restos:
        return

    raise EsquemaCrmViejo(
        "La base tiene el esquema del CRM anterior (login con Auth0): "
        + ", ".join(sorted(restos))
        + ". El login ahora es propio y esas tablas no se migran solas. "
        "Las tres son descartables (lo único que se pierde son las sesiones "
        "abiertas, y no había cuentas locales todavía): borralas y volvé a "
        "arrancar, que init_db() crea las nuevas.\n"
        "    DROP TABLE IF EXISTS crm_transacciones_oidc;\n"
        "    DROP TABLE IF EXISTS crm_sesiones;"
    )
