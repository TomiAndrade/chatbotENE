"""Exportación de una conversación completa a Markdown desde el CRM
(app/crm/exportar.py, endpoint en app/crm/rutas.py).

El caso a cubrir con más cuidado es que la exportación no dependa de la
paginación del panel (`servicio.MENSAJES_POR_PAGINA`): tiene que traer
siempre el historial entero.
"""

from app.crm import servicio
from app.models import Mensaje, RolMensaje
from tests.conftest import login_crm
from tests.helpers import crear_conversacion

NUMERO_DE_PRUEBA = "5492995551234"


def test_exportar_incluye_todos_los_mensajes_en_orden_cronologico(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    conversacion = crear_conversacion(
        db,
        NUMERO_DE_PRUEBA,
        [
            (RolMensaje.USUARIO, "hola, quiero info", 10),
            (RolMensaje.BOT, "hola! contame en qué te ayudo", 9),
            (RolMensaje.HUMANO, "te respondo yo, soy Ana", 5),
        ],
    )

    respuesta = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/exportar")
    texto = respuesta.content.decode("utf-8")

    assert respuesta.status_code == 200
    orden = [
        texto.find("hola, quiero info"),
        texto.find("hola! contame en qué te ayudo"),
        texto.find("te respondo yo, soy Ana"),
    ]
    assert orden == sorted(orden)
    assert all(pos != -1 for pos in orden)


def test_exportar_distingue_usuario_bot_y_humano(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    conversacion = crear_conversacion(
        db,
        NUMERO_DE_PRUEBA,
        [
            (RolMensaje.USUARIO, "consulta", 3),
            (RolMensaje.BOT, "respuesta del bot", 2),
            (RolMensaje.HUMANO, "respuesta de una persona", 1),
        ],
    )

    texto = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/exportar").content.decode("utf-8")

    assert "### Usuario —" in texto
    assert "### Bot —" in texto
    assert "### Humano —" in texto


def test_exportar_incluye_autor_humano_cuando_existe_y_tolera_null(
    cliente_crm, usuario_crm, db
):
    login_crm(cliente_crm, usuario_crm)
    conversacion = crear_conversacion(
        db,
        NUMERO_DE_PRUEBA,
        [
            (RolMensaje.HUMANO, "sin autor conocido", 2),
            (RolMensaje.HUMANO, "con autor conocido", 1),
        ],
    )
    mensajes = db.query(Mensaje).filter_by(conversacion_id=conversacion.id).order_by(Mensaje.id).all()
    mensajes[1].autor_crm_id = usuario_crm.id
    db.commit()

    texto = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/exportar").content.decode("utf-8")

    assert "### Humano —" in texto
    assert f"### Humano ({usuario_crm.usuario}) —" in texto


def test_exportar_trae_el_historial_completo_aunque_supere_una_pagina(cliente_crm, usuario_crm, db):
    """La misma prueba que ya hace test_crm_historial.py con `mensajes_de`
    (paginado), pero para la exportación: acá no puede haber "hay_anteriores",
    tienen que estar los 120 mensajes en un solo archivo."""
    login_crm(cliente_crm, usuario_crm)
    cantidad = servicio.MENSAJES_POR_PAGINA * 2 + 20  # bien por encima de una página
    historia = [(RolMensaje.USUARIO, f"mensaje numero {n}", cantidad - n) for n in range(cantidad)]
    conversacion = crear_conversacion(db, NUMERO_DE_PRUEBA, historia)

    texto = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/exportar").content.decode("utf-8")

    assert texto.count("### Usuario —") == cantidad
    assert "mensaje numero 0" in texto
    assert f"mensaje numero {cantidad - 1}" in texto
    # Orden cronológico: el primero mandado aparece antes que el último.
    assert texto.find("mensaje numero 0") < texto.find(f"mensaje numero {cantidad - 1}")


def test_exportar_conserva_caracteres_utf8(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    texto_original = "¿Tienen sala para 10 personas el sábado? 🙂 Necesito confirmación mañana"
    conversacion = crear_conversacion(db, NUMERO_DE_PRUEBA, [(RolMensaje.USUARIO, texto_original, 1)])

    respuesta = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/exportar")

    assert "charset=utf-8" in respuesta.headers["content-type"]
    assert texto_original in respuesta.content.decode("utf-8")


def test_exportar_requiere_sesion(cliente_crm, usuario_crm, db):
    conversacion = crear_conversacion(db, NUMERO_DE_PRUEBA, [(RolMensaje.USUARIO, "hola", 1)])

    assert cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/exportar").status_code == 401


def test_exportar_content_type_y_content_disposition(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    conversacion = crear_conversacion(db, NUMERO_DE_PRUEBA, [(RolMensaje.USUARIO, "hola", 1)])

    respuesta = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/exportar")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"] == "text/markdown; charset=utf-8"
    assert respuesta.headers["content-disposition"] == f'attachment; filename="conversacion-{conversacion.id}.md"'


def test_exportar_una_conversacion_que_no_existe_da_404(cliente_crm, usuario_crm):
    login_crm(cliente_crm, usuario_crm)

    assert cliente_crm.get("/crm/api/conversaciones/9999/exportar").status_code == 404


def test_exportar_marca_un_adjunto_no_soportado_con_su_tipo_real(cliente_crm, usuario_crm, db):
    """No hay contenido de archivo guardado (ver specs/spec-adjuntos-no-soportados.md):
    la exportación tiene que usar el tipo real de la columna `Mensaje.tipo`,
    no inventar una descripción del adjunto."""
    login_crm(cliente_crm, usuario_crm)
    conversacion = crear_conversacion(db, NUMERO_DE_PRUEBA, [])
    db.add(
        Mensaje(
            conversacion_id=conversacion.id,
            rol=RolMensaje.USUARIO,
            contenido="[mensaje de tipo 'image' no soportado en esta etapa]",
            tipo="image",
        )
    )
    db.commit()

    texto = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/exportar").content.decode("utf-8")

    assert "tipo: image" in texto


def test_exportar_encabezado_incluye_canal_e_identificador(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    conversacion = crear_conversacion(db, NUMERO_DE_PRUEBA, [(RolMensaje.USUARIO, "hola", 1)])

    texto = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/exportar").content.decode("utf-8")

    assert f"# Conversación #{conversacion.id}" in texto
    assert "Canal: whatsapp" in texto
    assert f"Identificador: {NUMERO_DE_PRUEBA}" in texto
    assert "Iniciada:" in texto
    assert "Exportada:" in texto
