"""Las cuentas del panel: el alta inicial, el hash de la contraseña, el
cambio de contraseña y la desactivación (app/crm/usuarios.py,
app/crm/passwords.py, scripts/crm_usuario.py).

Lo que se prueba acá **no** es el login (eso está en
`tests/test_crm_login.py`) sino lo de atrás: que la contraseña nunca quede
guardada en claro, que el comando de consola no la reciba como argumento y
que cambiarla o desactivar la cuenta deje afuera a las sesiones abiertas.

Ninguna contraseña de este archivo está escrita a mano: se generan al azar
en cada corrida (ver `_password`), así que no hay ninguna en el repo.
"""

import secrets

import pytest

from app.crm import passwords, sesiones, usuarios
from app.crm.modelos import SesionCrm, UsuarioCrm
from scripts import crm_usuario
from tests.conftest import PASSWORD_DE_PRUEBA, login_crm


def _password() -> str:
    """Una contraseña de prueba distinta en cada llamada, larga como para
    pasar `passwords.validar`."""
    return "prueba-" + secrets.token_urlsafe(16)


# --- Alta ----------------------------------------------------------------


def test_la_primera_cuenta_se_crea_y_queda_activa(db):
    password = _password()

    usuario = usuarios.crear(db, "tomi", password)

    assert usuario.usuario == "tomi"
    assert usuario.activo is True
    assert usuarios.cantidad(db) == 1


def test_el_nombre_de_usuario_se_normaliza(db):
    """"Tomi" y "  tomi " son la misma cuenta: si no, quien entra no entiende
    por qué su contraseña "no anda"."""
    usuarios.crear(db, "  Tomi ", _password())

    assert usuarios.buscar(db, "tomi") is not None
    assert usuarios.buscar(db, "TOMI") is not None


def test_no_se_pueden_crear_dos_cuentas_con_el_mismo_nombre(db):
    usuarios.crear(db, "tomi", _password())

    with pytest.raises(usuarios.UsuarioYaExiste):
        usuarios.crear(db, "Tomi", _password())


def test_una_contraseña_corta_se_rechaza(db):
    """El mínimo es el largo y nada más (ver `passwords.validar`): las reglas
    de "una mayúscula y un símbolo" empujan a contraseñas cortas y
    previsibles."""
    with pytest.raises(passwords.PasswordInvalida):
        usuarios.crear(db, "tomi", "corta")

    assert usuarios.cantidad(db) == 0


def test_no_hay_registro_publico(cliente_crm):
    """No existe ningún endpoint que cree cuentas: las crea el comando de
    consola y nada más."""
    for ruta in ("/crm/api/usuarios", "/crm/api/registro", "/crm/registro"):
        assert cliente_crm.post(ruta, json={"usuario": "x", "password": "y"}).status_code in (404, 405)


# --- El hash -------------------------------------------------------------


def test_la_contraseña_no_queda_en_claro_en_ningun_lado(db):
    """Lo que se guarda es un hash Argon2id, y la contraseña no aparece en
    ninguna columna de la fila."""
    password = _password()

    usuario = usuarios.crear(db, "tomi", password)

    fila = db.query(UsuarioCrm).filter_by(usuario="tomi").one()
    assert fila.hash_password != password
    assert password not in fila.hash_password
    valores = [str(getattr(fila, columna.name)) for columna in UsuarioCrm.__table__.columns]
    assert all(password not in valor for valor in valores)
    assert usuario.hash_password.startswith("$argon2id$")


def test_dos_cuentas_con_la_misma_contraseña_tienen_hashes_distintos(db):
    """Cada hash lleva su propia sal aleatoria: dos hashes iguales delatarían
    que dos personas usan la misma contraseña."""
    password = _password()

    una = usuarios.crear(db, "una", password)
    otra = usuarios.crear(db, "otra", password)

    assert una.hash_password != otra.hash_password


def test_el_hash_verifica_la_contraseña_correcta_y_rechaza_la_incorrecta():
    password = _password()

    hash_guardado = passwords.hashear(password)

    assert passwords.verificar(hash_guardado, password) is True
    assert passwords.verificar(hash_guardado, password + "x") is False


def test_un_hash_ilegible_no_deja_entrar_ni_revienta():
    """Un valor editado a mano en la base tiene que dejar afuera a quien
    intenta entrar, no tirar un 500 (que además diría que esa cuenta
    existe)."""
    assert passwords.verificar("esto-no-es-un-hash", "lo-que-sea") is False


def test_los_parametros_del_hash_son_argon2id_y_no_los_de_una_libreria_cualquiera():
    """Argon2id, no Argon2i ni Argon2d, y con los parámetros que fija el
    módulo: si alguien los afloja, este test lo dice."""
    hash_guardado = passwords.hashear(_password())

    assert hash_guardado.startswith("$argon2id$")
    assert "m=65536,t=3,p=4" in hash_guardado
    assert passwords.hay_que_rehashear(hash_guardado) is False


# --- Cambiar la contraseña -----------------------------------------------


def test_cambiar_la_contraseña_deja_entrar_con_la_nueva_y_no_con_la_vieja(db):
    vieja = _password()
    nueva = _password()
    usuarios.crear(db, "tomi", vieja)

    usuarios.cambiar_password(db, "tomi", nueva)

    assert usuarios.autenticar(db, "tomi", nueva) is not None
    assert usuarios.autenticar(db, "tomi", vieja) is None


def test_cambiar_la_contraseña_cierra_las_sesiones_abiertas(cliente_crm, usuario_crm, db):
    """Si se cambia porque la anterior se filtró, dejar viva la sesión de
    quien la tenía no cambiaría nada."""
    login_crm(cliente_crm, usuario_crm)
    assert cliente_crm.get("/crm/api/conversaciones").status_code == 200

    usuarios.cambiar_password(db, usuario_crm.usuario, _password())

    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401
    assert db.query(SesionCrm).one().revocada_en is not None


def test_una_sesion_anterior_al_cambio_no_sirve_aunque_no_este_revocada(
    cliente_crm, usuario_crm, db
):
    """La segunda barrera: `buscar_sesion_valida` compara la fecha de la
    sesión contra `credenciales_cambiadas_en`. Cubre una sesión que hubiera
    creado otra instancia del servidor entre el cambio y la revocación."""
    login_crm(cliente_crm, usuario_crm)
    usuarios.cambiar_password(db, usuario_crm.usuario, _password())

    # Se deshace la revocación a mano: queda solo la marca de tiempo.
    sesion = db.query(SesionCrm).one()
    sesion.revocada_en = None
    db.commit()

    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401


# --- Desactivar ----------------------------------------------------------


def test_desactivar_deja_la_cuenta_sin_entrar_pero_no_la_borra(db):
    password = _password()
    usuarios.crear(db, "tomi", password)

    usuarios.desactivar(db, "tomi")

    assert usuarios.autenticar(db, "tomi", password) is None
    assert usuarios.buscar(db, "tomi") is not None
    assert usuarios.buscar(db, "tomi").activo is False


def test_desactivar_cierra_las_sesiones_abiertas(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    assert cliente_crm.get("/crm/api/conversaciones").status_code == 200

    usuarios.desactivar(db, usuario_crm.usuario)

    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401
    assert db.query(SesionCrm).one().revocada_en is not None


def test_una_sesion_de_una_cuenta_desactivada_no_sirve_aunque_no_este_revocada(
    cliente_crm, usuario_crm, db
):
    login_crm(cliente_crm, usuario_crm)
    usuarios.desactivar(db, usuario_crm.usuario)

    sesion = db.query(SesionCrm).one()
    sesion.revocada_en = None
    db.commit()

    assert cliente_crm.get("/crm/api/conversaciones").status_code == 401


def test_activar_devuelve_el_acceso_con_la_misma_contraseña(db):
    password = _password()
    usuarios.crear(db, "tomi", password)
    usuarios.desactivar(db, "tomi")

    usuarios.activar(db, "tomi")

    assert usuarios.autenticar(db, "tomi", password) is not None


def test_una_sesion_creada_despues_de_reactivar_la_cuenta_si_sirve(db, usuario_crm):
    """Reactivar no invalida nada hacia adelante: la sesión que se abre
    después vale."""
    usuarios.desactivar(db, usuario_crm.usuario)
    usuarios.activar(db, usuario_crm.usuario)

    token, _ = sesiones.crear_sesion(db, usuario_crm)

    assert sesiones.buscar_sesion_valida(db, token) is not None


# --- El comando de consola -----------------------------------------------


def _password_tipeada(monkeypatch, *valores: str) -> list[str]:
    """Reemplaza `getpass` por una lista de respuestas y devuelve lo que se
    fue preguntando, para poder afirmar que el prompt no muestra nada."""
    preguntas: list[str] = []
    respuestas = list(valores)

    def falso(prompt: str = "") -> str:
        preguntas.append(prompt)
        return respuestas.pop(0)

    monkeypatch.setattr(crm_usuario.getpass, "getpass", falso)
    return preguntas


def test_el_comando_crea_la_primera_cuenta_pidiendo_la_contraseña_sin_mostrarla(
    monkeypatch, db, capsys
):
    """El alta inicial completa: el comando pide la contraseña dos veces con
    `getpass` (que no la muestra mientras se tipea) y crea la cuenta."""
    password = _password()
    _password_tipeada(monkeypatch, password, password)
    monkeypatch.setattr(crm_usuario, "_preparar_base", lambda: None)

    crm_usuario.main(["crear", "tomi"])

    assert usuarios.autenticar(db, "tomi", password) is not None
    salida = capsys.readouterr().out
    assert "creada" in salida
    assert "primera cuenta" in salida
    # La contraseña no se imprime en ningún momento.
    assert password not in salida


def test_el_comando_no_acepta_la_contraseña_como_argumento(monkeypatch, db):
    """Un argumento queda en el historial de la consola y en la lista de
    procesos. El comando toma solo el nombre de usuario."""
    monkeypatch.setattr(crm_usuario, "_preparar_base", lambda: None)

    with pytest.raises(SystemExit) as error:
        crm_usuario.main(["crear", "tomi", "una-contraseña-cualquiera"])

    assert "Uso" in str(error.value)
    assert usuarios.cantidad(db) == 0


def test_el_comando_pide_la_contraseña_dos_veces_y_corta_si_no_coinciden(monkeypatch, db, capsys):
    _password_tipeada(monkeypatch, _password(), _password(), _password(), _password(), _password(), _password())
    monkeypatch.setattr(crm_usuario, "_preparar_base", lambda: None)

    with pytest.raises(SystemExit):
        crm_usuario.main(["crear", "tomi"])

    assert usuarios.cantidad(db) == 0
    assert "no coinciden" in capsys.readouterr().out


def test_el_comando_rechaza_una_contraseña_corta_sin_mostrarla(monkeypatch, db, capsys):
    corta = "corta"
    _password_tipeada(monkeypatch, corta, corta, corta, corta, corta, corta)
    monkeypatch.setattr(crm_usuario, "_preparar_base", lambda: None)

    with pytest.raises(SystemExit):
        crm_usuario.main(["crear", "tomi"])

    salida = capsys.readouterr().out
    assert str(passwords.LARGO_MINIMO) in salida
    assert usuarios.cantidad(db) == 0


def test_el_comando_cambia_la_contraseña_y_lo_dice(monkeypatch, db, usuario_crm, capsys):
    nueva = _password()
    _password_tipeada(monkeypatch, nueva, nueva)
    monkeypatch.setattr(crm_usuario, "_preparar_base", lambda: None)

    crm_usuario.main(["cambiar-password", usuario_crm.usuario])

    # El comando escribe en su propia sesión de SQLAlchemy, como cuando corre
    # de verdad. La de este test ya tiene la fila cargada y no la relee sola:
    # sin esto se estaría afirmando sobre el estado viejo.
    db.expire_all()
    assert usuarios.autenticar(db, usuario_crm.usuario, nueva) is not None
    assert usuarios.autenticar(db, usuario_crm.usuario, PASSWORD_DE_PRUEBA) is None
    salida = capsys.readouterr().out
    assert "sesiones abiertas se cerraron" in salida
    assert nueva not in salida


def test_el_comando_desactiva_y_activa_una_cuenta(monkeypatch, db, usuario_crm):
    monkeypatch.setattr(crm_usuario, "_preparar_base", lambda: None)

    crm_usuario.main(["desactivar", usuario_crm.usuario])
    db.expire_all()  # el comando escribió en su propia sesión (ver arriba)
    assert usuarios.buscar(db, usuario_crm.usuario).activo is False

    crm_usuario.main(["activar", usuario_crm.usuario])
    db.expire_all()
    assert usuarios.buscar(db, usuario_crm.usuario).activo is True


def test_el_comando_sobre_una_cuenta_que_no_existe_no_hace_nada(monkeypatch, db):
    monkeypatch.setattr(crm_usuario, "_preparar_base", lambda: None)

    with pytest.raises(SystemExit) as error:
        crm_usuario.main(["desactivar", "nadie"])

    assert "No existe" in str(error.value)


def test_listar_no_muestra_ningun_hash(monkeypatch, db, usuario_crm, capsys):
    """El hash no hace falta para nada y es material para un ataque de
    diccionario offline si la pantalla queda a la vista."""
    monkeypatch.setattr(crm_usuario, "_preparar_base", lambda: None)

    crm_usuario.main(["listar"])

    salida = capsys.readouterr().out
    assert usuario_crm.usuario in salida
    assert "activa" in salida
    assert "$argon2" not in salida
    assert usuario_crm.hash_password not in salida
