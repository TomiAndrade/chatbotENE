"""Control preventivo de gasto de WhatsApp/Meta — etapa 1
(specs/spec-costo-whatsapp-meta.md): persistencia del `wa_message_id` real,
condición exacta de "contabilizado", consumo mensual y los campos nuevos del
CRM. Es una ESTIMACIÓN preventiva, no facturación exacta.

Dos estilos de test, mismo criterio que el resto de la suite:
- Los que ejercitan `enviar_y_guardar` de punta a punta pasan por
  `POST /webhook` real (mismo patrón que tests/test_llamada_ia.py).
- Los que ejercitan la agregación de `app.costo_meta.consumo_mensual` arman
  conversaciones, mensajes y envíos directo en la base (mismo patrón que
  tests/test_metricas.py) porque lo que se prueba es la cuenta, no cómo
  llegó cada fila.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import main as main_mod
from app.config import config
from app.costo_meta import CATEGORIA_SERVICE, consumo_mensual
from app.db import SessionLocal
from app.models import Conversacion, EnvioWhatsapp, Mensaje, RolMensaje
from tests.conftest import TELEFONO_DE_PRUEBA, login_crm
from tests.helpers import firmar_meta, payload_meta_texto

SECRETO = "test-app-secret"


def _post_mensaje(client, wa_message_id: str, texto: str, telefono: str = TELEFONO_DE_PRUEBA):
    payload = payload_meta_texto(wa_message_id, telefono, texto)
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)}
    )


def _conversacion(db, identificador: str) -> Conversacion:
    conversacion = db.query(Conversacion).filter_by(identificador_externo=identificador).first()
    if conversacion is None:
        conversacion = Conversacion(canal="whatsapp", identificador_externo=identificador)
        db.add(conversacion)
        db.commit()
        db.refresh(conversacion)
    return conversacion


def _mensaje_bot(db, conversacion: Conversacion, wa_message_id: str, creado_en: datetime) -> Mensaje:
    mensaje = Mensaje(
        conversacion_id=conversacion.id,
        rol=RolMensaje.BOT,
        contenido="respuesta de prueba",
        wa_message_id=wa_message_id,
        creado_en=creado_en,
    )
    db.add(mensaje)
    db.commit()
    db.refresh(mensaje)
    return mensaje


def _envio(db, conversacion: Conversacion, mensaje: Mensaje, creado_en: datetime, **kwargs) -> EnvioWhatsapp:
    envio = EnvioWhatsapp(
        conversacion_id=conversacion.id,
        mensaje_id=mensaje.id,
        wa_message_id=mensaje.wa_message_id,
        categoria=CATEGORIA_SERVICE,
        tarifa_ars=kwargs.get("tarifa_ars", config.meta_tarifa_service_ars),
        costo_estimado_ars=kwargs.get("costo_estimado_ars", config.meta_tarifa_service_ars),
        creado_en=creado_en,
    )
    db.add(envio)
    db.commit()
    db.refresh(envio)
    return envio


# --- 1. Meta acepta: se persiste el id, se contabiliza una vez, con tarifa/costo ---


def test_meta_acepta_persiste_id_y_contabiliza_una_vez_con_tarifa_y_costo(client, meta_enviados):
    _post_mensaje(client, "wamid.entrante-ok", "hola, quiero info")

    db = SessionLocal()
    try:
        conversacion = db.query(Conversacion).filter_by(identificador_externo=TELEFONO_DE_PRUEBA).one()
        mensaje_bot = db.query(Mensaje).filter_by(conversacion_id=conversacion.id, rol=RolMensaje.BOT).one()

        assert mensaje_bot.wa_message_id is not None
        assert mensaje_bot.wa_message_id != ""

        envios = db.query(EnvioWhatsapp).filter_by(mensaje_id=mensaje_bot.id).all()
        assert len(envios) == 1
        envio = envios[0]
        assert envio.wa_message_id == mensaje_bot.wa_message_id
        assert envio.conversacion_id == conversacion.id
        assert envio.categoria == CATEGORIA_SERVICE
        assert envio.tarifa_ars == config.meta_tarifa_service_ars
        assert envio.costo_estimado_ars == config.meta_tarifa_service_ars
    finally:
        db.close()


# --- 2. Meta responde error: no se contabiliza ---------------------------


def test_meta_rechaza_el_envio_no_contabiliza_costo(client, meta_enviados, monkeypatch):
    def meta_caido(telefono: str, texto: str) -> dict:
        raise RuntimeError("Meta no responde")

    monkeypatch.setattr(main_mod.meta_client, "enviar_mensaje_texto", meta_caido)

    _post_mensaje(client, "wamid.entrante-error", "hola")

    db = SessionLocal()
    try:
        assert db.query(EnvioWhatsapp).count() == 0
        conversacion = db.query(Conversacion).filter_by(identificador_externo=TELEFONO_DE_PRUEBA).one()
        # Tampoco se guardó el mensaje del bot: enviar_y_guardar corta antes,
        # exactamente el mismo comportamiento que tenía antes de esta entrega.
        assert db.query(Mensaje).filter_by(conversacion_id=conversacion.id, rol=RolMensaje.BOT).count() == 0
    finally:
        db.close()


# --- 3. Dos envíos exitosos: el acumulado mensual es la suma correcta -----


def test_dos_envios_exitosos_suman_el_costo_acumulado(db):
    ahora = datetime.now(timezone.utc)
    conversacion = _conversacion(db, "5492990000001")
    mensaje_1 = _mensaje_bot(db, conversacion, "wamid.acum-1", ahora)
    mensaje_2 = _mensaje_bot(db, conversacion, "wamid.acum-2", ahora)
    _envio(db, conversacion, mensaje_1, ahora)
    _envio(db, conversacion, mensaje_2, ahora)

    resultado = consumo_mensual(db, ahora)

    assert resultado["mensajes_contabilizados"] == 2
    assert resultado["costo_estimado_ars"] == config.meta_tarifa_service_ars * 2


# --- 4. Cambio de mes: solo se contabiliza el mes solicitado/actual -------


def test_solo_cuenta_los_envios_del_mes_actual(db):
    ahora = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    mes_pasado = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    mes_que_viene = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)  # 2026-11-01 00:00 ARG

    conversacion = _conversacion(db, "5492990000002")
    mensaje_actual = _mensaje_bot(db, conversacion, "wamid.mes-actual", ahora)
    mensaje_pasado = _mensaje_bot(db, conversacion, "wamid.mes-pasado", mes_pasado)
    mensaje_futuro = _mensaje_bot(db, conversacion, "wamid.mes-futuro", mes_que_viene)
    _envio(db, conversacion, mensaje_actual, ahora)
    _envio(db, conversacion, mensaje_pasado, mes_pasado)
    _envio(db, conversacion, mensaje_futuro, mes_que_viene)

    resultado = consumo_mensual(db, ahora)

    assert resultado["mensajes_contabilizados"] == 1
    assert resultado["costo_estimado_ars"] == config.meta_tarifa_service_ars
    assert resultado["mes"] == "2026-10"


def test_no_borra_ni_resetea_filas_de_meses_anteriores(db):
    """Conservar histórico: una fila de un mes que ya pasó sigue en la base,
    aunque `consumo_mensual` de este mes no la cuente."""
    ahora = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    mes_pasado = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    conversacion = _conversacion(db, "5492990000003")
    mensaje_pasado = _mensaje_bot(db, conversacion, "wamid.historico", mes_pasado)
    _envio(db, conversacion, mensaje_pasado, mes_pasado)

    consumo_mensual(db, ahora)

    assert db.query(EnvioWhatsapp).filter_by(wa_message_id="wamid.historico").count() == 1


# --- 5. Porcentaje utilizado: cálculo correcto contra el presupuesto ------


def test_porcentaje_utilizado_correcto_contra_el_presupuesto(db, monkeypatch):
    monkeypatch.setattr(config, "meta_presupuesto_mensual_ars", Decimal("100"))
    monkeypatch.setattr(config, "meta_tarifa_service_ars", Decimal("12.5"))

    ahora = datetime.now(timezone.utc)
    conversacion = _conversacion(db, "5492990000004")
    for indice in range(4):  # 4 * 12.5 = 50, la mitad del presupuesto
        mensaje = _mensaje_bot(db, conversacion, f"wamid.pct-{indice}", ahora)
        _envio(db, conversacion, mensaje, ahora)

    resultado = consumo_mensual(db, ahora)

    assert resultado["costo_estimado_ars"] == Decimal("50.0")
    assert resultado["porcentaje_utilizado"] == 0.5
    assert resultado["saldo_estimado_restante_ars"] == Decimal("50.0")


# --- 6. ARS: sin errores evidentes de precisión monetaria -----------------


def test_sin_error_de_precision_en_ars_con_muchos_envios(db):
    """128 mensajes a la tarifa real (37.6798, con 4 decimales) es el mismo
    ejemplo conceptual del encargo: si esto diera un resultado con ruido de
    punto flotante (37.6798 * 128 en float no da un decimal exacto), Decimal
    lo evita."""
    ahora = datetime.now(timezone.utc)
    conversacion = _conversacion(db, "5492990000005")
    for indice in range(128):
        mensaje = _mensaje_bot(db, conversacion, f"wamid.precision-{indice}", ahora)
        _envio(db, conversacion, mensaje, ahora)

    resultado = consumo_mensual(db, ahora)

    esperado = config.meta_tarifa_service_ars * 128
    assert resultado["costo_estimado_ars"] == esperado
    assert resultado["mensajes_contabilizados"] == 128


# --- 7. wa_message_id duplicado no genera doble consumo -------------------


def test_wa_message_id_duplicado_no_genera_doble_fila_de_costo(db):
    """Dos `Mensaje` nunca pueden compartir `wa_message_id` (esa columna ya
    era `unique=True` antes de esta entrega), así que la única forma de
    ejercitar la constraint nueva de `EnvioWhatsapp.wa_message_id` es
    intentar guardar dos filas de costo con el mismo id de Meta apuntando a
    mensajes internos distintos — exactamente el caso que la constraint
    tiene que frenar."""
    ahora = datetime.now(timezone.utc)
    conversacion = _conversacion(db, "5492990000006")
    mensaje_1 = _mensaje_bot(db, conversacion, "wamid.msg-1", ahora)
    mensaje_2 = _mensaje_bot(db, conversacion, "wamid.msg-2", ahora)

    db.add(
        EnvioWhatsapp(
            conversacion_id=conversacion.id, mensaje_id=mensaje_1.id, wa_message_id="wamid.duplicado",
            categoria=CATEGORIA_SERVICE, tarifa_ars=config.meta_tarifa_service_ars,
            costo_estimado_ars=config.meta_tarifa_service_ars, creado_en=ahora,
        )
    )
    db.commit()

    db.add(
        EnvioWhatsapp(
            conversacion_id=conversacion.id, mensaje_id=mensaje_2.id, wa_message_id="wamid.duplicado",
            categoria=CATEGORIA_SERVICE, tarifa_ars=config.meta_tarifa_service_ars,
            costo_estimado_ars=config.meta_tarifa_service_ars, creado_en=ahora,
        )
    )
    try:
        db.commit()
        assert False, "se esperaba que la restricción única de wa_message_id rechazara el duplicado"
    except Exception:
        db.rollback()

    assert db.query(EnvioWhatsapp).filter_by(wa_message_id="wamid.duplicado").count() == 1


# --- 8. Métricas del CRM: devuelven los nuevos campos esperados -----------


def test_metricas_del_crm_devuelven_whatsapp_meta(cliente_crm, usuario_crm, db):
    ahora = datetime.now(timezone.utc)
    conversacion = _conversacion(db, "5492990000007")
    mensaje = _mensaje_bot(db, conversacion, "wamid.crm-1", ahora)
    _envio(db, conversacion, mensaje, ahora)

    login_crm(cliente_crm, usuario_crm)
    respuesta = cliente_crm.get("/crm/api/metricas")

    assert respuesta.status_code == 200
    whatsapp_meta = respuesta.json()["whatsapp_meta"]
    assert whatsapp_meta["mensajes_contabilizados"] == 1
    assert whatsapp_meta["presupuesto_mensual_ars"] is not None
    assert whatsapp_meta["porcentaje_utilizado"] is not None
    assert whatsapp_meta["saldo_estimado_restante_ars"] is not None
    assert "mes" in whatsapp_meta
