"""Armado del historial (ver spec-etapa2.md, tests 1 y 2: trae 20 en orden
cronológico, y con el último mensaje a 8 días el historial llega vacío)."""

from datetime import datetime, timedelta, timezone

from app.config import config
from app.historial import PREFIJO_HUMANO, construir_historial, mapear_mensaje
from app.models import Conversacion, Mensaje, RolMensaje


def _crear_conversacion(db, identificador_externo: str = "5492990000001") -> Conversacion:
    conversacion = Conversacion(canal="whatsapp", identificador_externo=identificador_externo)
    db.add(conversacion)
    db.commit()
    db.refresh(conversacion)
    return conversacion


def _agregar_mensaje(db, conversacion: Conversacion, rol: RolMensaje, contenido: str, creado_en: datetime) -> Mensaje:
    mensaje = Mensaje(conversacion_id=conversacion.id, rol=rol, contenido=contenido, creado_en=creado_en)
    db.add(mensaje)
    db.commit()
    db.refresh(mensaje)
    return mensaje


def test_trae_como_maximo_los_ultimos_n_en_orden_cronologico(db, monkeypatch):
    monkeypatch.setattr(config, "historial_max_mensajes", 20)

    conversacion = _crear_conversacion(db)
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(25):
        _agregar_mensaje(db, conversacion, RolMensaje.USUARIO, f"mensaje {i}", base + timedelta(minutes=i))
    actual = _agregar_mensaje(db, conversacion, RolMensaje.USUARIO, "mensaje actual", base + timedelta(minutes=100))

    historial = construir_historial(db, conversacion, actual)

    assert len(historial) == 20
    assert [m.contenido for m in historial] == [f"mensaje {i}" for i in range(5, 25)]
    assert all(historial[i].creado_en <= historial[i + 1].creado_en for i in range(len(historial) - 1))


def test_mensaje_humano_queda_prefijado_al_mapear(db):
    conversacion = _crear_conversacion(db, identificador_externo="5492990000002")
    mensaje = _agregar_mensaje(
        db, conversacion, RolMensaje.HUMANO, "ya te contacto en un rato", datetime.now(timezone.utc)
    )

    rol_logico, texto = mapear_mensaje(mensaje)

    assert rol_logico == "asistente"
    assert texto == PREFIJO_HUMANO + "ya te contacto en un rato"


def test_mensaje_usuario_y_bot_no_se_prefijan(db):
    conversacion = _crear_conversacion(db, identificador_externo="5492990000003")
    ahora = datetime.now(timezone.utc)
    usuario = _agregar_mensaje(db, conversacion, RolMensaje.USUARIO, "hola", ahora)
    bot = _agregar_mensaje(db, conversacion, RolMensaje.BOT, "¡hola!", ahora)

    assert mapear_mensaje(usuario) == ("usuario", "hola")
    assert mapear_mensaje(bot) == ("asistente", "¡hola!")


def test_corte_por_antiguedad_deja_historial_vacio(db, monkeypatch):
    monkeypatch.setattr(config, "historial_dias_validez", 7)

    conversacion = _crear_conversacion(db, identificador_externo="5492990000004")
    hace_8_dias = datetime.now(timezone.utc) - timedelta(days=8)
    _agregar_mensaje(db, conversacion, RolMensaje.USUARIO, "hola, hace mucho", hace_8_dias)
    actual = _agregar_mensaje(db, conversacion, RolMensaje.USUARIO, "hola de nuevo", datetime.now(timezone.utc))

    historial = construir_historial(db, conversacion, actual)

    assert historial == []


def test_dentro_de_la_validez_no_se_corta(db, monkeypatch):
    monkeypatch.setattr(config, "historial_dias_validez", 7)

    conversacion = _crear_conversacion(db, identificador_externo="5492990000005")
    hace_6_dias = datetime.now(timezone.utc) - timedelta(days=6)
    _agregar_mensaje(db, conversacion, RolMensaje.USUARIO, "hola, hace unos días", hace_6_dias)
    actual = _agregar_mensaje(db, conversacion, RolMensaje.USUARIO, "hola de nuevo", datetime.now(timezone.utc))

    historial = construir_historial(db, conversacion, actual)

    assert len(historial) == 1
    assert historial[0].contenido == "hola, hace unos días"
