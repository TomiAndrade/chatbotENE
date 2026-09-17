"""Quién puede entrar al panel: la cookie de sesión y la protección CSRF.

El login es **propio**: usuario y contraseña de una cuenta de
`crm_usuarios`, verificada con Argon2id (ver `app/crm/passwords.py` y
`app/crm/usuarios.py`). No hay proveedor externo, ni contraseña compartida en
una variable de entorno, ni registro público: las cuentas se crean desde la
consola con `scripts/crm_usuario.py`.

Lo que vive en este módulo es lo que pasa **después** de que el login dijo
quién es la persona:

- la cookie `crm_sesion`, que lleva **solo un identificador aleatorio** — el
  resto (de quién es, hasta cuándo vale, su token CSRF) está en Postgres, en
  `crm_sesiones` (ver `app/crm/sesiones.py`);
- el token anti-CSRF para las acciones que escriben (reactivar y salir).

Nada de esto se cruza con el webhook: `POST /webhook` se sigue validando con
la firma de Meta, no comparte cookie ni secreto con el panel, y la cookie de
sesión sale acotada a `path=/crm`, así que ni siquiera viaja en los requests
de Meta.
"""

import hmac
import logging

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import config
from app.crm import sesiones
from app.crm.servicio import obtener_db
from app.validacion_config import crm_sobre_https

logger = logging.getLogger("bot")

COOKIE_SESION = "crm_sesion"
HEADER_CSRF = "X-CRM-CSRF"

RUTA_COOKIE = "/crm"

# Las respuestas del panel traen conversaciones de gente: no se cachean en el
# navegador ni en ningún proxy del camino.
CABECERAS_SIN_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
}


def crm_habilitado() -> bool:
    """El CRM existe solo si alguien lo prendió a propósito
    (`CRM_HABILITADO=true`). Apagado, no se registra ninguna ruta del panel y
    `/crm` es un 404 como cualquier URL inexistente — el servidor está
    publicado en internet para que le pegue Meta.

    El resto de la configuración no se chequea acá: `validar_config()` ya
    corta el arranque si falta algo (ver spec-validacion-config-arranque.md).
    """
    return config.crm_habilitado


def cookie_segura() -> bool:
    """`Secure` salvo en el http local que permite la validación de arranque
    (`CRM_BASE_URL=http://localhost...` con `DEBUG=true`), donde el navegador
    descartaría la cookie y no se podría probar nada."""
    return crm_sobre_https(config)


def sin_cache(respuesta: Response) -> None:
    for nombre, valor in CABECERAS_SIN_CACHE.items():
        respuesta.headers[nombre] = valor


def poner_cookie_de_sesion(respuesta: Response, token: str) -> None:
    """La cookie de sesión.

    `SameSite=Lax` (lo que ya tenía): el navegador la manda en las
    navegaciones de arriba por GET —entrar al panel desde un link o un
    favorito— y **no** en un POST que nazca en otro sitio, que es lo que
    importa para CSRF. Todo lo que el panel lee por GET es de solo lectura;
    las dos acciones que escriben son POST y además exigen el token de
    `verificar_csrf`.

    Con `Strict` la cookie tampoco viajaría al abrir el panel desde un link
    compartido, y la persona vería la pantalla de entrar teniendo la sesión
    abierta. Se gana poco y se pierde eso.
    """
    respuesta.set_cookie(
        COOKIE_SESION,
        token,
        max_age=int(sesiones.DURACION_SESION.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=cookie_segura(),
        path=RUTA_COOKIE,
    )


def borrar_cookie_de_sesion(respuesta: Response) -> None:
    respuesta.delete_cookie(COOKIE_SESION, path=RUTA_COOKIE)


def token_de_sesion(request: Request) -> str | None:
    return request.cookies.get(COOKIE_SESION)


def sesion_actual(request: Request, db: Session):
    """La sesión de este request, o None. Es lo que miran las páginas para
    decidir si mostrar el panel o mandar al login."""
    return sesiones.buscar_sesion_valida(db, token_de_sesion(request))


def requiere_sesion(
    request: Request,
    response: Response,
    db: Session = Depends(obtener_db),
):
    """Dependencia de **todos** los endpoints de datos del panel.

    La cookie tiene que corresponder a una sesión que sirva ahora mismo:
    existe, no está revocada, no venció, la cuenta sigue activa y la sesión
    es posterior al último cambio de contraseña de esa cuenta. Las cinco
    condiciones las resuelve `buscar_sesion_valida` (ver
    `app/crm/sesiones.py`), y las cinco se miran **en cada request**, no solo
    al terminar el login: desactivar una cuenta o cambiarle la contraseña
    deja afuera a las sesiones que ya estaban abiertas.

    Un solo código de error, 401: el panel manda a la pantalla de entrar. No
    hay un caso "autenticado pero sin permiso" — quien tiene cuenta activa
    tiene acceso al panel entero (ver spec-crm-conversaciones.md, sección 9:
    no hay roles).
    """
    sin_cache(response)

    sesion = sesion_actual(request, db)
    if sesion is None:
        raise HTTPException(status_code=401, detail="Sesión no válida o vencida")

    return sesion


def verificar_csrf(request: Request, sesion) -> None:
    """Para las acciones que escriben (reactivar y salir).

    La cookie es SameSite=Lax, así que el navegador no la manda en un POST
    que nazca en otro sitio; esto es la segunda barrera. El token
    vive en la base, se lo da el panel a sí mismo por `/crm/api/sesion` y
    tiene que volver en el header — algo que solo puede hacer código servido
    desde este mismo origen (la cookie es HttpOnly y no hay CORS abierto).
    """
    enviado = request.headers.get(HEADER_CSRF, "")
    # Los dos lados se comparan en bytes y no como str: `compare_digest` sobre
    # texto revienta con TypeError si aparece un carácter no ASCII, y el valor
    # de un header lo elige quien manda el request (Starlette lo decodifica
    # como latin-1, así que un byte alto llega como no ASCII). Comparar bytes
    # convierte eso en el 403 que corresponde, no en un 500.
    if not enviado or not hmac.compare_digest(
        enviado.encode("utf-8"), sesion.csrf.encode("utf-8")
    ):
        raise HTTPException(status_code=403, detail="Token CSRF inválido")
