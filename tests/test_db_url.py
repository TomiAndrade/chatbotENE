"""`normalizar_url`: el arreglo de la URL de conexión antes de crear el engine.

Es lo único de la migración a Postgres que se puede probar sin una base
Postgres de verdad — el resto (fechas con tzinfo, el IntegrityError que aborta
la transacción entera) hay que validarlo a mano contra la base real, ver el
README.
"""

from app.db import normalizar_url


def test_el_prefijo_que_entrega_render_se_convierte():
    """Render entrega `postgres://`, un esquema que SQLAlchemy ya no acepta:
    tal cual falla al arrancar con `Can't load plugin:
    sqlalchemy.dialects:postgres`."""
    assert (
        normalizar_url("postgres://usuario:pass@host:5432/bot")
        == "postgresql://usuario:pass@host:5432/bot"
    )


def test_postgresql_a_secas_no_se_toca():
    """Sin prefijo viejo no hay nada que normalizar: SQLAlchemy resuelve el
    driver (psycopg2, vía requirements.txt) solo, sin un `+driver` explícito."""
    url = "postgresql://usuario:pass@host:5432/bot"
    assert normalizar_url(url) == url


def test_un_driver_ya_explicito_se_respeta():
    """Si alguien eligió un driver a propósito, no se lo pisamos."""
    url = "postgresql+psycopg2://usuario:pass@host:5432/bot"
    assert normalizar_url(url) == url


def test_sqlite_no_se_toca():
    """El default de desarrollo tiene que seguir funcionando igual."""
    assert normalizar_url("sqlite:///./bot.db") == "sqlite:///./bot.db"


def test_solo_se_reemplaza_el_prefijo():
    """La contraseña puede contener la cadena `postgres://` literal; el
    reemplazo es con `count=1` justamente para no tocarla."""
    normalizada = normalizar_url("postgres://usuario:postgres://x@host:5432/bot")
    assert normalizada == "postgresql://usuario:postgres://x@host:5432/bot"
