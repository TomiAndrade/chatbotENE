"""El botón "Reactivar bot": lo único que el CRM escribe.

Tiene que hacer una cosa sola —apagar la pausa— y ninguna más: no mandar
mensajes, no borrar historial, no responder los mensajes que quedaron sin
contestar mientras estaba pausada. El bot vuelve a hablar recién con el
próximo mensaje que llegue, con las reglas de siempre.
"""

import json
from datetime import datetime, timedelta, timezone

from app.crm.auth import HEADER_CSRF
from app.crm.rutas import MENSAJE_BOT_REACTIVADO
from app.models import Conversacion, Mensaje, MotivoPausa, RolMensaje
from tests.conftest import TELEFONO_DE_PRUEBA, login_crm
from tests.helpers import crear_conversacion, firmar_meta, payload_meta_texto

SECRETO_META = "test-app-secret"


def _escalada(db, identificador=TELEFONO_DE_PRUEBA) -> Conversacion:
    hace_un_rato = datetime.now(timezone.utc) - timedelta(minutes=15)
    return crear_conversacion(
        db,
        identificador,
        [
            (RolMensaje.USUARIO, "quiero reservar el auditorio", 16),
            (RolMensaje.BOT, "tu consulta pasó a una persona del equipo", 15),
        ],
        modo_humano=True,
        motivo_pausa=MotivoPausa.ESCALAMIENTO,
        modo_humano_desde=hace_un_rato,
        resumen_escalamiento="Quiere reservar el auditorio",
        escalada_en=hace_un_rato,
    )


def test_reactivar_apaga_la_pausa_y_lo_dice(cliente_crm, usuario_crm, db):
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _escalada(db)

    respuesta = cliente_crm.post(
        f"/crm/api/conversaciones/{conversacion.id}/reactivar", headers={HEADER_CSRF: csrf}
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["conversacion"]["pausado"] is False
    assert cuerpo["conversacion"]["motivo_pausa"] is None
    # Contra la constante del servidor, no contra el texto escrito a mano: si
    # el mensaje cambia, cambia en un solo lado.
    assert cuerpo["mensaje"] == MENSAJE_BOT_REACTIVADO

    db.refresh(conversacion)
    assert conversacion.modo_humano is False
    assert conversacion.motivo_pausa is None
    assert conversacion.modo_humano_desde is None
    assert conversacion.resumen_escalamiento is None
    assert conversacion.escalada_en is None


def test_reactivar_no_toca_los_mensajes(cliente_crm, usuario_crm, db):
    """Reactivar es levantar la pausa, no archivar la conversación."""
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _escalada(db)
    antes = [
        (m.id, m.rol, m.contenido)
        for m in db.query(Mensaje).filter_by(conversacion_id=conversacion.id).order_by(Mensaje.id).all()
    ]

    cliente_crm.post(f"/crm/api/conversaciones/{conversacion.id}/reactivar", headers={HEADER_CSRF: csrf})

    despues = [
        (m.id, m.rol, m.contenido)
        for m in db.query(Mensaje).filter_by(conversacion_id=conversacion.id).order_by(Mensaje.id).all()
    ]
    assert despues == antes


def test_reactivar_no_manda_nada_por_whatsapp(cliente_crm, usuario_crm, db, meta_enviados):
    """Al usuario no le llega ningún mensaje por reactivar: del otro lado no
    pasó nada. Y tampoco se re-responden los mensajes viejos."""
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _escalada(db)

    cliente_crm.post(f"/crm/api/conversaciones/{conversacion.id}/reactivar", headers={HEADER_CSRF: csrf})

    assert meta_enviados == []


def test_despues_de_reactivar_el_proximo_mensaje_sigue_el_flujo_normal(
    cliente_crm, usuario_crm, client, db, meta_enviados
):
    """El criterio que importa: reactivar tiene que devolver la conversación
    al circuito de siempre. El bot no contesta por reactivar, contesta cuando
    llega un mensaje nuevo."""
    csrf = login_crm(cliente_crm, usuario_crm)
    conversacion = _escalada(db)

    # Con la pausa puesta, un mensaje entrante no recibe respuesta.
    _entrante(client, "wamid.reactivar.1", "¿me confirmaron la sala?")
    assert meta_enviados == []

    cliente_crm.post(f"/crm/api/conversaciones/{conversacion.id}/reactivar", headers={HEADER_CSRF: csrf})
    assert meta_enviados == []

    # Y con la pausa levantada, el mensaje siguiente sí.
    _entrante(client, "wamid.reactivar.2", "¿hola?")

    assert len(meta_enviados) == 1
    assert meta_enviados[0][0] == TELEFONO_DE_PRUEBA

    mensajes = db.query(Mensaje).filter_by(conversacion_id=conversacion.id).order_by(Mensaje.id).all()
    # Los dos entrantes quedaron guardados, incluido el que llegó durante la
    # pausa: lo que no hay es una respuesta para ese.
    contenidos = [m.contenido for m in mensajes]
    assert "¿me confirmaron la sala?" in contenidos
    assert "¿hola?" in contenidos


def test_reactivar_sin_token_csrf_no_hace_nada(cliente_crm, usuario_crm, db):
    """La cookie sola no alcanza para escribir: sin el token CSRF que solo
    puede leer el panel, la conversación queda como estaba."""
    login_crm(cliente_crm, usuario_crm)
    conversacion = _escalada(db)

    respuesta = cliente_crm.post(f"/crm/api/conversaciones/{conversacion.id}/reactivar")

    assert respuesta.status_code == 403
    db.refresh(conversacion)
    assert conversacion.modo_humano is True


def test_reactivar_con_un_token_csrf_de_mentira_no_hace_nada(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    conversacion = _escalada(db)

    respuesta = cliente_crm.post(
        f"/crm/api/conversaciones/{conversacion.id}/reactivar",
        headers={HEADER_CSRF: "token-inventado"},
    )

    assert respuesta.status_code == 403
    db.refresh(conversacion)
    assert conversacion.modo_humano is True


def test_reactivar_sin_sesion_no_hace_nada(cliente_crm, usuario_crm, db):
    conversacion = _escalada(db)

    respuesta = cliente_crm.post(f"/crm/api/conversaciones/{conversacion.id}/reactivar")

    assert respuesta.status_code == 401
    db.refresh(conversacion)
    assert conversacion.modo_humano is True


def test_reactivar_una_conversacion_que_no_existe_da_404(cliente_crm, usuario_crm):
    csrf = login_crm(cliente_crm, usuario_crm)

    respuesta = cliente_crm.post("/crm/api/conversaciones/9999/reactivar", headers={HEADER_CSRF: csrf})

    assert respuesta.status_code == 404


def _entrante(client, wa_message_id: str, texto: str):
    payload = payload_meta_texto(wa_message_id, TELEFONO_DE_PRUEBA, texto)
    cuerpo = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook",
        content=cuerpo,
        headers={"X-Hub-Signature-256": firmar_meta(cuerpo, SECRETO_META)},
    )
