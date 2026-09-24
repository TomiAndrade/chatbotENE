"""Bloqueo duro mensual de gasto de WhatsApp/Meta — etapa 2.1
(specs/spec-tope-duro-meta.md). META_TOPE_DURO_HABILITADO está apagado por
default en toda la suite (no lo fija tests/conftest.py, así que usa el
default real de app/config.py): los tests que lo necesitan prendido lo
piden explícito con `monkeypatch.setattr(config, "meta_tope_duro_habilitado", True)`.

Dos estilos, mismo criterio que tests/test_costo_whatsapp.py:
- Los que ejercitan `enviar_y_guardar` de punta a punta pasan por
  `POST /webhook` real.
- Los que ejercitan `app.costo_meta.reservar_gasto`/`asegurar_fila_mensual`
  directo (concurrencia, mes nuevo) arman las filas en la base.
"""

import json
import threading
from datetime import datetime, timezone
from decimal import Decimal

from app import envio as envio_mod
from app.config import config
from app.costo_meta import asegurar_fila_mensual, liberar_reserva, mes_actual, reservar_gasto
from app.db import SessionLocal
from app.models import Conversacion, EnvioWhatsapp, Mensaje, PresupuestoMetaMensual, RolMensaje
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import firmar_meta, payload_meta_texto

SECRETO = "test-app-secret"


def _post_mensaje(client, wa_message_id: str, texto: str, telefono: str = TELEFONO_DE_PRUEBA):
    payload = payload_meta_texto(wa_message_id, telefono, texto)
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)}
    )


def _fila_mensual(db, mes: str) -> PresupuestoMetaMensual | None:
    return db.query(PresupuestoMetaMensual).filter_by(mes=mes).first()


def _conversacion(db, identificador: str) -> Conversacion:
    conversacion = Conversacion(canal="whatsapp", identificador_externo=identificador)
    db.add(conversacion)
    db.commit()
    db.refresh(conversacion)
    return conversacion


# --- 1. Flag apagado conserva el comportamiento actual ---------------------


def test_flag_apagado_conserva_el_comportamiento_de_la_etapa_1(client, meta_enviados, db):
    assert config.meta_tope_duro_habilitado is False  # default de la suite, no tocado acá

    _post_mensaje(client, "wamid.flag-apagado", "hola")

    assert len(meta_enviados) == 1  # Meta se llamó, sin ningún bloqueo de por medio
    conversacion = db.query(Conversacion).filter_by(identificador_externo=TELEFONO_DE_PRUEBA).one()
    assert db.query(Mensaje).filter_by(conversacion_id=conversacion.id, rol=RolMensaje.BOT).count() == 1
    assert db.query(EnvioWhatsapp).count() == 1
    # Con el flag apagado, reservar_gasto/asegurar_fila_mensual nunca se
    # llaman: no debería existir ninguna fila de control.
    assert db.query(PresupuestoMetaMensual).count() == 0


# --- 2. Reserva dentro del presupuesto permite enviar -----------------------


def test_reserva_dentro_del_presupuesto_permite_enviar(client, meta_enviados, db, monkeypatch):
    monkeypatch.setattr(config, "meta_tope_duro_habilitado", True)
    monkeypatch.setattr(config, "meta_tarifa_service_ars", Decimal("50"))
    monkeypatch.setattr(config, "meta_presupuesto_mensual_ars", Decimal("1000"))

    _post_mensaje(client, "wamid.dentro-presupuesto", "hola")

    assert len(meta_enviados) == 1
    mes = mes_actual(datetime.now(timezone.utc))
    fila = _fila_mensual(db, mes)
    assert fila is not None
    assert fila.costo_comprometido_ars == Decimal("50")


# --- 3. Mensaje que superaría el presupuesto NO llama a Meta ---------------


def test_mensaje_que_supera_presupuesto_no_llama_a_meta(client, meta_enviados, db, monkeypatch):
    monkeypatch.setattr(config, "meta_tope_duro_habilitado", True)
    monkeypatch.setattr(config, "meta_tarifa_service_ars", Decimal("50"))
    monkeypatch.setattr(config, "meta_presupuesto_mensual_ars", Decimal("10"))  # menos que la tarifa

    _post_mensaje(client, "wamid.supera-presupuesto", "hola")

    assert meta_enviados == [], "no se tiene que haber llamado a Meta"
    conversacion = db.query(Conversacion).filter_by(identificador_externo=TELEFONO_DE_PRUEBA).one()
    assert db.query(Mensaje).filter_by(conversacion_id=conversacion.id, rol=RolMensaje.BOT).count() == 0
    assert db.query(EnvioWhatsapp).count() == 0

    mes = mes_actual(datetime.now(timezone.utc))
    fila = _fila_mensual(db, mes)
    assert fila is not None, "asegurar_fila_mensual la crea igual, aunque la reserva no entre"
    assert fila.costo_comprometido_ars == Decimal("0"), "la reserva fallida no debe sumar nada"


# --- 4. Reservas concurrentes no superan el presupuesto agregado -----------


def test_reservas_concurrentes_no_superan_el_presupuesto_agregado(monkeypatch):
    """20 hilos, cada uno con su propia sesión, reservando al mismo tiempo
    (sincronizados con un Barrier) contra un presupuesto que solo alcanza
    para 5. Si el UPDATE condicional no fuera atómico, más de 5 podrían
    ganar la carrera y el comprometido final superaría el presupuesto."""
    monkeypatch.setattr(config, "meta_tarifa_service_ars", Decimal("100"))
    monkeypatch.setattr(config, "meta_presupuesto_mensual_ars", Decimal("500"))  # alcanza para 5 de 20

    mes = "2027-03"  # mes de prueba, no el actual, para no interferir con otros tests
    n_hilos = 20
    barrera = threading.Barrier(n_hilos)
    resultados = []
    lock_resultados = threading.Lock()

    def intentar_reservar():
        db = SessionLocal()
        try:
            barrera.wait()  # todos arrancan lo más cerca posible del mismo instante
            gano = reservar_gasto(db, mes)
            with lock_resultados:
                resultados.append(gano)
        finally:
            db.close()

    hilos = [threading.Thread(target=intentar_reservar) for _ in range(n_hilos)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    assert sum(resultados) == 5, "tienen que ganar exactamente 5 (500 / 100)"
    assert resultados.count(False) == 15

    db = SessionLocal()
    try:
        fila = _fila_mensual(db, mes)
        assert fila.costo_comprometido_ars == Decimal("500")
        assert fila.costo_comprometido_ars <= fila.presupuesto_ars
    finally:
        db.close()


# --- 5. Meta rechaza/falla -> la reserva se libera --------------------------


def test_meta_rechaza_el_envio_libera_la_reserva(client, meta_enviados, db, monkeypatch):
    monkeypatch.setattr(config, "meta_tope_duro_habilitado", True)
    monkeypatch.setattr(config, "meta_tarifa_service_ars", Decimal("50"))
    monkeypatch.setattr(config, "meta_presupuesto_mensual_ars", Decimal("1000"))

    def meta_caido(telefono: str, texto: str) -> dict:
        raise RuntimeError("Meta no responde")

    monkeypatch.setattr(envio_mod.meta_client, "enviar_mensaje_texto", meta_caido)

    _post_mensaje(client, "wamid.meta-caido-tope", "hola")

    mes = mes_actual(datetime.now(timezone.utc))
    fila = _fila_mensual(db, mes)
    assert fila is not None
    assert fila.costo_comprometido_ars == Decimal("0"), "se reservó y después se liberó: vuelve a 0"
    assert db.query(EnvioWhatsapp).count() == 0


# --- 6. Meta acepta -> la reserva permanece ---------------------------------


def test_meta_acepta_la_reserva_permanece(client, meta_enviados, db, monkeypatch):
    monkeypatch.setattr(config, "meta_tope_duro_habilitado", True)
    monkeypatch.setattr(config, "meta_tarifa_service_ars", Decimal("50"))
    monkeypatch.setattr(config, "meta_presupuesto_mensual_ars", Decimal("1000"))

    _post_mensaje(client, "wamid.meta-acepta-tope", "hola")

    mes = mes_actual(datetime.now(timezone.utc))
    fila = _fila_mensual(db, mes)
    assert fila.costo_comprometido_ars == Decimal("50")

    conversacion = db.query(Conversacion).filter_by(identificador_externo=TELEFONO_DE_PRUEBA).one()
    envio = db.query(EnvioWhatsapp).filter_by(conversacion_id=conversacion.id).one()
    assert envio.costo_estimado_ars == fila.costo_comprometido_ars, (
        "en el camino feliz, lo comprometido coincide con el detalle contabilizado"
    )


# --- 7. Meta acepta pero falla la persistencia -> la reserva permanece -----


def test_meta_acepta_pero_falla_persistencia_local_la_reserva_permanece(client, db, monkeypatch, caplog):
    """Simula "Meta aceptó, pero guardar Mensaje/EnvioWhatsapp falló" sin
    tocar código de producción: se pre-crea un Mensaje con el mismo
    wa_message_id que "va a devolver" Meta, así el INSERT del mensaje del
    bot choca con la UNIQUE de Mensaje.wa_message_id (IntegrityError) en
    cuanto enviar_y_guardar intenta guardarlo — después de que la reserva
    ya se comiteó en su propia transacción, antes de llamar a Meta."""
    monkeypatch.setattr(config, "meta_tope_duro_habilitado", True)
    monkeypatch.setattr(config, "meta_tarifa_service_ars", Decimal("50"))
    monkeypatch.setattr(config, "meta_presupuesto_mensual_ars", Decimal("1000"))

    otra_conversacion = _conversacion(db, "colision-wa-message-id")
    db.add(
        Mensaje(
            conversacion_id=otra_conversacion.id, rol=RolMensaje.BOT, contenido="ya existe",
            wa_message_id="wamid.colision-persistencia",
        )
    )
    db.commit()

    def meta_acepta_id_colisionado(telefono: str, texto: str) -> dict:
        return {"messages": [{"id": "wamid.colision-persistencia"}]}

    monkeypatch.setattr(envio_mod.meta_client, "enviar_mensaje_texto", meta_acepta_id_colisionado)

    with caplog.at_level("ERROR"):
        _post_mensaje(client, "wamid.entrante-colision-persistencia", "hola")

    mes = mes_actual(datetime.now(timezone.utc))
    fila = _fila_mensual(db, mes)
    assert fila is not None
    assert fila.costo_comprometido_ars == Decimal("50"), (
        "Meta aceptó: la reserva NO se libera aunque el guardado local haya fallado"
    )

    conversacion = db.query(Conversacion).filter_by(identificador_externo=TELEFONO_DE_PRUEBA).one()
    # El mensaje del bot de ESTE intento no llegó a guardarse (chocó con el
    # ya existente); la conversación de este intento queda sin mensajes de bot.
    assert db.query(Mensaje).filter_by(conversacion_id=conversacion.id, rol=RolMensaje.BOT).count() == 0
    # El EnvioWhatsapp con ese wa_message_id es el de la fila precreada, no uno nuevo.
    assert db.query(EnvioWhatsapp).count() == 0


# --- 8. Creación concurrente de la fila mensual no genera dos filas --------


def test_creacion_concurrente_de_fila_mensual_no_genera_dos_filas():
    mes = "2027-04"  # mes de prueba, no el actual
    n_hilos = 15
    barrera = threading.Barrier(n_hilos)

    def crear():
        db = SessionLocal()
        try:
            barrera.wait()
            asegurar_fila_mensual(db, mes)
        finally:
            db.close()

    hilos = [threading.Thread(target=crear) for _ in range(n_hilos)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    db = SessionLocal()
    try:
        assert db.query(PresupuestoMetaMensual).filter_by(mes=mes).count() == 1
    finally:
        db.close()


def test_colision_en_asegurar_fila_mensual_no_pierde_pendiente_ajeno_de_la_sesion():
    """Revisión post-implementación: `asegurar_fila_mensual` aísla el intento
    de INSERT en un SAVEPOINT (`db.begin_nested()`) precisamente para que un
    `IntegrityError` por la carrera del `UNIQUE(mes)` no haga `rollback()` de
    otro cambio ya pendiente y sin comitear en la misma sesión.

    Dos sesiones reales, cada una con su propio cambio ajeno pendiente
    (una `Conversacion` sin comitear) antes de intentar crear la misma fila
    de mes, sincronizadas con un Barrier para que las dos pasen el `SELECT`
    inicial antes de que cualquiera comitee — la ventana real de la carrera,
    no una simulación. Exactamente una gana; a la otra `asegurar_fila_mensual`
    le atrapa el `IntegrityError` y usa el SAVEPOINT para descartar solo el
    INSERT que chocó. Sin el SAVEPOINT (un `db.rollback()` a secas), la
    sesión perdedora hubiera perdido también su `Conversacion` pendiente.
    """
    mes = "2027-06"
    barrera = threading.Barrier(2)
    resultados = {}
    lock_resultados = threading.Lock()

    def intentar(nombre: str):
        db = SessionLocal()
        try:
            pendiente = Conversacion(canal="whatsapp", identificador_externo=f"pendiente-{nombre}")
            db.add(pendiente)  # trabajo ajeno, sin comitear todavía

            barrera.wait()
            asegurar_fila_mensual(db, mes)

            # Si esta sesión ganó la carrera, asegurar_fila_mensual ya
            # comiteó todo (incluida `pendiente`) en su rama de éxito. Si
            # perdió, `pendiente` sigue pendiente después del SAVEPOINT
            # revertido — este commit es el que prueba que seguía viva.
            db.commit()

            with lock_resultados:
                resultados[nombre] = (
                    db.query(Conversacion).filter_by(identificador_externo=f"pendiente-{nombre}").count() == 1
                )
        finally:
            db.close()

    hilo_a = threading.Thread(target=intentar, args=("a",))
    hilo_b = threading.Thread(target=intentar, args=("b",))
    hilo_a.start()
    hilo_b.start()
    hilo_a.join()
    hilo_b.join()

    assert resultados == {"a": True, "b": True}, (
        "el pendiente de cada sesión tiene que sobrevivir sin importar cuál de las dos ganó la carrera"
    )

    db = SessionLocal()
    try:
        assert db.query(PresupuestoMetaMensual).filter_by(mes=mes).count() == 1
    finally:
        db.close()


# --- 9. Mes nuevo usa una fila independiente --------------------------------


def test_mes_nuevo_usa_una_fila_independiente(db, monkeypatch):
    monkeypatch.setattr(config, "meta_tarifa_service_ars", Decimal("50"))
    monkeypatch.setattr(config, "meta_presupuesto_mensual_ars", Decimal("1000"))

    mes_anterior = "2027-01"
    mes_siguiente = "2027-02"

    assert reservar_gasto(db, mes_anterior) is True
    assert reservar_gasto(db, mes_siguiente) is True

    fila_anterior = _fila_mensual(db, mes_anterior)
    fila_siguiente = _fila_mensual(db, mes_siguiente)
    assert fila_anterior.costo_comprometido_ars == Decimal("50")
    assert fila_siguiente.costo_comprometido_ars == Decimal("50")

    # Liberar en el mes siguiente no toca la reserva del mes anterior.
    liberar_reserva(db, mes_siguiente)
    db.refresh(fila_anterior)
    db.refresh(fila_siguiente)
    assert fila_anterior.costo_comprometido_ars == Decimal("50"), "no debe verse afectada por el otro mes"
    assert fila_siguiente.costo_comprometido_ars == Decimal("0")
