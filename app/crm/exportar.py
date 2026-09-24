"""Exportación de una conversación completa a Markdown, desde su detalle en
el CRM. Uso principal: bajar el historial para analizarlo con una IA aparte.

Alcance mínimo a propósito (pedido explícito, 22/09/2026): sin anonimización,
sin resumen con IA, una conversación a la vez. Solo arma el texto — la
consulta que trae TODOS los mensajes, sin la paginación del panel, vive en
`app/crm/servicio.py` (`todos_los_mensajes`), como el resto de las consultas
del CRM.
"""

from datetime import datetime, timezone

from app.config import config
from app.crm.servicio import _a_utc
from app.models import Conversacion, Mensaje, RolMensaje

NOMBRE_DE_ROL = {
    RolMensaje.USUARIO: "Usuario",
    RolMensaje.BOT: "Bot",
    RolMensaje.HUMANO: "Humano",
}

# El único valor de Mensaje.tipo que participa del flujo normal (agrupa y
# llama al modelo, ver TIPO_TEXTO en app/main.py). Cualquier otro valor no
# nulo es un adjunto no soportado (specs/spec-adjuntos-no-soportados.md): la
# base no guarda contenido de archivo para esos, solo el tipo real que mandó
# Meta.
TIPO_TEXTO = "text"


def nombre_de_archivo(conversacion_id: int) -> str:
    """conversacion-<id>.md. El id ya viene validado como int por la ruta de
    FastAPI, así que no hace falta sanitizar nada más: no puede traer "/",
    ".." ni ningún carácter que no sea un dígito."""
    return f"conversacion-{conversacion_id}.md"


def _fecha_local(fecha: datetime | None) -> str:
    if fecha is None:
        return "(sin fecha)"
    return _a_utc(fecha).astimezone(config.timezone).strftime("%d/%m/%Y %H:%M")


def _cuerpo_del_mensaje(mensaje: Mensaje) -> str:
    """El texto tal cual está guardado, salvo un adjunto no soportado: ahí la
    base no tiene contenido de archivo (solo un marcador de texto para
    diagnóstico, ver `_extraer_contenido` en app/main.py), así que se marca
    con el tipo real (`Mensaje.tipo`) en vez de reexponer ese marcador como
    si fuera lo que escribió la persona."""
    if mensaje.tipo and mensaje.tipo != TIPO_TEXTO:
        return f"_[Adjunto recibido — tipo: {mensaje.tipo}. Sin contenido de archivo guardado en esta etapa.]_"
    return mensaje.contenido


def generar_markdown(
    conversacion: Conversacion,
    mensajes: list[Mensaje],
    autores: dict[int, str],
) -> str:
    """El Markdown completo de `conversacion`. `mensajes` tiene que ser el
    historial entero, no una página — quien llama es responsable de traerlo
    así (`servicio.todos_los_mensajes`).

    El contenido de cada mensaje se escribe tal cual está guardado, sin
    modificarlo (alcance mínimo: nada de anonimizar ni tocar el contenido
    libre). Nunca se agregan datos que no estén ya en la base."""
    lineas = [
        f"# Conversación #{conversacion.id}",
        "",
        f"- Canal: {conversacion.canal}",
        f"- Identificador: {conversacion.identificador_externo}",
        f"- Iniciada: {_fecha_local(conversacion.creada_en)}",
        f"- Exportada: {_fecha_local(datetime.now(timezone.utc))}",
        f"- Zona horaria: {config.timezone}",
        "",
        "## Historial",
        "",
    ]

    if not mensajes:
        lineas.append("_Esta conversación todavía no tiene mensajes._")
    else:
        for mensaje in mensajes:
            rol = NOMBRE_DE_ROL.get(mensaje.rol, mensaje.rol.value)
            if mensaje.rol == RolMensaje.HUMANO and mensaje.autor_crm_id in autores:
                rol = f"{rol} ({autores[mensaje.autor_crm_id]})"
            lineas.append(f"### {rol} — {_fecha_local(mensaje.creado_en)}")
            lineas.append(_cuerpo_del_mensaje(mensaje))
            lineas.append("")

    return "\n".join(lineas).rstrip() + "\n"
