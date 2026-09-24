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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import atencion
from app.config import config
from app.crm import exportar, intentos, metricas, servicio, sesiones, usuarios
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
from app.envio import ResultadoEnvio, enviar_respuesta_humana
from app.meta import WHATSAPP_MAX_CARACTERES
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
    "metricas.js": "text/javascript; charset=utf-8",
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


@router.get("/metricas")
def pagina_metricas(request: Request, db: Session = Depends(obtener_db)):
    """El dashboard de costos y actividad (specs/spec-dashboard-metricas.md).
    Misma sesión que el panel de conversaciones — sin sesión, al login."""
    if sesion_actual(request, db) is None:
        return RedirectResponse(url="/crm/login", status_code=303)
    return _pagina("metricas.html")


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


@router.get("/api/conversaciones/{conversacion_id}/exportar")
def exportar_conversacion(
    conversacion_id: int,
    sesion=Depends(requiere_sesion),
    db: Session = Depends(obtener_db),
):
    """Descarga el historial completo de la conversación en Markdown, para
    analizarlo con una IA aparte. Trae SIEMPRE todos los mensajes
    (`servicio.todos_los_mensajes`), nunca la página que esté viendo el
    panel en ese momento.

    GET y sin CSRF a propósito, igual que el resto de las lecturas del panel
    (`/api/conversaciones`, `/api/conversaciones/{id}/mensajes`): no escribe
    nada, así que no aplica la protección que sí exigen las acciones de
    atención, responder, reactivar y salir
    (ver `verificar_csrf` en app/crm/auth.py).
    """
    conversacion = _conversacion_o_404(db, conversacion_id)
    mensajes = servicio.todos_los_mensajes(db, conversacion)
    autores = servicio.nombres_de_autores(db, mensajes)
    contenido = exportar.generar_markdown(conversacion, mensajes, autores)

    logger.info(
        "CRM: %s exportó la conversación %s (%s mensajes)",
        sesion.usuario.usuario, conversacion.id, len(mensajes),
    )

    respuesta = Response(
        content=contenido.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{exportar.nombre_de_archivo(conversacion.id)}"',
        },
    )
    sin_cache(respuesta)
    return respuesta


@router.post("/api/conversaciones/{conversacion_id}/reactivar")
def reactivar_conversacion(
    conversacion_id: int,
    request: Request,
    sesion=Depends(requiere_sesion),
    db: Session = Depends(obtener_db),
):
    """Apaga la pausa.

    Si la conversación tiene una atención abierta, esto **es** resolverla y
    sigue las mismas reglas que `resolver_atencion`: solo su responsable, y
    una pendiente hay que tomarla primero (409 en los dos casos). Si no,
    este botón sería una forma de cerrar la atención de otra persona.

    No manda nada por WhatsApp ni vuelve a procesar mensajes viejos: deja la
    conversación como si nunca se hubiera pausado y el bot responde recién
    cuando llegue un mensaje nuevo, con las reglas de siempre. Los mensajes
    quedan todos donde están.
    """
    verificar_csrf(request, sesion)
    conversacion = _conversacion_o_404(db, conversacion_id)

    abierta = atencion.atencion_abierta(db, conversacion.id)
    if abierta is not None:
        _resolver_o_409(db, abierta, sesion.usuario.id)
        db.refresh(conversacion)
    else:
        reactivar_bot(conversacion)
        db.commit()

    logger.info(
        "CRM: %s reactivó el bot en la conversación %s", sesion.usuario.usuario, conversacion.id,
    )
    return {
        "conversacion": servicio.detalle_conversacion(db, conversacion),
        "mensaje": MENSAJE_BOT_REACTIVADO,
    }


# --- Atención humana (app/atencion.py) -----------------------------------
#
# Las acciones van por conversación y no por id de atención: una conversación
# tiene como mucho una atención abierta, así que el panel no necesita
# conocer otro id. Todas escriben, así que todas exigen CSRF.

ERROR_ATENCION_YA_ABIERTA = "Esta conversación ya tiene una atención abierta."
ERROR_ATENCION_NO_DISPONIBLE = "Otra persona ya tomó esta conversación o ya se resolvió."
ERROR_SIN_ATENCION_ABIERTA = "Esta conversación no tiene una atención abierta."
ERROR_ATENCION_SIN_TOMAR = "Para resolver esta conversación, primero hay que tomarla."
ERROR_ATENCION_AJENA = "Esta conversación la tiene otra persona: solo quien la tomó puede resolverla."

# Un código por motivo de rechazo del flujo de atención humana (iniciar,
# tomar, resolver, reactivar cuando hay una atención de por medio, y
# responder). Todos los errores propios de ese flujo van por
# `_error_estructurado`, nunca por un `detail` de string suelto — así el
# Kanban tiene un único contrato para los ocho, en vez de tener que
# distinguir "string" de "{code, message}" según qué acción falló.
CODIGO_ATENCION_YA_ABIERTA = "atencion_ya_abierta"
CODIGO_SIN_ATENCION_ABIERTA = "sin_atencion_abierta"
CODIGO_ATENCION_NO_DISPONIBLE = "atencion_no_disponible"
CODIGO_ATENCION_SIN_TOMAR = "atencion_sin_tomar"
CODIGO_ATENCION_AJENA = "atencion_ajena"
CODIGO_PRESUPUESTO_AGOTADO = "presupuesto_agotado"
CODIGO_FUERA_DE_VENTANA = "fuera_de_ventana"
CODIGO_FALLO_META = "fallo_meta"
CODIGO_TEXTO_INVALIDO = "texto_invalido"


class DatosRespuestaHumana(BaseModel):
    # Sin `min_length` a propósito: un `Field(min_length=1)` rechaza
    # `{"texto": ""}` en la validación de Pydantic, antes de que el cuerpo
    # del endpoint corra — y ese 422 sale con el formato genérico de FastAPI
    # (una lista de errores), no con `{code, message}` como el resto de esta
    # sección. La validación de contenido (vacío tras `strip()`, demasiado
    # largo) la hace `responder_atencion` a mano, así que un texto vacío da
    # el mismo 422 estructurado que uno demasiado largo.
    texto: str


def _error_estructurado(status_code: int, codigo: str, mensaje: str) -> HTTPException:
    """Único formato de error propio del flujo de atención humana (iniciar,
    tomar, resolver, reactivar-cuando-hay-atención, responder):
    `detail={"code": ..., "message": ...}`. Nunca un `detail` de string
    suelto para estas ocho acciones — eso queda para el resto del CRM,
    fuera del alcance de esta unificación."""
    return HTTPException(
        status_code=status_code,
        detail={"code": codigo, "message": mensaje},
    )


def _resolver_o_409(db: Session, abierta, usuario_id: int):
    """Resuelve con las reglas de `atencion.resolver` y traduce cada motivo
    de rechazo a un 409 estructurado. Lo usan "Resolver" y "Reactivar bot",
    que con una atención abierta son la misma acción."""
    try:
        return atencion.resolver(db, abierta, usuario_id)
    except atencion.AtencionSinTomar:
        raise _error_estructurado(409, CODIGO_ATENCION_SIN_TOMAR, ERROR_ATENCION_SIN_TOMAR)
    except atencion.AtencionAjena:
        raise _error_estructurado(409, CODIGO_ATENCION_AJENA, ERROR_ATENCION_AJENA)
    except atencion.AtencionNoDisponible:
        raise _error_estructurado(409, CODIGO_ATENCION_NO_DISPONIBLE, ERROR_ATENCION_NO_DISPONIBLE)


def _atencion_abierta_o_404(db: Session, conversacion_id: int):
    abierta = atencion.atencion_abierta(db, conversacion_id)
    if abierta is None:
        raise _error_estructurado(404, CODIGO_SIN_ATENCION_ABIERTA, ERROR_SIN_ATENCION_ABIERTA)
    return abierta


def _respuesta_de_atencion(db: Session, conversacion, de_la_conversacion) -> dict:
    return {
        "atencion": servicio.atencion_a_dict(db, de_la_conversacion),
        "conversacion": servicio.detalle_conversacion(db, conversacion),
    }


@router.post("/api/conversaciones/{conversacion_id}/atencion", status_code=201)
def iniciar_atencion(
    conversacion_id: int,
    request: Request,
    sesion=Depends(requiere_sesion),
    db: Session = Depends(obtener_db),
):
    """Iniciar una atención desde la vista general. El bot se pausa en el
    acto. La atención queda pendiente y sin responsable: asignársela es
    "tomar", un paso aparte."""
    verificar_csrf(request, sesion)
    conversacion = _conversacion_o_404(db, conversacion_id)

    try:
        nueva = atencion.iniciar_desde_crm(db, conversacion, sesion.usuario.id)
    except atencion.AtencionYaAbierta:
        raise _error_estructurado(409, CODIGO_ATENCION_YA_ABIERTA, ERROR_ATENCION_YA_ABIERTA)

    logger.info(
        "CRM: %s inició la atención %s en la conversación %s",
        sesion.usuario.usuario, nueva.id, conversacion.id,
    )
    return _respuesta_de_atencion(db, conversacion, nueva)


@router.post("/api/conversaciones/{conversacion_id}/atencion/tomar")
def tomar_atencion(
    conversacion_id: int,
    request: Request,
    sesion=Depends(requiere_sesion),
    db: Session = Depends(obtener_db),
):
    """Quedarse con la atención. Si otra persona la tomó primero, 409."""
    verificar_csrf(request, sesion)
    conversacion = _conversacion_o_404(db, conversacion_id)
    abierta = _atencion_abierta_o_404(db, conversacion.id)

    try:
        tomada = atencion.tomar(db, abierta, sesion.usuario.id)
    except atencion.AtencionNoDisponible:
        raise _error_estructurado(409, CODIGO_ATENCION_NO_DISPONIBLE, ERROR_ATENCION_NO_DISPONIBLE)

    logger.info(
        "CRM: %s tomó la atención %s de la conversación %s",
        sesion.usuario.usuario, tomada.id, conversacion.id,
    )
    return _respuesta_de_atencion(db, conversacion, tomada)


@router.post("/api/conversaciones/{conversacion_id}/atencion/resolver")
def resolver_atencion(
    conversacion_id: int,
    request: Request,
    sesion=Depends(requiere_sesion),
    db: Session = Depends(obtener_db),
):
    """Cerrar la atención y devolverle la conversación al bot. No manda nada
    por WhatsApp: el bot contesta recién el próximo mensaje entrante.

    Solo la resuelve su responsable. Una pendiente hay que tomarla primero, y
    una ajena devuelve 409. Una que ya se cerró devuelve 404: la conversación
    ya no tiene atención abierta. Queda registrado quién resolvió
    (`resuelta_por`)."""
    verificar_csrf(request, sesion)
    conversacion = _conversacion_o_404(db, conversacion_id)
    abierta = _atencion_abierta_o_404(db, conversacion.id)

    resuelta = _resolver_o_409(db, abierta, sesion.usuario.id)

    logger.info(
        "CRM: %s resolvió la atención %s de la conversación %s",
        sesion.usuario.usuario, resuelta.id, conversacion.id,
    )
    db.refresh(conversacion)
    return {**_respuesta_de_atencion(db, conversacion, resuelta), "mensaje": MENSAJE_BOT_REACTIVADO}


@router.post("/api/conversaciones/{conversacion_id}/atencion/responder", status_code=201)
def responder_atencion(
    conversacion_id: int,
    datos: DatosRespuestaHumana,
    request: Request,
    sesion=Depends(requiere_sesion),
    db: Session = Depends(obtener_db),
):
    """Envía texto libre como la persona responsable de la atención.

    El autor sale siempre de la sesión. La autorización se vuelve a validar
    con bloqueo en `app.envio` inmediatamente antes de llamar a Meta.
    """
    verificar_csrf(request, sesion)
    conversacion = _conversacion_o_404(db, conversacion_id)

    texto = datos.texto.strip()
    if not texto or len(texto) > WHATSAPP_MAX_CARACTERES:
        raise _error_estructurado(
            422,
            CODIGO_TEXTO_INVALIDO,
            f"El texto debe tener entre 1 y {WHATSAPP_MAX_CARACTERES} caracteres.",
        )

    try:
        procesado = enviar_respuesta_humana(
            db,
            conversacion_id=conversacion.id,
            texto=texto,
            autor_crm_id=sesion.usuario.id,
        )
    except atencion.AtencionSinTomar:
        raise _error_estructurado(409, CODIGO_ATENCION_SIN_TOMAR, ERROR_ATENCION_SIN_TOMAR)
    except atencion.AtencionAjena:
        raise _error_estructurado(409, CODIGO_ATENCION_AJENA, ERROR_ATENCION_AJENA)
    except atencion.AtencionNoDisponible:
        raise _error_estructurado(409, CODIGO_ATENCION_NO_DISPONIBLE, ERROR_ATENCION_NO_DISPONIBLE)

    if procesado.resultado == ResultadoEnvio.BLOQUEADO_PRESUPUESTO:
        raise _error_estructurado(
            409,
            CODIGO_PRESUPUESTO_AGOTADO,
            "El presupuesto mensual no permite enviar este mensaje.",
        )
    if procesado.resultado == ResultadoEnvio.FUERA_DE_VENTANA:
        raise _error_estructurado(
            409,
            CODIGO_FUERA_DE_VENTANA,
            "La ventana de atención de 24 horas está cerrada.",
        )
    if procesado.resultado == ResultadoEnvio.FALLO_META:
        raise _error_estructurado(
            502,
            CODIGO_FALLO_META,
            "Meta no aceptó el mensaje para envío.",
        )
    if procesado.resultado != ResultadoEnvio.EXITOSO or procesado.mensaje is None:
        logger.error(
            "Resultado inesperado enviando respuesta humana en conversación %s: %s",
            conversacion.id, procesado.resultado,
        )
        raise _error_estructurado(502, CODIGO_FALLO_META, "No se pudo aceptar el mensaje para envío.")

    logger.info(
        "CRM: %s respondió en la conversación %s (mensaje=%s)",
        sesion.usuario.usuario, conversacion.id, procesado.mensaje.id,
    )
    db.refresh(conversacion)
    abierta = atencion.atencion_abierta(db, conversacion.id)
    return {
        "status": "aceptado_por_meta",
        "mensaje": servicio.mensaje_a_dict(db, procesado.mensaje),
        "atencion": servicio.atencion_a_dict(db, abierta) if abierta is not None else None,
        "conversacion": servicio.detalle_conversacion(db, conversacion),
    }


@router.get("/api/motivos")
def motivos(sesion=Depends(requiere_sesion)):
    return servicio.motivos_legibles()


@router.get("/api/metricas")
def metricas_del_rango(
    desde: str | None = Query(default=None, description="YYYY-MM-DD"),
    hasta: str | None = Query(default=None, description="YYYY-MM-DD"),
    sesion=Depends(requiere_sesion),
    db: Session = Depends(obtener_db),
):
    """El dashboard de costos y actividad (specs/spec-dashboard-metricas.md).
    Sin `desde`/`hasta`, los últimos 7 días. Nunca devuelve teléfonos ni
    contenido de mensajes — solo conteos, duraciones y tokens agregados."""
    try:
        inicio, fin = metricas.rango_utc(desde, hasta)
    except metricas.RangoInvalido as error:
        raise HTTPException(status_code=400, detail=str(error))
    return metricas.resumen(db, inicio, fin)
