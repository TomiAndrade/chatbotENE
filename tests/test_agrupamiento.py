"""Agrupar y ordenar mensajes consecutivos (entrega 1.2 de roadmap-bot-crm.md,
ver specs/spec-agrupamiento-mensajes.md).

Los tests que necesitan controlar con precisión cuándo se intercala un
mensaje nuevo durante la ventana de espera reemplazan `main.dormir` por una
función sincronizada con `threading.Event` — mismo patrón que ya usa
tests/test_escalamiento.py para las carreras entre una respuesta lenta y un
escalamiento, aplicado acá al punto de sincronización nuevo (la espera de
agrupamiento, no la llamada al modelo). Así no hace falta que el reloj real
avance segundos de verdad: la ventana configurada
(AGRUPAR_VENTANA_SEGUNDOS=0.02, ver tests/conftest.py) solo importa para los
tests que no controlan `dormir` a mano.
"""

import json
import threading
import time as time_mod
from datetime import datetime, timedelta, timezone

from app import main as main_mod
from app.config import config
from app.db import SessionLocal
from app.mensajes import MENSAJE_ADJUNTO_IMAGEN
from app.models import Conversacion, Mensaje, RolMensaje
from app.respuesta import RespuestaGenerada
from tests.conftest import TELEFONO_DE_PRUEBA
from tests.helpers import firmar_meta, mensaje_meta_texto, payload_meta_mensajes, payload_meta_texto

SECRETO = "test-app-secret"

OTRO_TELEFONO = "5492996009999"


def _conversacion_de(identificador_externo: str) -> Conversacion:
    db = SessionLocal()
    try:
        return db.query(Conversacion).filter_by(canal="whatsapp", identificador_externo=identificador_externo).one()
    finally:
        db.close()


def _dormir_controlado():
    """Fake de `main.dormir`: en la primera llamada avisa `pausado_en_espera`
    y se bloquea hasta que el test le dé permiso con `puede_continuar`. Las
    llamadas siguientes no esperan nada de verdad — para cuando el bucle de
    `_esperar_ventana_de_agrupamiento` vuelve a preguntar, ya no hay nada más
    que el test tenga que inyectar."""
    pausado_en_espera = threading.Event()
    puede_continuar = threading.Event()
    contador = {"n": 0}

    def fake(segundos: float) -> None:
        contador["n"] += 1
        if contador["n"] == 1:
            pausado_en_espera.set()
            assert puede_continuar.wait(timeout=5), "el test nunca dejó continuar"

    return fake, pausado_en_espera, puede_continuar


# --- Ráfaga de varios mensajes -> una sola respuesta -------------------------


def test_tres_mensajes_seguidos_generan_una_sola_respuesta_con_todo_el_texto(meta_enviados, monkeypatch):
    llamadas = []

    def modelo(historial, mensaje_nuevo):
        llamadas.append(mensaje_nuevo)
        return RespuestaGenerada(texto="listo, ya te respondo", escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    fake_dormir, pausado_en_espera, puede_continuar = _dormir_controlado()
    monkeypatch.setattr(main_mod, "dormir", fake_dormir)

    hilo = threading.Thread(
        target=main_mod.procesar_mensaje_entrante,
        args=(TELEFONO_DE_PRUEBA, "wamid.rafaga1", "che"),
    )
    hilo.start()
    assert pausado_en_espera.wait(timeout=5), "el primer mensaje nunca llegó a esperar la ventana"

    # Estos dos mensajes llegan mientras el primero todavía está "esperando":
    # no logran tomar la reserva y se suman al mismo lote.
    main_mod.procesar_mensaje_entrante(TELEFONO_DE_PRUEBA, "wamid.rafaga2", "quería preguntar algo")
    main_mod.procesar_mensaje_entrante(TELEFONO_DE_PRUEBA, "wamid.rafaga3", "¿tienen sala para 10 personas?")
    puede_continuar.set()
    hilo.join(timeout=5)
    assert not hilo.is_alive()

    assert llamadas == ["che\nquería preguntar algo\n¿tienen sala para 10 personas?"]
    assert len(meta_enviados) == 1
    assert meta_enviados[0] == (TELEFONO_DE_PRUEBA, "listo, ya te respondo")

    db = SessionLocal()
    guardados = (
        db.query(Mensaje)
        .filter(Mensaje.wa_message_id.in_(["wamid.rafaga1", "wamid.rafaga2", "wamid.rafaga3"]))
        .all()
    )
    conversacion = _conversacion_de(TELEFONO_DE_PRUEBA)
    db.close()
    # Los tres se guardan individuales, con su propio wa_message_id — el
    # agrupamiento no los fusiona en la base, solo en la llamada al modelo.
    assert len(guardados) == 3
    assert conversacion.ultimo_mensaje_agrupado_id == max(m.id for m in guardados)
    assert conversacion.generando_desde is None, "la reserva se soltó al terminar"


def test_dedup_dentro_de_una_rafaga_no_duplica_el_mensaje_en_el_lote(meta_enviados, monkeypatch):
    """Un reintento de Meta del mismo wa_message_id, entregado mientras el
    lote todavía se está esperando, no tiene que aparecer dos veces en el
    texto que ve el modelo."""
    llamadas = []

    def modelo(historial, mensaje_nuevo):
        llamadas.append(mensaje_nuevo)
        return RespuestaGenerada(texto="ok", escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    fake_dormir, pausado_en_espera, puede_continuar = _dormir_controlado()
    monkeypatch.setattr(main_mod, "dormir", fake_dormir)

    hilo = threading.Thread(
        target=main_mod.procesar_mensaje_entrante,
        args=(TELEFONO_DE_PRUEBA, "wamid.dedup1", "hola"),
    )
    hilo.start()
    assert pausado_en_espera.wait(timeout=5)

    main_mod.procesar_mensaje_entrante(TELEFONO_DE_PRUEBA, "wamid.dedup2", "una consulta")
    main_mod.procesar_mensaje_entrante(TELEFONO_DE_PRUEBA, "wamid.dedup2", "una consulta")  # reintento de Meta
    puede_continuar.set()
    hilo.join(timeout=5)

    assert llamadas == ["hola\nuna consulta"]
    db = SessionLocal()
    guardados = db.query(Mensaje).filter_by(wa_message_id="wamid.dedup2").all()
    db.close()
    assert len(guardados) == 1


# --- Contactos distintos, independientes -------------------------------------


def test_contactos_distintos_no_se_bloquean_entre_si(client, meta_enviados, monkeypatch):
    llamadas = []

    def modelo(historial, mensaje_nuevo):
        llamadas.append(mensaje_nuevo)
        return RespuestaGenerada(texto=f"respuesta para {mensaje_nuevo}", escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    def _post(wa_message_id: str, telefono: str, texto: str):
        payload = payload_meta_texto(wa_message_id, telefono, texto)
        cuerpo = json.dumps(payload).encode("utf-8")
        return client.post(
            "/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)}
        )

    _post("wamid.contactoA", TELEFONO_DE_PRUEBA, "hola desde A")
    _post("wamid.contactoB", OTRO_TELEFONO, "hola desde B")

    assert sorted(llamadas) == ["hola desde A", "hola desde B"]
    assert len(meta_enviados) == 2
    enviados_por_telefono = dict(meta_enviados)
    assert enviados_por_telefono[TELEFONO_DE_PRUEBA] == "respuesta para hola desde A"
    assert enviados_por_telefono[OTRO_TELEFONO] == "respuesta para hola desde B"


def test_http_varios_textos_del_mismo_contacto_en_un_payload_una_sola_llamada(client, meta_enviados, monkeypatch):
    """El caso real del webhook: Meta puede mandar varios `messages[]` del
    mismo contacto en un solo POST (varios mensajes seguidos que llegaron
    juntos). `_procesar_cambio` los guarda todos primero y recién después
    dispara una sola tarea de agrupamiento (ver el docstring de
    `procesar_mensaje_entrante` sobre `disparar_agrupamiento`) — tiene que
    verse una sola llamada al modelo con el texto concatenado en orden, no
    una por mensaje."""
    llamadas = []

    def modelo(historial, mensaje_nuevo):
        llamadas.append(mensaje_nuevo)
        return RespuestaGenerada(texto="listo", escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    payload = payload_meta_mensajes(
        [
            mensaje_meta_texto("wamid.http1", TELEFONO_DE_PRUEBA, "che"),
            mensaje_meta_texto("wamid.http2", TELEFONO_DE_PRUEBA, "quería preguntar"),
            mensaje_meta_texto("wamid.http3", TELEFONO_DE_PRUEBA, "¿tienen sala para 10 personas?"),
        ]
    )
    cuerpo = json.dumps(payload).encode("utf-8")
    respuesta = client.post(
        "/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO)}
    )

    assert respuesta.status_code == 200
    assert llamadas == ["che\nquería preguntar\n¿tienen sala para 10 personas?"]
    assert len(meta_enviados) == 1
    assert meta_enviados[0] == (TELEFONO_DE_PRUEBA, "listo")

    db = SessionLocal()
    guardados = (
        db.query(Mensaje)
        .filter(Mensaje.wa_message_id.in_(["wamid.http1", "wamid.http2", "wamid.http3"]))
        .all()
    )
    db.close()
    assert len(guardados) == 3, "los tres se guardan individuales aunque compartan una sola respuesta"


# --- Un mensaje que llega mientras el modelo todavía está generando ---------
# forma su propio lote posterior, no se mezcla con el que ya está en curso.


def test_mensaje_durante_llamada_lenta_al_modelo_forma_un_lote_posterior(meta_enviados, monkeypatch):
    llamadas = []
    modelo_en_curso = threading.Event()
    puede_terminar_modelo = threading.Event()

    def modelo(historial, mensaje_nuevo):
        llamadas.append(mensaje_nuevo)
        if len(llamadas) == 1:
            modelo_en_curso.set()
            assert puede_terminar_modelo.wait(timeout=5), "el test nunca dejó terminar el modelo"
        return RespuestaGenerada(texto=f"respuesta {len(llamadas)}", escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    hilo = threading.Thread(
        target=main_mod.procesar_mensaje_entrante,
        args=(TELEFONO_DE_PRUEBA, "wamid.lento1", "primero"),
    )
    hilo.start()
    assert modelo_en_curso.wait(timeout=5), "el modelo nunca arrancó a generar la primera respuesta"

    # Llega mientras el modelo todavía está generando la respuesta del
    # primer lote: la reserva sigue tomada, así que este mensaje no la
    # consigue, se guarda y queda pendiente para el próximo lote.
    main_mod.procesar_mensaje_entrante(TELEFONO_DE_PRUEBA, "wamid.lento2", "segundo")

    puede_terminar_modelo.set()
    hilo.join(timeout=5)
    assert not hilo.is_alive()

    # Dos llamadas independientes, cada una con su propio texto: no se
    # mezclaron en un solo lote.
    assert llamadas == ["primero", "segundo"]
    assert [texto for _, texto in meta_enviados] == ["respuesta 1", "respuesta 2"]

    conversacion = _conversacion_de(TELEFONO_DE_PRUEBA)
    assert conversacion.generando_desde is None, "la reserva se soltó al terminar el segundo lote"
    assert conversacion.ultimo_mensaje_agrupado_id is not None


# --- Contactos distintos, avanzando de verdad en paralelo -------------------


def test_dos_contactos_avanzan_en_paralelo_sin_bloquearse(meta_enviados, monkeypatch):
    """Distinto del test de arriba (que solo prueba que no se bloquean
    posteando uno después del otro): acá los dos hilos tienen que llegar a
    estar esperando la ventana de agrupamiento *al mismo tiempo*, cada uno
    con su propio `threading.Event`. Si el agrupamiento compartiera una
    reserva global en vez de una por conversación, el segundo contacto no
    llegaría nunca a esperar mientras el primero sigue bloqueado — este test
    lo detectaría como un timeout, no como una aserción de contenido."""
    llamadas = []

    def modelo(historial, mensaje_nuevo):
        llamadas.append(mensaje_nuevo)
        return RespuestaGenerada(texto=f"respuesta para {mensaje_nuevo}", escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    identificador_del_hilo = threading.local()
    eventos = {
        TELEFONO_DE_PRUEBA: (threading.Event(), threading.Event(), {"n": 0}),
        OTRO_TELEFONO: (threading.Event(), threading.Event(), {"n": 0}),
    }

    def fake_dormir(segundos: float) -> None:
        pausado, puede_continuar, contador = eventos[identificador_del_hilo.telefono]
        contador["n"] += 1
        if contador["n"] == 1:
            pausado.set()
            assert puede_continuar.wait(timeout=5), "el test nunca dejó continuar a este contacto"

    monkeypatch.setattr(main_mod, "dormir", fake_dormir)

    def _procesar(telefono: str, wa_message_id: str, texto: str) -> None:
        identificador_del_hilo.telefono = telefono
        main_mod.procesar_mensaje_entrante(telefono, wa_message_id, texto)

    hilo_a = threading.Thread(target=_procesar, args=(TELEFONO_DE_PRUEBA, "wamid.paraleloA", "hola A"))
    hilo_b = threading.Thread(target=_procesar, args=(OTRO_TELEFONO, "wamid.paraleloB", "hola B"))
    hilo_a.start()
    hilo_b.start()

    pausado_a, puede_continuar_a, _ = eventos[TELEFONO_DE_PRUEBA]
    pausado_b, puede_continuar_b, _ = eventos[OTRO_TELEFONO]
    assert pausado_a.wait(timeout=5), "el contacto A nunca llegó a esperar la ventana"
    assert pausado_b.wait(timeout=5), "el contacto B nunca llegó a esperar la ventana"

    puede_continuar_a.set()
    puede_continuar_b.set()
    hilo_a.join(timeout=5)
    hilo_b.join(timeout=5)
    assert not hilo_a.is_alive() and not hilo_b.is_alive()

    assert sorted(llamadas) == ["hola A", "hola B"]
    assert len(meta_enviados) == 2
    enviados_por_telefono = dict(meta_enviados)
    assert enviados_por_telefono[TELEFONO_DE_PRUEBA] == "respuesta para hola A"
    assert enviados_por_telefono[OTRO_TELEFONO] == "respuesta para hola B"


# --- Recuperación automática al arranque -------------------------------------


def test_recuperar_lotes_pendientes_retoma_reservas_vencidas_sin_pisar_las_vigentes(meta_enviados, monkeypatch, db):
    """`app.main._recuperar_lotes_pendientes` (llamada desde `al_iniciar`,
    ver specs/spec-agrupamiento-mensajes.md, "Reinicios y recuperación") es
    lo que evita que un lote pendiente se quede esperando en silencio hasta
    que llegue un mensaje nuevo, si el proceso murió con la reserva tomada.

    Multi-proceso: una conversación con una reserva vigente (otro proceso la
    está usando de verdad, ahora mismo) no se toca — la recuperación no
    asume que este proceso es el único corriendo."""
    monkeypatch.setattr(config, "agrupar_abandono_segundos", 3.0)

    llamadas = []

    def modelo(historial, mensaje_nuevo):
        llamadas.append(mensaje_nuevo)
        return RespuestaGenerada(texto="recuperado", escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    # Conversación con una reserva vieja: el proceso que la tomó murió a
    # mitad de camino, mucho más allá del umbral de abandono.
    conv_vencida = Conversacion(canal="whatsapp", identificador_externo="5492990004444")
    db.add(conv_vencida)
    db.commit()
    db.refresh(conv_vencida)
    hace_rato = datetime.now(timezone.utc) - timedelta(seconds=10)
    db.query(Conversacion).filter_by(id=conv_vencida.id).update(
        {"generando_desde": hace_rato, "generando_token": "token-abandonado"}
    )
    db.add(Mensaje(conversacion_id=conv_vencida.id, rol=RolMensaje.USUARIO, contenido="hola desde A", tipo="text"))
    db.commit()

    # Conversación con una reserva recién tomada: simula otro proceso que la
    # está usando en este mismo instante.
    conv_vigente = Conversacion(canal="whatsapp", identificador_externo="5492990005555")
    db.add(conv_vigente)
    db.commit()
    db.refresh(conv_vigente)
    db.query(Conversacion).filter_by(id=conv_vigente.id).update(
        {"generando_desde": datetime.now(timezone.utc), "generando_token": "token-vigente"}
    )
    db.add(Mensaje(conversacion_id=conv_vigente.id, rol=RolMensaje.USUARIO, contenido="hola desde B", tipo="text"))
    db.commit()

    hilos = main_mod._recuperar_lotes_pendientes()
    for hilo in hilos:
        hilo.join(timeout=5)

    assert llamadas == ["hola desde A"], "solo la reserva vencida se recupera y llama al modelo"
    assert meta_enviados == [(conv_vencida.identificador_externo, "recuperado")]

    db.refresh(conv_vencida)
    assert conv_vencida.generando_desde is None
    assert conv_vencida.ultimo_mensaje_agrupado_id is not None

    db.refresh(conv_vigente)
    assert conv_vigente.generando_token == "token-vigente", "la reserva vigente de otro proceso no se pisó"
    assert conv_vigente.ultimo_mensaje_agrupado_id is None, "el mensaje de B sigue sin procesar, tal como estaba"


# --- Un adjunto en medio de una ráfaga de texto no se agrupa -----------------


def test_un_adjunto_durante_la_espera_se_responde_aparte_y_no_entra_al_lote(meta_enviados, monkeypatch):
    llamadas = []

    def modelo(historial, mensaje_nuevo):
        llamadas.append(mensaje_nuevo)
        return RespuestaGenerada(texto="dale", escalar=False, resumen=None)

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    fake_dormir, pausado_en_espera, puede_continuar = _dormir_controlado()
    monkeypatch.setattr(main_mod, "dormir", fake_dormir)

    hilo = threading.Thread(
        target=main_mod.procesar_mensaje_entrante,
        args=(TELEFONO_DE_PRUEBA, "wamid.mix1", "che"),
    )
    hilo.start()
    assert pausado_en_espera.wait(timeout=5)

    # Un adjunto no soportado, mientras el texto todavía se está agrupando.
    main_mod.procesar_mensaje_entrante(
        TELEFONO_DE_PRUEBA, "wamid.mix-imagen", "[mensaje de tipo 'image' no soportado en esta etapa]", "image",
    )
    main_mod.procesar_mensaje_entrante(TELEFONO_DE_PRUEBA, "wamid.mix2", "¿me pasás el precio?")
    puede_continuar.set()
    hilo.join(timeout=5)

    # El texto agrupado no incluye el adjunto.
    assert llamadas == ["che\n¿me pasás el precio?"]

    textos = [texto for _, texto in meta_enviados]
    assert MENSAJE_ADJUNTO_IMAGEN in textos, "el adjunto se respondió aparte, sin esperar al lote"
    assert "dale" in textos
    assert len(textos) == 2


# --- modo_humano durante la espera corta el lote sin generar nada -----------


def test_no_se_genera_respuesta_si_modo_humano_se_activa_durante_la_espera(meta_enviados, monkeypatch, db):
    """Reemplaza, bajo el mecanismo de agrupamiento, a los tests de carrera
    que tests/test_escalamiento.py tenía antes de esta entrega (ver el
    comentario que dejaron en su lugar): si la conversación pasa a modo
    humano mientras el lote todavía se está esperando, no se llama al modelo
    ni se manda nada — ni para ese lote."""

    def modelo(historial, mensaje_nuevo):
        raise AssertionError("no debería llamarse al modelo si ya hay modo_humano")

    monkeypatch.setattr(main_mod, "generar_respuesta", modelo)

    fake_dormir, pausado_en_espera, puede_continuar = _dormir_controlado()
    monkeypatch.setattr(main_mod, "dormir", fake_dormir)

    hilo = threading.Thread(
        target=main_mod.procesar_mensaje_entrante,
        args=(TELEFONO_DE_PRUEBA, "wamid.pausa-durante-espera", "hola"),
    )
    hilo.start()
    assert pausado_en_espera.wait(timeout=5)

    conversacion = _conversacion_de(TELEFONO_DE_PRUEBA)
    db.query(Conversacion).filter_by(id=conversacion.id).update({"modo_humano": True})
    db.commit()

    puede_continuar.set()
    hilo.join(timeout=5)
    assert not hilo.is_alive()

    assert meta_enviados == []

    db2 = SessionLocal()
    conversacion_final = db2.query(Conversacion).filter_by(id=conversacion.id).one()
    mensaje_pendiente = db2.query(Mensaje).filter_by(wa_message_id="wamid.pausa-durante-espera").one()
    db2.close()
    assert conversacion_final.generando_desde is None, "la reserva se soltó igual"
    assert conversacion_final.ultimo_mensaje_agrupado_id is None, "el lote no se marcó como procesado"
    assert mensaje_pendiente.rol == RolMensaje.USUARIO


# --- Reserva atómica: abandono y concurrencia --------------------------------


def test_una_reserva_vieja_se_puede_retomar_por_abandono(db, monkeypatch):
    monkeypatch.setattr(config, "agrupar_abandono_segundos", 0.01)

    conversacion = Conversacion(canal="whatsapp", identificador_externo="5492990001111")
    db.add(conversacion)
    db.commit()
    db.refresh(conversacion)

    primer_token = main_mod._reclamar_generacion(db, conversacion)
    assert primer_token is not None

    time_mod.sleep(0.05)  # más que agrupar_abandono_segundos

    segundo_token = main_mod._reclamar_generacion(db, conversacion)
    assert segundo_token is not None
    assert segundo_token != primer_token


def test_dos_intentos_de_reserva_simultaneos_solo_uno_gana(db):
    conversacion = Conversacion(canal="whatsapp", identificador_externo="5492990002222")
    db.add(conversacion)
    db.commit()
    db.refresh(conversacion)

    otra_sesion = SessionLocal()
    try:
        token_a = main_mod._reclamar_generacion(db, conversacion)
        token_b = main_mod._reclamar_generacion(otra_sesion, conversacion)

        assert token_a is not None
        assert token_b is None, "una reserva recién tomada no se puede retomar todavía"
    finally:
        otra_sesion.close()


def test_liberar_con_un_token_viejo_no_pisa_la_reserva_nueva(db, monkeypatch):
    """Si mientras un dueño procesaba se lo consideró abandonado y otro
    retomó la reserva, cuando el dueño viejo por fin llega a liberarla su
    UPDATE tiene que afectar cero filas — nunca borrarle la reserva al nuevo
    dueño."""
    monkeypatch.setattr(config, "agrupar_abandono_segundos", 0.01)

    conversacion = Conversacion(canal="whatsapp", identificador_externo="5492990003333")
    db.add(conversacion)
    db.commit()
    db.refresh(conversacion)

    token_viejo = main_mod._reclamar_generacion(db, conversacion)
    assert token_viejo is not None

    time_mod.sleep(0.05)
    token_nuevo = main_mod._reclamar_generacion(db, conversacion)
    assert token_nuevo is not None and token_nuevo != token_viejo

    main_mod._liberar_generacion(db, conversacion, token_viejo)

    db.refresh(conversacion)
    assert conversacion.generando_token == token_nuevo, "la liberación con el token viejo no tocó la reserva nueva"
    assert conversacion.generando_desde is not None
