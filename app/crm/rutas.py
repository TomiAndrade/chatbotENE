"""Router del CRM: las páginas, sus archivos estáticos, el login y los
endpoints JSON que consume el panel.

Todo cuelga de `/crm`, y todo lo que muestre o toque conversaciones exige
sesión válida (`requiere_sesion`). El webhook queda fuera de esto: no
comparte autenticación, ni secreto, ni cookie — `POST /webhook` sigue
validándose con la firma de Meta como siempre, y `/health` tampoco pasa por
acá.

Lo único abierto son la pantalla de entrar, el POST del login y los cuatro
archivos estáticos (hoja de estilos, los dos JavaScript y el logo). No traen
ningún dato: hay que poder bajarlos justamente para poder loguearse.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import config
from app.crm import intentos, servicio, sesiones, usuarios
from app.crm.auth import (
    borrar_cookie_de_sesion,
    poner_cookie_de_sesion,
    requiere_sesion,
    sesion_actual,
    sin_cache,
    token_de_sesion,
    verificar_csrf,
)
from app.crm.servicio import obtener_db
from app.pausa import reactivar_bot

logger = logging.getLogger("bot")

router = APIRouter(prefix="/crm", tags=["crm"])

_DIRECTORIO = Path(__file__).resolve().parent
_PAGINAS = _DIRECTORIO / "paginas"
_ESTATICOS = _DIRECTORIO / "estaticos"

# Lista explícita en vez de montar el directorio entero: así no hay forma de
# pedir un archivo que no esté acá (ni de salirse de la carpeta con "..").
ARCHIVOS_ESTATICOS = {
    "crm.css": "text/css; charset=utf-8",
    "panel.js": "text/javascript; charset=utf-8",
    "login.js": "text/javascript; charset=utf-8",
    "logo-ene.png": "image/png",
}

# El texto que el panel muestra después de reactivar. Vive en el servidor
# para que los tests puedan afirmarlo contra esta constante y no contra una
# copia escrita a mano en el JavaScript.
MENSAJE_BOT_REACTIVADO = "Bot reactivado. Responderá cuando llegue un nuevo mensaje"

# **El mismo texto para los tres fallos posibles**: la cuenta no existe, está
# desactivada, o la contraseña está mal. Si dijeran cosas distintas, el login
# serviría para averiguar qué cuentas hay.
ERROR_CREDENCIALES = "Usuario o contraseña incorrectos."

ERROR_DEMASIADOS_INTENTOS = "Demasiados intentos fallidos. Esperá unos minutos y volvé a probar."

LIMITE_CONVERSACIONES_MAXIMO = 100


def _pagina(nombre: str, status_code: int = 200) -> HTMLResponse:
    """Sirve una de las páginas del panel sin caché. Se leen del disco en cada
    request: son dos archivos chicos y así un cambio de estilo no necesita
    reiniciar el server."""
    contenido = (_PAGINAS / nombre).read_text(encoding="utf-8")
    respuesta = HTMLResponse(contenido, status_code=status_code)
    sin_cache(respuesta)
    return respuesta


# --- Páginas -------------------------------------------------------------


@router.get("/login")
def pagina_login(request: Request, db: Session = Depends(obtener_db)):
    """La pantalla con el formulario de usuario y contraseña. Si ya hay
    sesión válida, no tiene sentido mostrarla."""
    if sesion_actual(request, db) is not None:
        return RedirectResponse(url="/crm", status_code=303)
    return _pagina("login.html")


@router.get("")
def pagina_panel(request: Request, db: Session = Depends(obtener_db)):
    """El panel en sí. Sin sesión no se sirve el HTML: redirige al login."""
    if sesion_actual(request, db) is None:
        return RedirectResponse(url="/crm/login", status_code=303)
    return _pagina("panel.html")


@router.get("/estaticos/{archivo}")
def archivo_estatico(archivo: str):
    tipo = ARCHIVOS_ESTATICOS.get(archivo)
    if tipo is None:
        raise HTTPException(status_code=404, detail="No existe")
    return FileResponse(_ESTATICOS / archivo, media_type=tipo)


# --- Login ---------------------------------------------------------------


class DatosDeLogin(BaseModel):
    """El cuerpo del POST de login.

    Es **JSON y no un formulario** a propósito: un formulario de otro sitio
    puede hacer un POST cruzado sin JavaScript, pero no puede mandar
    `Content-Type: application/json` sin que el navegador pida permiso antes
    (preflight de CORS), y acá no hay CORS abierto. Sumado a la cookie
    `SameSite=Lax`, no hay forma de disparar este endpoint desde otra página.
    """

    usuario: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


def _error_de_login(detalle: str, status_code: int) -> JSONResponse:
    respuesta = JSONResponse({"detail": detalle}, status_code=status_code)
    sin_cache(respuesta)
    return respuesta


@router.post("/api/login")
def login(datos: DatosDeLogin, request: Request, db: Session = Depends(obtener_db)):
    """Entrar con usuario y contraseña.

    El orden importa, y es el mismo para una cuenta que existe y para una que
    no:

    1. **Primero el límite de intentos** (`app/crm/intentos.py`), contado
       contra la base para que funcione igual con varias instancias del
       servidor. Con la cuenta bloqueada no se mira la contraseña: si se
       mirara, el bloqueo no frenaría a quien acierta en el intento número
       seis.
    2. Después la contraseña, con Argon2id. `usuarios.autenticar` tarda lo
       mismo exista o no la cuenta (verifica contra un hash de relleno), así
       que el reloj tampoco dice qué cuentas hay.
    3. Sesión nueva siempre, revocando la que viniera en la cookie: nadie
       puede plantar una cookie y esperar a que alguien se loguee con ella
       (fijación de sesión).

    Los tres fallos posibles —no existe, desactivada, contraseña mal—
    devuelven **el mismo 401 con el mismo texto**.
    """
    nombre = usuarios.normalizar(datos.usuario)
    ip = intentos.ip_del_request(request)

    # Momento natural para la limpieza: si nadie entra al panel, tampoco se
    # acumulan filas nuevas.
    sesiones.limpiar_vencidas(db)
    intentos.limpiar_viejos(db)

    if intentos.esta_bloqueado(db, nombre, ip):
        logger.warning("CRM: login bloqueado por demasiados intentos (usuario=%s)", nombre)
        return _error_de_login(ERROR_DEMASIADOS_INTENTOS, 429)

    usuario = usuarios.autenticar(db, nombre, datos.password)
    if usuario is None:
        intentos.registrar(db, nombre, ip, exitoso=False)
        logger.warning("CRM: login fallido (usuario=%s)", nombre)
        return _error_de_login(ERROR_CREDENCIALES, 401)

    sesiones.revocar_token(db, token_de_sesion(request))
    token_nuevo, sesion = sesiones.crear_sesion(db, usuario)
    intentos.registrar(db, nombre, ip, exitoso=True)
    logger.info("CRM: login correcto de %s (sesión %s)", usuario.usuario, sesion.id)

    respuesta = JSONResponse({"usuario": usuario.usuario})
    sin_cache(respuesta)
    poner_cookie_de_sesion(respuesta, token_nuevo)
    return respuesta


# --- API del panel -------------------------------------------------------


@router.get("/api/sesion")
def datos_sesion(sesion=Depends(requiere_sesion)):
    """Lo que el panel necesita al arrancar: quién está logueado, el token
    CSRF para las acciones que escriben y en qué zona horaria mostrar las
    fechas (la misma `TIMEZONE` que usa el bot)."""
    return {
        "usuario": sesion.usuario.usuario,
        "csrf": sesion.csrf,
        "zona_horaria": str(config.timezone),
    }


@router.post("/api/logout")
def logout(request: Request, sesion=Depends(requiere_sesion), db: Session = Depends(obtener_db)):
    """Cierra la sesión del panel: revoca la fila en la base y borra la
    cookie. La cookie deja de servir en el acto, incluso si alguien tenía una
    copia guardada.

    Es POST y con token CSRF a propósito: un GET de logout lo puede disparar
    cualquier página con una imagen apuntada acá.

    Sin proveedor externo no hay una segunda sesión que cerrar: con Auth0 el
    logout tenía que mandar además al navegador al logout del proveedor, y
    eso ya no existe.
    """
    verificar_csrf(request, sesion)
    nombre = sesion.usuario.usuario
    sesiones.revocar_sesion(db, sesion)
    logger.info("CRM: logout de %s (sesión %s)", nombre, sesion.id)

    respuesta = JSONResponse({"ok": True})
    sin_cache(respuesta)
    borrar_cookie_de_sesion(respuesta)
    return respuesta


@router.get("/api/conversaciones")
def listar_conversaciones(
    filtro: str = Query(default="todas", pattern="^(todas|pausadas)$"),
    limite: int = Query(default=40, ge=1, le=LIMITE_CONVERSACIONES_MAXIMO),
    desplazamiento: int = Query(default=0, ge=0),
    sesion=Depends(requiere_sesion),
    db: Session = Depends(obtener_db),
):
    return servicio.listar_conversaciones(
        db, solo_pausadas=(filtro == "pausadas"), limite=limite, desplazamiento=desplazamiento
    )


def _conversacion_o_404(db: Session, conversacion_id: int):
    conversacion = servicio.buscar_conversacion(db, conversacion_id)
    if conversacion is None:
        raise HTTPException(status_code=404, detail="No existe esa conversación")
    return conversacion


@router.get("/api/conversaciones/{conversacion_id}/mensajes")
def mensajes_de_conversacion(
    conversacion_id: int,
    limite: int = Query(default=servicio.MENSAJES_POR_PAGINA, ge=1, le=200),
    antes_de: int | None = Query(default=None, ge=1),
    desde: int | None = Query(default=None, ge=0),
    sesion=Depends(requiere_sesion),
    db: Session = Depends(obtener_db),
):
    """Una página del historial más el estado actual de la conversación. El
    estado viaja en la misma respuesta porque el panel lo refresca junto con
    los mensajes: si alguien escaló mientras estabas leyendo, el cartel de
    pausa tiene que aparecer sin recargar la página."""
    conversacion = _conversacion_o_404(db, conversacion_id)
    pagina = servicio.mensajes_de(db, conversacion, limite=limite, antes_de=antes_de, desde=desde)
    return {"conversacion": servicio.detalle_conversacion(db, conversacion), **pagina}


@router.post("/api/conversaciones/{conversacion_id}/reactivar")
def reactivar_conversacion(
    conversacion_id: int,
    request: Request,
    sesion=Depends(requiere_sesion),
    db: Session = Depends(obtener_db),
):
    """Apaga la pausa. Lo único que escribe el CRM sobre las conversaciones.

    No manda nada por WhatsApp ni vuelve a procesar mensajes viejos: deja la
    conversación como si nunca se hubiera pausado y el bot responde recién
    cuando llegue un mensaje nuevo, con las reglas de siempre. Los mensajes
    quedan todos donde están.
    """
    verificar_csrf(request, sesion)
    conversacion = _conversacion_o_404(db, conversacion_id)

    reactivar_bot(conversacion)
    db.commit()

    logger.info(
        "CRM: %s reactivó el bot en la conversación %s", sesion.usuario.usuario, conversacion.id,
    )
    return {
        "conversacion": servicio.detalle_conversacion(db, conversacion),
        "mensaje": MENSAJE_BOT_REACTIVADO,
    }


@router.get("/api/motivos")
def motivos(sesion=Depends(requiere_sesion)):
    return servicio.motivos_legibles()
