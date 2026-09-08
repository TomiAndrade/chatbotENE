"""Validación de la configuración al arranque (spec-validacion-config-arranque.md).

Se invoca explícitamente desde el startup de FastAPI (`al_iniciar` en
app/main.py) — nunca al importar app.config. La suite de tests fija
DATABASE_URL a SQLite antes de importar app.config, y ese import no puede
disparar nada: por eso esto vive en un módulo aparte, no en app/config.py.

Sin modo dev: las reglas son las mismas en todos los entornos, no hay
variable que las relaje (ver la spec, sección "Decisión de diseño").
"""

from urllib.parse import urlparse

from app.config import Config

PROVEEDORES_VALIDOS = frozenset({"fijo", "openai_compat", "claude"})

# El proyecto usa psycopg2-binary (requirements.txt), que SQLAlchemy resuelve
# solo para el esquema "postgresql://" sin necesidad de un "+driver"
# explícito. No "postgresql+psycopg://": ese prefijo es de psycopg3, que no
# está instalado acá.
#
# La URL se valida cruda, tal cual viene en DATABASE_URL, porque ya no hay
# ninguna normalización de por medio: Render entrega `postgresql://`
# (verificado contra la base real, `ene-bot-db`).
PREFIJO_DATABASE_URL_VALIDO = "postgresql://"


class ConfigInvalida(Exception):
    """Configuración incompleta o incoherente para arrancar. El mensaje trae
    todos los problemas encontrados, no solo el primero (ver spec, regla 5)."""


def _vacio(valor: str) -> bool:
    return not valor or not valor.strip()


def validar_config(config: Config) -> None:
    """Junta todos los problemas de `config` y los reporta juntos en un
    ConfigInvalida. No aborta en el primer error encontrado."""
    errores: list[str] = []

    if _vacio(config.database_url):
        errores.append("DATABASE_URL es obligatoria y no puede estar vacía.")
    elif not config.database_url.startswith(PREFIJO_DATABASE_URL_VALIDO):
        # No se loguea la URL completa: puede traer usuario y contraseña.
        # El esquema (lo que va antes de "://") no es un secreto.
        esquema = config.database_url.split("://", 1)[0]
        errores.append(
            f"DATABASE_URL debe empezar con {PREFIJO_DATABASE_URL_VALIDO!r} "
            f"(no SQLite ni otro motor): el valor actual usa el esquema {esquema!r}."
        )

    if _vacio(config.proveedor_ia):
        errores.append("PROVEEDOR_IA es obligatoria y no puede estar vacía.")
    elif config.proveedor_ia not in PROVEEDORES_VALIDOS:
        errores.append(
            f"PROVEEDOR_IA={config.proveedor_ia!r} no es un proveedor conocido "
            f"(válidos: {', '.join(sorted(PROVEEDORES_VALIDOS))})."
        )

    for nombre_variable, valor in (
        ("META_ACCESS_TOKEN", config.meta_access_token),
        ("META_PHONE_NUMBER_ID", config.meta_phone_number_id),
        ("META_VERIFY_TOKEN", config.meta_verify_token),
        ("META_APP_SECRET", config.meta_app_secret),
    ):
        if _vacio(valor):
            errores.append(f"{nombre_variable} es obligatoria y no puede estar vacía.")

    if config.proveedor_ia == "openai_compat":
        if _vacio(config.openai_compat_api_key):
            errores.append(
                "OPENAI_COMPAT_API_KEY es obligatoria porque PROVEEDOR_IA=openai_compat."
            )
        if _vacio(config.base_url):
            errores.append("BASE_URL es obligatoria porque PROVEEDOR_IA=openai_compat.")
        if _vacio(config.modelo):
            errores.append("MODELO es obligatoria porque PROVEEDOR_IA=openai_compat.")
    elif config.proveedor_ia == "claude":
        if _vacio(config.anthropic_api_key):
            errores.append("ANTHROPIC_API_KEY es obligatoria porque PROVEEDOR_IA=claude.")
        if _vacio(config.modelo):
            errores.append("MODELO es obligatoria porque PROVEEDOR_IA=claude.")

    if errores:
        detalle = "\n".join(f"- {error}" for error in errores)
        raise ConfigInvalida(f"Configuración inválida al arrancar:\n{detalle}")


def _host_y_base(database_url: str) -> str:
    """Host (y puerto, si no es el default) más el nombre de la base, sin
    usuario ni contraseña — son las únicas partes de DATABASE_URL que no son
    secretas."""
    partes = urlparse(database_url)
    nombre_base = partes.path.lstrip("/")
    host = partes.hostname or "?"
    if partes.port:
        host = f"{host}:{partes.port}"
    return f"{host}/{nombre_base}"


def resumen_config(config: Config) -> str:
    """Línea para loguear al arrancar, con la configuración efectiva. Sin
    ningún secreto: ni tokens, ni keys, ni contraseñas, ni el usuario o
    contraseña de DATABASE_URL."""
    return (
        "Config de arranque — "
        f"proveedor_ia={config.proveedor_ia}, modelo={config.modelo or '(sin modelo)'}, "
        f"database={_host_y_base(config.database_url)}, "
        f"meta_api_version={config.meta_api_version}, "
        f"escalamiento_habilitado={config.escalamiento_habilitado}"
    )
