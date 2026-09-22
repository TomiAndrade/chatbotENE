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
from app.meta import ESPERA_INICIAL_SEGUNDOS, MAX_REINTENTOS
from app.respuesta import PRESUPUESTO_TOTAL_SEGUNDOS

PROVEEDORES_VALIDOS = frozenset({"fijo", "openai_compat", "claude"})

# Margen fijo sobre el peor caso calculado de abajo, para cubrir latencia
# real de red/base que las constantes de PRESUPUESTO_TOTAL_SEGUNDOS y
# MAX_REINTENTOS/ESPERA_INICIAL_SEGUNDOS no capturan (esas miden trabajo de
# CPU/espera propia, no cuánto tarda de verdad un socket contra Meta o
# contra Postgres bajo carga).
MARGEN_ABANDONO_SEGUNDOS = 10.0

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

    errores.extend(_errores_agrupamiento(config))
    errores.extend(_errores_crm(config))
    errores.extend(_errores_costo_meta(config))

    if errores:
        detalle = "\n".join(f"- {error}" for error in errores)
        raise ConfigInvalida(f"Configuración inválida al arrancar:\n{detalle}")


def minimo_seguro_agrupar_abandono_segundos(config: Config) -> float:
    """El piso de AGRUPAR_ABANDONO_SEGUNDOS para que no le robe la reserva a
    una generación que todavía está en curso, legítimamente (ver
    specs/spec-agrupamiento-mensajes.md, "Coherencia del timeout de
    abandono"). Se calcula a partir de las constantes reales del resto del
    sistema — no se copia el número a mano en un comentario — así que si
    `PRESUPUESTO_TOTAL_SEGUNDOS` (app/respuesta.py) o los reintentos de
    envío a Meta (app/meta.py) cambian, esta cuenta se ajusta sola en vez de
    quedar desincronizada en silencio.

    Peor caso de un lote que sí es legítimo: toda la espera de agrupamiento
    (`AGRUPAR_ESPERA_MAXIMA_SEGUNDOS`) + la llamada al modelo con su propio
    reintento (`PRESUPUESTO_TOTAL_SEGUNDOS`) + los reintentos de envío a
    Meta (backoff de `ESPERA_INICIAL_SEGUNDOS` que se duplica en cada
    intento, hasta `MAX_REINTENTOS`) + un margen fijo por la latencia real
    de red/base que ninguna de esas constantes mide.
    """
    backoff_envio_meta = sum(
        ESPERA_INICIAL_SEGUNDOS * (2**intento) for intento in range(MAX_REINTENTOS - 1)
    )
    return (
        config.agrupar_espera_maxima_segundos
        + PRESUPUESTO_TOTAL_SEGUNDOS
        + backoff_envio_meta
        + MARGEN_ABANDONO_SEGUNDOS
    )


def _errores_agrupamiento(config: Config) -> list[str]:
    """Agrupamiento de mensajes consecutivos (ver
    specs/spec-agrupamiento-mensajes.md). Los tres AGRUPAR_* tienen que ser
    positivos — un valor en cero o negativo rompe la lógica de la ventana de
    espera y del abandono, no solo la vuelve rara — y el abandono tiene que
    quedar por encima del piso seguro calculado más arriba."""
    errores: list[str] = []

    for nombre_variable, valor in (
        ("AGRUPAR_VENTANA_SEGUNDOS", config.agrupar_ventana_segundos),
        ("AGRUPAR_ESPERA_MAXIMA_SEGUNDOS", config.agrupar_espera_maxima_segundos),
        ("AGRUPAR_ABANDONO_SEGUNDOS", config.agrupar_abandono_segundos),
    ):
        if valor <= 0:
            errores.append(f"{nombre_variable}={valor!r} tiene que ser mayor a 0.")

    if config.agrupar_espera_maxima_segundos > 0 and config.agrupar_abandono_segundos > 0:
        minimo = minimo_seguro_agrupar_abandono_segundos(config)
        if config.agrupar_abandono_segundos <= minimo:
            errores.append(
                f"AGRUPAR_ABANDONO_SEGUNDOS={config.agrupar_abandono_segundos!r} es demasiado bajo: "
                f"tiene que ser mayor a {minimo:.0f}s (AGRUPAR_ESPERA_MAXIMA_SEGUNDOS + el "
                "presupuesto de la llamada al modelo + los reintentos de envío a Meta + margen). "
                "Con un valor más bajo, una generación legítima que todavía está en curso puede "
                "perder su reserva antes de terminar y otro proceso la retoma encima de ella."
            )

    return errores


def _errores_costo_meta(config: Config) -> list[str]:
    """Control preventivo de gasto de WhatsApp/Meta (ver
    specs/spec-costo-whatsapp-meta.md, etapa 1). Mismo criterio que
    `_errores_agrupamiento`: valores en cero o negativos no tienen sentido de
    negocio (una tarifa o un presupuesto en $0 no es "gratis", es una
    configuración a medio cargar) y arrancar así dejaría el costo estimado y
    el porcentaje de consumo mostrando cualquier cosa en el CRM."""
    errores: list[str] = []

    if config.meta_tarifa_service_ars <= 0:
        errores.append(
            f"META_TARIFA_SERVICE_ARS={config.meta_tarifa_service_ars!r} tiene que ser mayor a 0."
        )
    if config.meta_presupuesto_mensual_ars <= 0:
        errores.append(
            f"META_PRESUPUESTO_MENSUAL_ARS={config.meta_presupuesto_mensual_ars!r} tiene que ser mayor a 0."
        )

    return errores


HOSTS_LOCALES = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _errores_crm(config: Config) -> list[str]:
    """El CRM viene apagado (`CRM_HABILITADO=false`, el default): con el
    panel apagado no se registra ninguna de sus rutas (ver app/main.py) y
    nada de esto se valida, así que quien solo corre el bot no tiene que
    configurar nada nuevo.

    Prendido, lo único que hace falta configurar es `CRM_BASE_URL`: el login
    es propio y las cuentas viven en la base, no en variables de entorno
    (**no hay ninguna contraseña en la config**, ni compartida ni de nadie).
    Media configuración no arranca — un panel que se sirve por http en
    producción manda la cookie de sesión sin `Secure`, que es exactamente la
    falla silenciosa que esta validación existe para evitar
    (spec-validacion-config-arranque.md).

    Que **haya** al menos una cuenta con la que entrar no se valida acá: eso
    se sabe recién con la base abierta, y lo avisa `al_iniciar()` con un
    WARNING después de `init_db()`.
    """
    if not config.crm_habilitado:
        return []

    return _errores_base_url_crm(config)


def _errores_base_url_crm(config: Config) -> list[str]:
    """CRM_BASE_URL es la URL pública del panel. Se configura y no se deduce
    del request: el header `Host` (o un `X-Forwarded-Host`) lo elige quien
    manda el request, así que no sirve para decidir si la conexión es segura.

    HTTPS obligatorio, con una sola excepción explícita: desarrollo local
    (`DEBUG=true` y host localhost). Producción con http queda rechazada,
    aunque alguien la escriba a propósito: sin https la cookie de sesión y la
    contraseña del login viajan en claro.
    """
    if _vacio(config.crm_base_url):
        return [
            "CRM_BASE_URL es obligatoria con CRM_HABILITADO=true "
            "(p. ej. https://bot.ene.example o http://localhost:8000 en desarrollo)."
        ]

    partes = urlparse(config.crm_base_url)
    if partes.scheme not in ("http", "https") or not partes.hostname:
        return [
            f"CRM_BASE_URL={config.crm_base_url!r} no es una URL válida: "
            "tiene que ser http(s)://host[:puerto], sin ruta ni barra final."
        ]
    if partes.path:
        return [f"CRM_BASE_URL={config.crm_base_url!r} no puede tener ruta: solo esquema, host y puerto."]

    if partes.scheme == "https":
        return []
    if partes.hostname in HOSTS_LOCALES and config.debug:
        return []
    return [
        f"CRM_BASE_URL={config.crm_base_url!r} usa http. Solo se permite http "
        "en desarrollo local: host localhost/127.0.0.1 y DEBUG=true. "
        "En producción el panel necesita https (la cookie de sesión sale con el flag Secure)."
    ]


def crm_sobre_https(config: Config) -> bool:
    """Si el panel se sirve por https. Decide el flag `Secure` de las
    cookies: prendido siempre, salvo en el http local que permite
    `_errores_base_url_crm` — con Secure el navegador descartaría la cookie
    en http://localhost y no se podría probar nada."""
    return config.crm_base_url.startswith("https://")


def _resumen_crm(config: Config) -> str:
    """Solo si el panel está prendido. Cuántas cuentas hay no va acá: esto se
    loguea antes de que exista el engine de la base."""
    return "activo" if config.crm_habilitado else "apagado"


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
        f"escalamiento_habilitado={config.escalamiento_habilitado}, "
        # Del CRM, solo si está prendido. Ni quién tiene cuenta ni cuántas
        # son: son datos de personas y no hacen falta en el log de arranque.
        f"crm={_resumen_crm(config)}"
    )
