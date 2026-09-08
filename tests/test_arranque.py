"""al_iniciar() (app/main.py): orden de arranque real de la app.

Ningún test de esta suite pasa por al_iniciar() — el fixture `client` en
conftest.py usa TestClient(app) sin `with` a propósito, así que no dispara el
startup real (ver ese fixture para el porqué). Sin este archivo, nada
detecta si alguien saca la llamada a validar_config() de al_iniciar(), o si
la deja después de crear_engine()/init_db(): la suite entera seguiría en
verde.
"""

import pytest

import app.main as main_mod
from app.validacion_config import ConfigInvalida


def test_al_iniciar_valida_antes_de_crear_el_engine_y_las_tablas(monkeypatch):
    orden = []

    monkeypatch.setattr(main_mod, "validar_config", lambda cfg: orden.append("validar_config"))
    monkeypatch.setattr(main_mod, "resumen_config", lambda cfg: "resumen de prueba")
    monkeypatch.setattr(main_mod, "crear_engine", lambda: orden.append("crear_engine"))
    monkeypatch.setattr(main_mod, "init_db", lambda: orden.append("init_db"))

    main_mod.al_iniciar()

    assert orden == ["validar_config", "crear_engine", "init_db"]


def test_al_iniciar_no_crea_el_engine_ni_las_tablas_si_la_config_es_invalida(monkeypatch):
    """Con una config inválida, ni crear_engine() ni init_db() se llegan a
    llamar — es lo que garantiza que no se cree un archivo SQLite (o se
    toque cualquier base) antes de que el proceso muera."""
    llamados = []

    def validar_que_falla(cfg):
        raise ConfigInvalida("config rota, a propósito, para este test")

    monkeypatch.setattr(main_mod, "validar_config", validar_que_falla)
    monkeypatch.setattr(main_mod, "crear_engine", lambda: llamados.append("crear_engine"))
    monkeypatch.setattr(main_mod, "init_db", lambda: llamados.append("init_db"))

    with pytest.raises(ConfigInvalida):
        main_mod.al_iniciar()

    assert llamados == []
