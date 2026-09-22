"""La sesión del panel: la cookie, el vencimiento, la revocación, el CSRF y
que el panel y el webhook sigan siendo dos puertas separadas
(app/crm/auth.py, app/crm/sesiones.py).

El login en sí —usuario, contraseña, límite de intentos— tiene sus propios
tests en `tests/test_crm_login.py`, y las cuentas en
`tests/test_crm_usuarios.py`.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import config
from app.crm.auth import COOKIE_SESION, HEADER_CSRF, crm_habilitado
from app.crm.modelos import SesionCrm
from tests.conftest import TELEFONO_DE_PRUEBA, hacer_login, login_crm
from tests.helpers import firmar_meta, payload_meta_texto

SECRETO_META = "test-app-secret"

# Todo lo que devuelve datos de conversaciones, más los dos que escriben.
# Ninguno puede contestar sin sesión.
ENDPOINTS_PRIVADOS = [
    ("GET", "/crm/api/sesion"),
    ("GET", "/crm/api/conversaciones"),
    ("GET", "/crm/api/conversaciones?filtro=pausadas"),
    ("GET", "/crm/api/conversaciones/1/mensajes"),
    ("GET", "/crm/api/conversaciones/1/exportar"),
    ("GET", "/crm/api/motivos"),
    ("GET", "/crm/api/metricas"),
    ("POST", "/crm/api/conversaciones/1/reactivar"),
    ("POST", "/crm/api/logout"),
]


@pytest.mark.parametrize("metodo,url", ENDPOINTS_PRIVADOS)
def test_sin_sesion_ningun_endpoint_privado_responde(cliente_crm, metodo, url):
    respuesta = cliente_crm.request(metodo, url)

    assert respuesta.status_code == 401


@pytest.mark.parametrize("metodo,url", ENDPOINTS_PRIVADOS)
def test_con_una_cuenta_creada_pero_sin_loguearse_tampoco(cliente_crm, usuario_crm, metodo, url):
    """Que exista una cuenta no abre nada: hace falta la sesión."""
    respuesta = cliente_crm.request(metodo, url)

    assert respuesta.status_code == 401


def test_sin_sesion_el_panel_manda_al_login(cliente_crm):
    respuesta = cliente_crm.get("/crm", follow_redirects=False)

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/crm/login"


def test_sin_sesion_el_dashboard_de_metricas_manda_al_login(cliente_crm):
    respuesta = cliente_crm.get("/crm/metricas", follow_redirects=False)

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/crm/login"


def test_con_sesion_el_dashboard_de_metricas_se_sirve(cliente_crm, usuario_crm):
    login_crm(cliente_crm, usuario_crm)

    respuesta = cliente_crm.get("/crm/metricas")

    assert respuesta.status_code == 200
    assert "métricas" in respuesta.text.lower()


def test_la_pantalla_de_entrar_se_puede_abrir_sin_sesion(cliente_crm):
    """Es la única página abierta: si pidiera sesión no habría forma de
    loguearse."""
    respuesta = cliente_crm.get("/crm/login")

    assert respuesta.status_code == 200
    assert "Centro de conversaciones" in respuesta.text


def test_la_pantalla_de_entrar_pide_usuario_y_contraseña(cliente_crm):
    """El login es propio: hay un formulario con los dos campos y ningún
    rastro del botón que mandaba a Auth0."""
    html = cliente_crm.get("/crm/login").text

    assert 'type="password"' in html
    assert 'autocomplete="username"' in html
    assert "auth0" not in html.lower()


def test_la_pantalla_de_acceso_denegado_de_auth0_ya_no_existe(cliente_crm):
    """Era la pantalla de "tu cuenta no está en la lista de `sub`". Sin Auth0
    no hay ese caso: o tenés cuenta activa, o no entrás."""
    assert cliente_crm.get("/crm/acceso-denegado", follow_redirects=False).status_code == 404


@pytest.mark.parametrize(
    "ruta",
    ["/crm/auth/login", "/crm/auth/callback"],
)
def test_las_rutas_del_flujo_de_auth0_ya_no_existen(cliente_crm, ruta):
    """El ida y vuelta con el proveedor se borró entero: no quedó nada
    escuchando "por las dudas"."""
    assert cliente_crm.get(ruta, follow_redirects=False).status_code == 404


def test_una_cookie_del_sistema_anterior_no_da_acceso(cliente_crm, usuario_crm, db):
    """Una fila de `crm_sesiones` de la época de Auth0 guardaba el SHA-256
    del token pelado. Ahora el hash lleva adelante un separador de dominio
    (`sesiones.SEPARADOR_DE_DOMINIO`), así que el hash viejo no coincide con
    ninguna búsqueda: **esa cookie no encuentra nada**, aunque la fila
    siguiera en la base.

    (El arranque además no deja pasar una base con el esquema viejo, ver
    `test_crm_esquema_viejo.py`. Esto es la otra barrera.)
    """
    import hashlib

    from app.crm import sesiones

    token_viejo = "token-de-una-sesion-de-la-epoca-de-auth0"
    db.add(
        SesionCrm(
            # El hash tal como lo calculaba el código anterior: sin separador.
            token_hash=hashlib.sha256(token_viejo.encode("utf-8")).hexdigest(),
            usuario_id=usuario_crm.id,
            csrf="csrf-viejo",
            creada_en=datetime.now(timezone.utc),
            expira_en=datetime.now(timezone.utc) + timedelta(hours=12),
        )
    )
    db.commit()

    cliente_crm.cookies.set(COOKIE_SESION, token_viejo)

    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401
    assert sesiones.buscar_sesion_valida(db, token_viejo) is None


def test_una_cookie_inventada_no_sirve(cliente_crm):
    cliente_crm.cookies.set(COOKIE_SESION, "un-identificador-cualquiera")

    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401


def test_cambiarle_una_letra_a_la_cookie_la_invalida(cliente_crm, usuario_crm):
    """No hay nada que "editar" en la cookie: es un índice contra la base, y
    el hash de un valor alterado no coincide con ninguna fila."""
    login_crm(cliente_crm, usuario_crm)
    valor = cliente_crm.cookies[COOKIE_SESION]

    cliente_crm.cookies.set(COOKIE_SESION, valor[:-1] + ("A" if valor[-1] != "A" else "B"))

    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401


def test_la_cookie_de_sesion_es_httponly_lax_y_acotada_al_panel(cliente_crm, usuario_crm):
    """HttpOnly para que no la lea ningún JavaScript; SameSite=Lax para que
    el navegador no la mande en un POST que nazca en otro sitio; `path=/crm`
    para que no viaje en los requests del webhook."""
    respuesta = hacer_login(cliente_crm)

    cookie = respuesta.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" in cookie  # CRM_BASE_URL es https en la suite, como en producción
    assert "path=/crm" in cookie


def test_una_sesion_vencida_no_sirve(cliente_crm, usuario_crm, db):
    """El vencimiento es absoluto: no se estira con el uso."""
    login_crm(cliente_crm, usuario_crm)
    assert cliente_crm.get("/crm/api/conversaciones").status_code == 200

    sesion = db.query(SesionCrm).one()
    sesion.expira_en = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401


def test_una_sesion_revocada_no_sirve(cliente_crm, usuario_crm, db):
    """Revocar en la base corta el acceso aunque la cookie siga en el
    navegador — es lo que permite sacar a alguien sin esperar el
    vencimiento."""
    login_crm(cliente_crm, usuario_crm)

    sesion = db.query(SesionCrm).one()
    sesion.revocada_en = datetime.now(timezone.utc)
    db.commit()

    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401


def test_logout_cierra_la_sesion(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)

    respuesta = cliente_crm.post("/crm/api/logout", headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 200
    assert db.query(SesionCrm).one().revocada_en is not None
    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401


def test_logout_invalida_tambien_una_copia_vieja_de_la_cookie(cliente_crm, usuario_crm, db):
    """Alguien que se hubiera guardado el valor de la cookie antes del logout
    no puede volver a entrar con ella: la sesión está revocada en la base."""
    csrf = login_crm(cliente_crm, usuario_crm)
    copia = cliente_crm.cookies[COOKIE_SESION]

    cliente_crm.post("/crm/api/logout", headers={HEADER_CSRF: csrf})

    cliente_crm.cookies.set(COOKIE_SESION, copia)
    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401


def test_logout_sin_token_csrf_no_cierra_nada(cliente_crm, usuario_crm, db):
    """Si el logout se pudiera disparar sin token, cualquier página ajena
    podría desloguear a quien esté mirando el panel."""
    login_crm(cliente_crm, usuario_crm)

    respuesta = cliente_crm.post("/crm/api/logout")

    assert respuesta.status_code == 403
    assert db.query(SesionCrm).one().revocada_en is None
    assert cliente_crm.get("/crm/api/conversaciones").status_code == 200


def test_logout_con_un_token_csrf_de_mentira_no_cierra_nada(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)

    respuesta = cliente_crm.post("/crm/api/logout", headers={HEADER_CSRF: "no-es-el-token"})

    assert respuesta.status_code == 403
    assert db.query(SesionCrm).one().revocada_en is None


def test_un_token_csrf_con_caracteres_raros_da_403_y_no_un_500(cliente_crm, usuario_crm, db):
    """El valor del header lo elige quien manda el request, y se manda como
    bytes: Starlette lo decodifica como latin-1, así que un byte alto llega
    como carácter no ASCII. `hmac.compare_digest` sobre texto reventaría ahí
    con TypeError (un 500); tiene que ser un 403 como cualquier token que no
    coincide.

    El header va en bytes a propósito: httpx no deja mandar un str no ASCII,
    pero un cliente cualquiera sí puede poner esos bytes en la línea del
    header."""
    login_crm(cliente_crm, usuario_crm)

    respuesta = cliente_crm.post(
        "/crm/api/logout",
        headers={HEADER_CSRF.encode("ascii"): "tokeñ-rarísimo".encode("latin-1")},
    )

    assert respuesta.status_code == 403
    assert db.query(SesionCrm).one().revocada_en is None


def test_un_login_nuevo_reemplaza_la_sesion_anterior(cliente_crm, usuario_crm, db):
    """Sesión nueva después de autenticar, y la anterior revocada: nadie
    puede plantar una cookie y esperar a que alguien se loguee con ella
    (fijación de sesión)."""
    login_crm(cliente_crm, usuario_crm)
    primera = cliente_crm.cookies[COOKIE_SESION]

    hacer_login(cliente_crm)
    segunda = cliente_crm.cookies[COOKIE_SESION]

    assert primera != segunda
    sesiones = db.query(SesionCrm).order_by(SesionCrm.id).all()
    assert len(sesiones) == 2
    assert sesiones[0].revocada_en is not None
    assert sesiones[1].revocada_en is None


def test_las_respuestas_del_panel_no_se_cachean(cliente_crm, usuario_crm):
    """Traen conversaciones de gente: no pueden quedar en el caché del
    navegador ni en un proxy del camino."""
    login_crm(cliente_crm, usuario_crm)

    respuesta = cliente_crm.get("/crm/api/conversaciones")
    pagina = cliente_crm.get("/crm")

    assert "no-store" in respuesta.headers["cache-control"]
    assert "no-store" in pagina.headers["cache-control"]


def test_el_webhook_no_depende_de_la_sesion_del_crm(client, meta_enviados):
    """El panel y el webhook son dos puertas distintas: cerrar el panel no
    puede dejar al bot sin recibir mensajes, y la firma de Meta sigue siendo
    lo único que valida el webhook."""
    payload = payload_meta_texto("wamid.crm.1", TELEFONO_DE_PRUEBA, "hola sin sesión de CRM")
    cuerpo = json.dumps(payload).encode("utf-8")

    respuesta = client.post(
        "/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO_META)}
    )

    assert respuesta.status_code == 200
    assert len(meta_enviados) == 1


def test_la_sesion_del_crm_no_sirve_para_entrar_al_webhook(cliente_crm, usuario_crm):
    """Y al revés: estar logueado en el panel no saltea la firma de Meta."""
    login_crm(cliente_crm, usuario_crm)
    payload = payload_meta_texto("wamid.crm.2", TELEFONO_DE_PRUEBA, "mensaje sin firmar")

    respuesta = cliente_crm.post("/webhook", json=payload)

    assert respuesta.status_code == 401


def test_el_webhook_firmado_sigue_andando_con_el_panel_prendido(cliente_crm, meta_enviados):
    """El webhook no comparte nada con el panel: con el CRM montado en la
    misma app, un mensaje firmado entra y se responde igual que siempre."""
    payload = payload_meta_texto("wamid.crm.3", TELEFONO_DE_PRUEBA, "hola con el panel prendido")
    cuerpo = json.dumps(payload).encode("utf-8")

    respuesta = cliente_crm.post(
        "/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO_META)}
    )

    assert respuesta.status_code == 200
    assert len(meta_enviados) == 1


def test_health_sigue_abierto(cliente_crm):
    """El chequeo de salud no puede depender del login del panel: si
    dependiera, no se podría monitorear el bot."""
    assert cliente_crm.get("/health").status_code == 200


def test_los_estaticos_solo_sirven_los_archivos_de_la_lista(cliente_crm):
    """No es un directorio montado: solo existen los archivos declarados en
    ARCHIVOS_ESTATICOS, así que no hay forma de pedir otra cosa."""
    assert cliente_crm.get("/crm/estaticos/crm.css").status_code == 200
    assert cliente_crm.get("/crm/estaticos/logo-ene.png").status_code == 200
    assert cliente_crm.get("/crm/estaticos/panel.html").status_code == 404
    assert cliente_crm.get("/crm/estaticos/no-existe.js").status_code == 404


def test_el_panel_se_apaga_con_la_config(monkeypatch):
    """Con CRM_HABILITADO=false no se registra ninguna ruta del panel (ver
    app/main.py). Acá se prueba el interruptor; que el router no se monte se
    ve en el arranque, que corre una sola vez por proceso."""
    monkeypatch.setattr(config, "crm_habilitado", False)

    assert crm_habilitado() is False


def test_un_login_limpia_las_sesiones_vencidas_sin_tocar_las_vivas(cliente_crm, usuario_crm, db):
    """La limpieza corre al intentar un login (si nadie entra al panel,
    tampoco se acumulan filas nuevas) y está acotada: no es un DELETE suelto
    sobre toda la tabla."""
    from app.crm.sesiones import crear_sesion

    login_crm(cliente_crm, usuario_crm)  # una sesión viva, la de este cliente

    _, vieja = crear_sesion(db, usuario_crm)
    vieja.expira_en = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    # Por el hash y no por el id: SQLite reusa el rowid de una fila borrada,
    # así que la sesión que crea el login siguiente puede quedar con el mismo
    # id y el test pasaría (o fallaría) por la razón equivocada.
    hash_viejo = vieja.token_hash

    hacer_login(cliente_crm)

    assert db.query(SesionCrm).filter_by(token_hash=hash_viejo).first() is None
    # La sesión recién creada por ese login sigue sirviendo.
    assert cliente_crm.get("/crm/api/conversaciones").status_code == 200
