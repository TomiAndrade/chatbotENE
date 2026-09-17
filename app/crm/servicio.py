"""Las consultas del CRM y la traducción de los modelos a los diccionarios
que consume el frontend.

Todo con el ORM, como el resto del proyecto: nada de SQL crudo. Las rutas
(`app/crm/rutas.py`) no arman consultas — piden acá y devuelven lo que sale.
"""

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Conversacion, Mensaje, MotivoPausa
from app.pausa import pausa_vigente


def obtener_db():
    """Sesión de SQLAlchemy por request, cerrada al terminar (FastAPI la
    inyecta con Depends y cierra el generador cuando responde).

    Es la misma base y el mismo `SessionLocal` del bot: el CRM no abre una
    conexión propia ni tiene una base aparte. Vive acá y no en `rutas.py`
    porque también la usan las dependencias de `auth.py`, y `auth.py` no
    puede importar `rutas.py` (que importa `auth.py`).
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Cuántos mensajes trae una página del historial. El panel arranca con la
# última página y pide las anteriores con "Cargar mensajes anteriores".
MENSAJES_POR_PAGINA = 50

# Techo para el polling de mensajes nuevos: si por algo hubiera una avalancha,
# la respuesta no crece sin límite.
MAX_MENSAJES_NUEVOS = 200

# Largo de la vista previa del último mensaje en la lista. Se recorta en el
# servidor para no mandar conversaciones enteras a la pantalla de la lista.
LARGO_VISTA_PREVIA = 120


def _a_utc(fecha: datetime | None) -> datetime | None:
    """SQLite devuelve los DateTime(timezone=True) sin tzinfo aunque se hayan
    guardado en UTC (mismo caso que documenta `app/pausa.py`). Postgres los
    devuelve aware. Acá se normaliza a aware-UTC para que lo que sale al
    frontend sea siempre un instante sin ambigüedad."""
    if fecha is None:
        return None
    if fecha.tzinfo is None:
        return fecha.replace(tzinfo=timezone.utc)
    return fecha


def _iso(fecha: datetime | None) -> str | None:
    fecha_utc = _a_utc(fecha)
    return fecha_utc.isoformat() if fecha_utc is not None else None


def _recortar(texto: str) -> str:
    texto = " ".join(texto.split())
    if len(texto) <= LARGO_VISTA_PREVIA:
        return texto
    return texto[:LARGO_VISTA_PREVIA].rstrip() + "…"


def estado_de(conversacion: Conversacion, ahora: datetime) -> dict:
    """El estado de pausa tal como lo ve el bot, no el que sugiere la columna
    `modo_humano` sola: una pausa por intervención manual vencida es una
    conversación activa aunque el flag siga prendido (ver `pausa_vigente`).
    Si el panel mostrara el flag crudo, diría "pausado" de conversaciones que
    el bot ya está respondiendo."""
    pausado = pausa_vigente(conversacion, ahora)
    return {
        "pausado": pausado,
        # El motivo solo tiene sentido si la pausa está vigente. Con la pausa
        # vencida el valor viejo sigue en la columna y mostrarlo sería mentir.
        "motivo_pausa": (
            conversacion.motivo_pausa.value
            if pausado and conversacion.motivo_pausa is not None
            else None
        ),
        "resumen_escalamiento": conversacion.resumen_escalamiento if pausado else None,
        "escalada_en": _iso(conversacion.escalada_en) if pausado else None,
        "pausada_desde": _iso(conversacion.modo_humano_desde) if pausado else None,
    }


def _resumen(conversacion: Conversacion, ultimo_mensaje: Mensaje | None, ahora: datetime) -> dict:
    return {
        "id": conversacion.id,
        "canal": conversacion.canal,
        # Verbatim, como está guardado: el "9" de los números argentinos puede
        # estar o no y tocarlo acá haría que el panel muestre un número que no
        # es el que se usa para responder (spec-meta-cloud-api.md, sección 3).
        "identificador_externo": conversacion.identificador_externo,
        "ultima_actividad": _iso(conversacion.ultimo_mensaje_en),
        "vista_previa": (
            None
            if ultimo_mensaje is None
            else {"rol": ultimo_mensaje.rol.value, "texto": _recortar(ultimo_mensaje.contenido)}
        ),
        **estado_de(conversacion, ahora),
    }


def _ultimos_mensajes(db: Session, conversaciones: list[Conversacion]) -> dict[int, Mensaje]:
    """El último mensaje de cada conversación de la lista, en dos consultas y
    no una por conversación."""
    if not conversaciones:
        return {}

    ids = [conversacion.id for conversacion in conversaciones]
    ids_ultimos = (
        db.query(func.max(Mensaje.id))
        .filter(Mensaje.conversacion_id.in_(ids))
        .group_by(Mensaje.conversacion_id)
        .all()
    )
    mensajes = db.query(Mensaje).filter(Mensaje.id.in_([fila[0] for fila in ids_ultimos])).all()
    return {mensaje.conversacion_id: mensaje for mensaje in mensajes}


def listar_conversaciones(
    db: Session, solo_pausadas: bool, limite: int, desplazamiento: int
) -> dict:
    """Conversaciones ordenadas por actividad más reciente.

    `solo_pausadas` no se puede resolver entero en SQL: la pausa por
    intervención manual expira con el tiempo (`pausa_vigente`), y eso no es
    una columna sino una cuenta contra el reloj. Lo que se hace es traer de
    la base el superconjunto (`modo_humano` prendido, que siempre incluye a
    las vigentes) y descartar en Python las vencidas. Son pocas —una pausa es
    la excepción, no el caso normal— así que traerlas todas y recortar
    después no es un problema de volumen.
    """
    ahora = datetime.now(timezone.utc)
    orden = (Conversacion.ultimo_mensaje_en.desc(), Conversacion.id.desc())

    if solo_pausadas:
        candidatas = db.query(Conversacion).filter(Conversacion.modo_humano.is_(True)).order_by(*orden).all()
        pausadas = [c for c in candidatas if pausa_vigente(c, ahora)]
        total = len(pausadas)
        pagina = pausadas[desplazamiento : desplazamiento + limite]
        hay_mas = total > desplazamiento + limite
    else:
        total = db.query(func.count(Conversacion.id)).scalar() or 0
        pagina = db.query(Conversacion).order_by(*orden).offset(desplazamiento).limit(limite).all()
        hay_mas = total > desplazamiento + len(pagina)

    ultimos = _ultimos_mensajes(db, pagina)
    return {
        "conversaciones": [_resumen(c, ultimos.get(c.id), ahora) for c in pagina],
        "hay_mas": hay_mas,
        "total": total,
        "total_pausadas": contar_pausadas(db, ahora),
    }


def contar_pausadas(db: Session, ahora: datetime | None = None) -> int:
    """Cuántas conversaciones están pausadas *de verdad* ahora mismo (ver
    `listar_conversaciones` sobre por qué no es un COUNT)."""
    ahora = ahora or datetime.now(timezone.utc)
    candidatas = db.query(Conversacion).filter(Conversacion.modo_humano.is_(True)).all()
    return sum(1 for conversacion in candidatas if pausa_vigente(conversacion, ahora))


def buscar_conversacion(db: Session, conversacion_id: int) -> Conversacion | None:
    return db.query(Conversacion).filter(Conversacion.id == conversacion_id).first()


def detalle_conversacion(db: Session, conversacion: Conversacion) -> dict:
    ahora = datetime.now(timezone.utc)
    ultimo = (
        db.query(Mensaje)
        .filter(Mensaje.conversacion_id == conversacion.id)
        .order_by(Mensaje.id.desc())
        .first()
    )
    return _resumen(conversacion, ultimo, ahora)


def _mensaje_a_dict(mensaje: Mensaje) -> dict:
    return {
        "id": mensaje.id,
        "rol": mensaje.rol.value,
        # Texto plano, tal cual está en la base. El frontend lo pinta con
        # textContent, nunca con innerHTML: lo que escribió el usuario es
        # contenido, no HTML.
        "contenido": mensaje.contenido,
        "creado_en": _iso(mensaje.creado_en),
    }


def mensajes_de(
    db: Session,
    conversacion: Conversacion,
    limite: int = MENSAJES_POR_PAGINA,
    antes_de: int | None = None,
    desde: int | None = None,
) -> dict:
    """Una página del historial, siempre en orden cronológico.

    Tres modos, según lo que pida el panel:

    - sin parámetros: los últimos `limite` mensajes (lo que se ve al abrir).
    - `antes_de=<id>`: la página anterior a ese mensaje ("Cargar mensajes
      anteriores").
    - `desde=<id>`: solo lo que llegó después de ese id. Es el del refresco
      periódico: no se vuelve a bajar la conversación entera cada vez.

    El orden de la consulta usa el id y no `creado_en`: dos mensajes
    guardados en el mismo instante (la respuesta del modelo y el aviso que
    sale atrás) tienen que quedar en el orden en que se crearon. Es el mismo
    criterio de `app/historial.py`.
    """
    consulta = db.query(Mensaje).filter(Mensaje.conversacion_id == conversacion.id)

    if desde is not None:
        mensajes = consulta.filter(Mensaje.id > desde).order_by(Mensaje.id.asc()).limit(MAX_MENSAJES_NUEVOS).all()
        return {"mensajes": [_mensaje_a_dict(m) for m in mensajes], "hay_anteriores": False}

    if antes_de is not None:
        consulta = consulta.filter(Mensaje.id < antes_de)

    # limite + 1 para saber si quedan más atrás sin hacer un COUNT aparte.
    recientes = consulta.order_by(Mensaje.id.desc()).limit(limite + 1).all()
    hay_anteriores = len(recientes) > limite
    pagina = list(reversed(recientes[:limite]))
    return {"mensajes": [_mensaje_a_dict(m) for m in pagina], "hay_anteriores": hay_anteriores}


def motivos_legibles() -> dict[str, str]:
    """Cómo se muestra cada `motivo_pausa` en el panel. Acá y no en el
    JavaScript para que un valor nuevo del enum se traduzca en un solo lado."""
    return {
        MotivoPausa.ESCALAMIENTO.value: "El bot derivó la conversación a una persona",
        MotivoPausa.INTERVENCION_MANUAL.value: "Alguien del equipo respondió desde WhatsApp",
    }
