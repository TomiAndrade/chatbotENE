"""El login del panel: contraseña correcta e incorrecta, límite de intentos
y que nada de todo eso deje averiguar qué cuentas existen
(app/crm/rutas.py, app/crm/intentos.py).

Las cuentas y el hash están en `tests/test_crm_usuarios.py`; la sesión que
queda después de entrar, en `tests/test_crm_acceso.py`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.crm import intentos, usuarios
from app.crm.auth import COOKIE_SESION
from app.crm.modelos import IntentoLoginCrm, SesionCrm
from app.crm.rutas import ERROR_CREDENCIALES, ERROR_DEMASIADOS_INTENTOS
from tests.conftest import PASSWORD_DE_PRUEBA, USUARIO_DE_PRUEBA, hacer_login


def _fallar_login(cliente, veces: int, usuario: str = USUARIO_DE_PRUEBA):
    """`veces` intentos con la contraseña equivocada. Devuelve la última
    respuesta."""
    respuesta = None
    for numero in range(veces):
        respuesta = hacer_login(cliente, usuario, f"esta-no-es-la-contraseña-{numero}")
    return respuesta


# --- Entrar --------------------------------------------------------------


def test_con_la_contraseña_correcta_se_entra(cliente_crm, usuario_crm, db):
    respuesta = hacer_login(cliente_crm)

    assert respuesta.status_code == 200
    assert respuesta.json()["usuario"] == USUARIO_DE_PRUEBA
    assert COOKIE_SESION in cliente_crm.cookies
    assert db.query(SesionCrm).count() == 1
    assert cliente_crm.get("/crm/api/conversaciones").status_code == 200


def test_el_nombre_de_usuario_no_distingue_mayusculas(cliente_crm, usuario_crm):
    """Se normaliza igual que al crear la cuenta (`usuarios.normalizar`)."""
    assert hacer_login(cliente_crm, "  EQUIPO-ENE ").status_code == 200


def test_con_la_contraseña_incorrecta_no_se_entra(cliente_crm, usuario_crm, db):
    respuesta = hacer_login(cliente_crm, USUARIO_DE_PRUEBA, "no-es-la-contraseña")

    assert respuesta.status_code == 401
    assert respuesta.json()["detail"] == ERROR_CREDENCIALES
    assert COOKIE_SESION not in cliente_crm.cookies
    assert db.query(SesionCrm).count() == 0
    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401


def test_la_respuesta_del_login_no_se_cachea(cliente_crm, usuario_crm):
    """Trae el nombre de quien entró y viene con el Set-Cookie de la
    sesión."""
    respuesta = hacer_login(cliente_crm)

    assert "no-store" in respuesta.headers["cache-control"]


def test_el_login_no_acepta_un_formulario(cliente_crm, usuario_crm):
    """Es JSON a propósito: un formulario de otro sitio puede hacer un POST
    cruzado sin JavaScript, pero no puede mandar Content-Type JSON sin
    preflight (ver el docstring de DatosDeLogin)."""
    respuesta = cliente_crm.post(
        "/crm/api/login",
        data={"usuario": USUARIO_DE_PRUEBA, "password": PASSWORD_DE_PRUEBA},
    )

    assert respuesta.status_code == 422
    assert COOKIE_SESION not in cliente_crm.cookies


def test_el_login_sin_campos_no_entra(cliente_crm, usuario_crm):
    assert cliente_crm.post("/crm/api/login", json={}).status_code == 422
    assert cliente_crm.post("/crm/api/login", json={"usuario": "", "password": ""}).status_code == 422


def test_no_existe_ninguna_contraseña_en_la_configuracion():
    """El login viejo (y su reemplazo fácil) era una contraseña compartida en
    una variable de entorno. No quedó ninguna: las credenciales viven en la
    base."""
    import dataclasses

    from app.config import Config

    nombres = [campo.name for campo in dataclasses.fields(Config)]
    assert [nombre for nombre in nombres if "password" in nombre] == []
    assert [nombre for nombre in nombres if nombre.startswith("crm_") and "usuario" in nombre] == []


# --- Sin enumeración de cuentas ------------------------------------------


def test_una_cuenta_que_no_existe_responde_igual_que_una_contraseña_mal(cliente_crm, usuario_crm):
    """El mismo código y el mismo texto: si dijeran cosas distintas, el login
    serviría para averiguar qué cuentas hay."""
    inexistente = hacer_login(cliente_crm, "no-existe-esta-cuenta", "lo-que-sea-largo")
    mal = hacer_login(cliente_crm, USUARIO_DE_PRUEBA, "no-es-la-contraseña")

    assert inexistente.status_code == mal.status_code == 401
    assert inexistente.json() == mal.json() == {"detail": ERROR_CREDENCIALES}


def test_una_cuenta_desactivada_responde_igual_que_una_contraseña_mal(cliente_crm, usuario_crm, db):
    """Tampoco se puede averiguar qué cuentas existen pero están apagadas."""
    usuarios.desactivar(db, usuario_crm.usuario)

    desactivada = hacer_login(cliente_crm)
    mal = hacer_login(cliente_crm, USUARIO_DE_PRUEBA, "no-es-la-contraseña")

    assert desactivada.status_code == mal.status_code == 401
    assert desactivada.json() == mal.json() == {"detail": ERROR_CREDENCIALES}


def test_el_bloqueo_tambien_alcanza_a_una_cuenta_que_no_existe(cliente_crm, usuario_crm):
    """El límite se cuenta por el nombre que se intentó, exista o no. Si solo
    contara para las cuentas reales, ver quién se bloquea y quién no diría
    exactamente cuáles existen."""
    _fallar_login(cliente_crm, intentos.MAXIMO_POR_USUARIO, usuario="no-existe-esta-cuenta")

    respuesta = hacer_login(cliente_crm, "no-existe-esta-cuenta", "lo-que-sea-largo")

    assert respuesta.status_code == 429
    assert respuesta.json()["detail"] == ERROR_DEMASIADOS_INTENTOS


def test_bloquear_una_cuenta_no_bloquea_a_las_demas(cliente_crm, usuario_crm, db):
    """El límite por cuenta es por cuenta: alguien martillando un nombre no
    puede dejar afuera al resto del equipo."""
    usuarios.crear(db, "otra-persona", PASSWORD_DE_PRUEBA)
    _fallar_login(cliente_crm, intentos.MAXIMO_POR_USUARIO, usuario="otra-persona")

    assert hacer_login(cliente_crm).status_code == 200


# --- Límite de intentos --------------------------------------------------


def test_despues_de_varios_intentos_fallidos_la_cuenta_queda_bloqueada(cliente_crm, usuario_crm):
    ultimo = _fallar_login(cliente_crm, intentos.MAXIMO_POR_USUARIO)

    assert ultimo.status_code == 401  # el que completa el cupo todavía responde 401
    siguiente = hacer_login(cliente_crm, USUARIO_DE_PRUEBA, "otra-mas")
    assert siguiente.status_code == 429
    assert siguiente.json()["detail"] == ERROR_DEMASIADOS_INTENTOS


def test_bloqueada_no_entra_ni_con_la_contraseña_correcta(cliente_crm, usuario_crm):
    """El chequeo va **antes** de mirar la contraseña: si no, el bloqueo no
    frenaría a quien acierta en el intento número seis."""
    _fallar_login(cliente_crm, intentos.MAXIMO_POR_USUARIO)

    respuesta = hacer_login(cliente_crm)

    assert respuesta.status_code == 429
    assert COOKIE_SESION not in cliente_crm.cookies


def test_debajo_del_limite_la_contraseña_correcta_sigue_entrando(cliente_crm, usuario_crm):
    """Cuatro errores de tipeo no dejan a nadie afuera."""
    _fallar_login(cliente_crm, intentos.MAXIMO_POR_USUARIO - 1)

    assert hacer_login(cliente_crm).status_code == 200


def test_un_login_correcto_borra_los_fallidos_de_esa_cuenta(cliente_crm, usuario_crm, db):
    """La persona demostró que es ella: los intentos de antes no siguen
    contando para el próximo bloqueo."""
    _fallar_login(cliente_crm, intentos.MAXIMO_POR_USUARIO - 1)

    assert hacer_login(cliente_crm).status_code == 200

    fallidos = (
        db.query(IntentoLoginCrm)
        .filter_by(usuario=USUARIO_DE_PRUEBA, exitoso=False)
        .count()
    )
    assert fallidos == 0
    # Y puede volver a equivocarse el cupo entero sin quedar bloqueada.
    assert _fallar_login(cliente_crm, intentos.MAXIMO_POR_USUARIO - 1).status_code == 401
    assert hacer_login(cliente_crm).status_code == 200


def test_el_bloqueo_se_suelta_cuando_los_intentos_salen_de_la_ventana(cliente_crm, usuario_crm, db):
    """La ventana es deslizante: los intentos viejos dejan de contar solos,
    sin que nadie tenga que desbloquear nada a mano."""
    _fallar_login(cliente_crm, intentos.MAXIMO_POR_USUARIO)
    assert hacer_login(cliente_crm).status_code == 429

    viejo = datetime.now(timezone.utc) - intentos.VENTANA - timedelta(minutes=1)
    for intento in db.query(IntentoLoginCrm).all():
        intento.creado_en = viejo
    db.commit()

    assert hacer_login(cliente_crm).status_code == 200


def test_el_limite_se_cuenta_contra_la_base_y_no_en_memoria(cliente_crm, usuario_crm, db):
    """Es lo que hace que funcione con varias instancias del servidor: el
    estado está en la tabla, no en el proceso. Se simulan los intentos de
    "otra instancia" escribiendo las filas directo, sin pasar por este
    cliente, y el bloqueo aparece igual.
    """
    for numero in range(intentos.MAXIMO_POR_USUARIO):
        db.add(
            IntentoLoginCrm(
                usuario=USUARIO_DE_PRUEBA,
                ip="10.0.0.9",
                exitoso=False,
                creado_en=datetime.now(timezone.utc),
            )
        )
    db.commit()

    assert hacer_login(cliente_crm).status_code == 429


def test_el_limite_por_ip_frena_a_quien_prueba_muchas_cuentas(cliente_crm, usuario_crm, db):
    """El de la cuenta no alcanza contra alguien que prueba un nombre
    distinto cada vez: ahí entra el límite por IP, más alto."""
    ahora = datetime.now(timezone.utc)
    for numero in range(intentos.MAXIMO_POR_IP):
        db.add(
            IntentoLoginCrm(
                usuario=f"cuenta-inventada-{numero}",
                ip="testclient",  # la IP que usa TestClient
                exitoso=False,
                creado_en=ahora,
            )
        )
    db.commit()

    # Este nombre no tiene ni un intento fallido propio: lo frena la IP.
    respuesta = hacer_login(cliente_crm)

    assert respuesta.status_code == 429


def test_los_intentos_viejos_se_limpian_solos(cliente_crm, usuario_crm, db):
    """La tabla no crece para siempre: al intentar un login se borran las
    filas anteriores a la ventana."""
    db.add(
        IntentoLoginCrm(
            usuario="alguien-de-hace-rato",
            ip="10.0.0.9",
            exitoso=False,
            creado_en=datetime.now(timezone.utc) - intentos.VENTANA - timedelta(hours=1),
        )
    )
    db.commit()

    hacer_login(cliente_crm)

    assert db.query(IntentoLoginCrm).filter_by(usuario="alguien-de-hace-rato").first() is None


def test_no_se_guarda_la_contraseña_del_intento(cliente_crm, usuario_crm, db):
    """La tabla de intentos registra qué nombre se probó, nunca con qué
    contraseña."""
    hacer_login(cliente_crm, USUARIO_DE_PRUEBA, "una-contraseña-equivocada")

    intento = db.query(IntentoLoginCrm).order_by(IntentoLoginCrm.id.desc()).first()
    columnas = [str(getattr(intento, columna.name)) for columna in IntentoLoginCrm.__table__.columns]
    assert all("una-contraseña-equivocada" not in valor for valor in columnas)


@pytest.mark.parametrize("header", ["1.2.3.4", "9.9.9.9, 1.1.1.1"])
def test_no_se_confia_en_x_forwarded_for(cliente_crm, usuario_crm, db, header):
    """Ese header lo pone quien manda el request: si se usara, alguien se
    inventa una IP distinta en cada intento y el límite por IP no frena
    nada. Lo que se guarda es siempre la IP de la conexión."""
    for numero in range(3):
        cliente_crm.post(
            "/crm/api/login",
            json={"usuario": USUARIO_DE_PRUEBA, "password": f"mal-{numero}"},
            headers={"X-Forwarded-For": header},
        )

    ips = {fila.ip for fila in db.query(IntentoLoginCrm).all()}
    assert ips == {"testclient"}
