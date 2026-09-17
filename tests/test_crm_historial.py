"""Lo que el CRM muestra: la lista de conversaciones, el historial paginado y
el estado de pausa de cada una (app/crm/servicio.py).

El estado que importa es el que ve el bot, no el que sugiere la columna
`modo_humano` sola: una pausa por intervención manual vencida es una
conversación que el bot ya está respondiendo, y el panel tiene que decir eso
(ver `pausa_vigente` en app/pausa.py).
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import config
from app.models import Mensaje, MotivoPausa, RolMensaje
from tests.conftest import login_crm
from tests.helpers import crear_conversacion

# Dos formas del mismo número argentino: con el "9" y sin él. Meta manda una u
# otra según cómo lo resuelva internamente y el bot las guarda tal cual, sin
# normalizar (spec-meta-cloud-api.md, sección 3).
NUMERO_CON_NUEVE = "5492995551234"
NUMERO_SIN_NUEVE = "542995559876"


def _ahora():
    return datetime.now(timezone.utc)


def test_las_conversaciones_vienen_de_la_mas_reciente_a_la_mas_vieja(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    crear_conversacion(db, "5492995550001", [(RolMensaje.USUARIO, "mensaje viejo", 300)])
    crear_conversacion(db, "5492995550002", [(RolMensaje.USUARIO, "mensaje de recién", 1)])
    crear_conversacion(db, "5492995550003", [(RolMensaje.USUARIO, "mensaje de hace un rato", 60)])

    datos = cliente_crm.get("/crm/api/conversaciones").json()

    assert [c["identificador_externo"] for c in datos["conversaciones"]] == [
        "5492995550002",
        "5492995550003",
        "5492995550001",
    ]
    assert datos["total"] == 3


def test_el_identificador_se_muestra_tal_cual_esta_guardado(cliente_crm, usuario_crm, db):
    """Ni se normaliza el "9" ni se le da formato: el panel tiene que mostrar
    el mismo string con el que el bot responde."""
    login_crm(cliente_crm, usuario_crm)
    crear_conversacion(db, NUMERO_CON_NUEVE, [(RolMensaje.USUARIO, "hola", 2)])
    crear_conversacion(db, NUMERO_SIN_NUEVE, [(RolMensaje.USUARIO, "hola", 1)])

    datos = cliente_crm.get("/crm/api/conversaciones").json()
    identificadores = [c["identificador_externo"] for c in datos["conversaciones"]]

    assert identificadores == [NUMERO_SIN_NUEVE, NUMERO_CON_NUEVE]


def test_la_lista_trae_la_vista_previa_del_ultimo_mensaje(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    crear_conversacion(
        db,
        NUMERO_CON_NUEVE,
        [
            (RolMensaje.USUARIO, "¿cuánto sale la sala?", 10),
            (RolMensaje.BOT, "La sala de reuniones sale $X por hora", 9),
        ],
    )

    conversacion = cliente_crm.get("/crm/api/conversaciones").json()["conversaciones"][0]

    assert conversacion["vista_previa"]["rol"] == "bot"
    assert conversacion["vista_previa"]["texto"] == "La sala de reuniones sale $X por hora"


def test_el_historial_diferencia_usuario_bot_y_equipo_con_su_fecha(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    conversacion = crear_conversacion(
        db,
        NUMERO_CON_NUEVE,
        [
            (RolMensaje.USUARIO, "hola, quiero alquilar el auditorio", 30),
            (RolMensaje.BOT, "te paso los datos del auditorio", 29),
            (RolMensaje.HUMANO, "hola, soy Ana de recepción", 5),
        ],
    )

    datos = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/mensajes").json()

    assert [m["rol"] for m in datos["mensajes"]] == ["usuario", "bot", "humano"]
    assert [m["contenido"] for m in datos["mensajes"]] == [
        "hola, quiero alquilar el auditorio",
        "te paso los datos del auditorio",
        "hola, soy Ana de recepción",
    ]
    # Cada mensaje viaja con su instante, aware y en UTC, para que el panel lo
    # pueda mostrar en la zona del polo sin adivinar.
    for mensaje in datos["mensajes"]:
        assert datetime.fromisoformat(mensaje["creado_en"]).tzinfo is not None


def test_el_historial_largo_se_pide_por_paginas(cliente_crm, usuario_crm, db):
    """Al abrir se ven los últimos mensajes; los anteriores se piden con
    `antes_de`, que es lo que hace el botón "Cargar mensajes anteriores"."""
    login_crm(cliente_crm, usuario_crm)
    historia = [(RolMensaje.USUARIO, f"mensaje {numero}", 200 - numero) for numero in range(120)]
    conversacion = crear_conversacion(db, NUMERO_CON_NUEVE, historia)

    ultima_pagina = cliente_crm.get(
        f"/crm/api/conversaciones/{conversacion.id}/mensajes?limite=50"
    ).json()

    assert len(ultima_pagina["mensajes"]) == 50
    assert ultima_pagina["hay_anteriores"] is True
    assert ultima_pagina["mensajes"][-1]["contenido"] == "mensaje 119"

    primer_id = ultima_pagina["mensajes"][0]["id"]
    anterior = cliente_crm.get(
        f"/crm/api/conversaciones/{conversacion.id}/mensajes?limite=50&antes_de={primer_id}"
    ).json()

    assert len(anterior["mensajes"]) == 50
    assert anterior["mensajes"][-1]["id"] < primer_id
    assert anterior["hay_anteriores"] is True


def test_el_refresco_solo_pide_lo_que_llego_despues(cliente_crm, usuario_crm, db):
    """`desde` es lo que usa el refresco periódico: si trajera todo de nuevo,
    el panel tendría que repintar la conversación entera cada 8 segundos y el
    scroll de quien está leyendo se movería solo."""
    login_crm(cliente_crm, usuario_crm)
    conversacion = crear_conversacion(
        db, NUMERO_CON_NUEVE, [(RolMensaje.USUARIO, "primero", 10), (RolMensaje.BOT, "segundo", 9)]
    )
    primera_carga = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/mensajes").json()
    ultimo_id = primera_carga["mensajes"][-1]["id"]

    db.add(Mensaje(conversacion_id=conversacion.id, rol=RolMensaje.USUARIO, contenido="tercero"))
    db.commit()

    nuevos = cliente_crm.get(
        f"/crm/api/conversaciones/{conversacion.id}/mensajes?desde={ultimo_id}"
    ).json()

    assert [m["contenido"] for m in nuevos["mensajes"]] == ["tercero"]


def test_el_contenido_del_mensaje_viaja_tal_cual_sin_interpretar(cliente_crm, usuario_crm, db):
    """Un mensaje con HTML adentro es texto, no marcado: el servidor lo
    devuelve verbatim y el panel lo pinta con textContent (ver el test de
    abajo), así que nunca se ejecuta."""
    login_crm(cliente_crm, usuario_crm)
    peligroso = "<script>alert('hola')</script> ¿está disponible?"
    conversacion = crear_conversacion(db, NUMERO_CON_NUEVE, [(RolMensaje.USUARIO, peligroso, 1)])

    datos = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/mensajes").json()

    assert datos["mensajes"][0]["contenido"] == peligroso


def test_el_panel_no_usa_innerhtml_en_ningun_lado():
    """El único lugar donde el contenido de un mensaje podría ejecutarse es el
    JavaScript del panel. Este test es la red que avisa si alguien alguna vez
    cambia un textContent por un innerHTML."""
    raiz = Path(__file__).resolve().parents[1]
    javascript = (raiz / "app" / "crm" / "estaticos" / "panel.js").read_text(encoding="utf-8")

    # Se busca el acceso a la propiedad (con el punto), no la palabra suelta:
    # el propio archivo la nombra en el comentario que explica por qué no se
    # usa, y ese comentario no tiene que hacer fallar el test.
    assert ".innerHTML" not in javascript
    assert ".outerHTML" not in javascript
    assert "insertAdjacentHTML(" not in javascript


# --- La barra de error no puede aparecer sola --------------------------
#
# Pasó de verdad: el panel mostraba una franja roja vacía, con el botón
# "Reintentar" al costado, aunque la carga hubiera salido bien y no hubiera
# ninguna conversación. El JavaScript le ponía `hidden` como corresponde; lo
# que fallaba era el CSS.
#
# El navegador aplica `hidden` con una regla suya, `[hidden] { display: none }`,
# y esa regla **pierde** contra cualquier regla nuestra que fije `display`.
# `.barra-error` fija `display: flex`, así que el atributo no hacía nada.

RUTA_ESTATICOS = Path(__file__).resolve().parents[1] / "app" / "crm"


def _panel_html() -> str:
    return (RUTA_ESTATICOS / "paginas" / "panel.html").read_text(encoding="utf-8")


def _crm_css() -> str:
    return (RUTA_ESTATICOS / "estaticos" / "crm.css").read_text(encoding="utf-8")


def _clases_de_elementos_ocultables(html: str) -> set[str]:
    """Las clases de cada elemento que arranca con el atributo `hidden`."""
    clases = set()
    for etiqueta in re.findall(r"<[^>]*\bhidden\b[^>]*>", html):
        atributo = re.search(r'class="([^"]*)"', etiqueta)
        if atributo:
            clases.update(atributo.group(1).split())
    return clases


def _clases_que_fijan_display(css: str) -> set[str]:
    """Las clases para las que el CSS declara un `display` propio: son las que
    le ganarían al `hidden` del navegador."""
    con_display = set()
    for selector, cuerpo in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if not re.search(r"(^|[;\s])display\s*:", cuerpo):
            continue
        con_display.update(re.findall(r"\.([a-zA-Z0-9_-]+)", selector))
    return con_display


def test_el_css_hace_que_hidden_gane_siempre():
    """La regla que arregla el bug de la franja vacía. Va con `!important`
    porque sin él hay que acordarse de una excepción por cada clase nueva que
    fije `display`, y el día que alguien se olvide el síntoma es un cartel que
    aparece solo."""
    css = _crm_css()

    assert re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", css)


def test_ningun_elemento_ocultable_puede_quedar_visible_por_su_clase():
    """El test que habría atajado el bug.

    Si alguna clase de un elemento que arranca con `hidden` declara su propio
    `display`, el atributo solo funciona gracias a la regla de arriba. Este
    test falla si esa regla desaparece **y** existe al menos un elemento en esa
    situación — o sea que no puede pasar por un vacío: se afirma también que
    el caso existe.
    """
    ocultables = _clases_de_elementos_ocultables(_panel_html())
    con_display = _clases_que_fijan_display(_crm_css())

    en_riesgo = ocultables & con_display

    # `barra-error` es la que provocó el bug; si alguien la saca, este test
    # tiene que dejar de pasar por casualidad y no en silencio.
    assert "barra-error" in en_riesgo
    assert re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", _crm_css()), (
        f"Estas clases le ganarían al atributo hidden sin la regla global: {sorted(en_riesgo)}"
    )


def test_la_barra_de_error_arranca_oculta_y_sin_texto():
    """Nada de texto escrito en el HTML: lo pone `mostrarError` cuando hay algo
    que decir. Si viniera escrito, la franja tendría contenido antes de que
    fallara nada."""
    html = _panel_html()

    barra = re.search(r'<p id="error-global"[^>]*>(.*?)</p>', html, re.DOTALL)
    assert barra is not None
    assert "hidden" in barra.group(0)
    assert re.search(r'<span id="error-global-texto"></span>', barra.group(1))


def test_el_panel_no_muestra_la_barra_de_error_fuera_de_mostrarError():
    """Un solo lugar prende la barra. Si otro la prendiera por su cuenta, la
    regla de "solo cuando falla una carga" dependería de acordarse."""
    javascript = (RUTA_ESTATICOS / "estaticos" / "panel.js").read_text(encoding="utf-8")

    assert javascript.count("errorGlobal.hidden = false") == 1
    # Y el texto nunca queda vacío: hay un fallback para la excepción que no
    # trae mensaje.
    assert "ERROR_SIN_MENSAJE" in javascript


# --- El historial tiene que poder scrollear ----------------------------
#
# Pasó de verdad: con más mensajes que pantalla, los de abajo quedaban
# recortados y no había forma de llegar a ellos. `.hilo` ya declaraba
# `flex: 1; min-height: 0; overflow-y: auto`, pero esas tres no hacen nada si
# el padre no es un contenedor flex — y el padre, `article#detalle`, no tenía
# ninguna regla: quedaba `display: block`.


def _declaraciones(css: str, clase: str) -> str:
    """El cuerpo de la regla de esa clase sola (no `.a .b` ni `.a.b`)."""
    regla = re.search(rf"^\.{re.escape(clase)}\s*\{{([^}}]*)\}}", css, re.MULTILINE)
    return regla.group(1) if regla else ""


def test_el_hilo_scrollea_porque_su_padre_es_una_columna_flex():
    """El test que habría atajado el bug del historial sin scroll.

    Afirma la cadena entera, que es lo que falla en pedazos: el hilo vive
    adentro de `#detalle`, `#detalle` declara la columna flex, y el hilo
    declara el scroll. Si alguien saca cualquiera de las tres, esto falla.
    """
    html = _panel_html()
    css = _crm_css()

    articulo = re.search(r'(<article[^>]*\bid="detalle"[^>]*>)(.*?)</article>', html, re.DOTALL)
    assert articulo is not None, "no está el contenedor del historial"
    apertura, adentro = articulo.group(1), articulo.group(2)

    assert 'id="hilo"' in adentro, "el hilo dejó de estar adentro de #detalle"

    clases = re.search(r'class="([^"]*)"', apertura)
    assert clases is not None and "detalle" in clases.group(1).split(), (
        "#detalle se quedó sin la clase que le da la columna flex"
    )

    # Sin `min-height: 0` un ítem flex no baja de su altura de contenido, así
    # que el hilo empujaría hacia abajo en vez de scrollear.
    for declaracion in ("display: flex", "flex-direction: column", "min-height: 0", "flex: 1"):
        assert declaracion in _declaraciones(css, "detalle"), (
            f".detalle perdió {declaracion!r} y el hilo deja de scrollear"
        )

    for declaracion in ("overflow-y: auto", "min-height: 0", "flex: 1"):
        assert declaracion in _declaraciones(css, "hilo"), (
            f".hilo perdió {declaracion!r} y deja de scrollear"
        )


def test_una_conversacion_escalada_se_muestra_pausada_con_su_motivo(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    escalada_en = _ahora() - timedelta(minutes=20)
    conversacion = crear_conversacion(
        db,
        NUMERO_CON_NUEVE,
        [(RolMensaje.USUARIO, "necesito hablar con alguien", 21)],
        modo_humano=True,
        motivo_pausa=MotivoPausa.ESCALAMIENTO,
        modo_humano_desde=escalada_en,
        resumen_escalamiento="Pide una reserva del auditorio para el viernes",
        escalada_en=escalada_en,
    )

    datos = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/mensajes").json()["conversacion"]

    assert datos["pausado"] is True
    assert datos["motivo_pausa"] == "escalamiento"
    assert datos["resumen_escalamiento"] == "Pide una reserva del auditorio para el viernes"
    assert datos["escalada_en"] is not None


def test_una_intervencion_manual_vigente_se_muestra_pausada(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    conversacion = crear_conversacion(
        db,
        NUMERO_CON_NUEVE,
        [(RolMensaje.HUMANO, "te respondo yo", 5)],
        modo_humano=True,
        motivo_pausa=MotivoPausa.INTERVENCION_MANUAL,
        modo_humano_desde=_ahora() - timedelta(minutes=5),
    )

    datos = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/mensajes").json()["conversacion"]

    assert datos["pausado"] is True
    assert datos["motivo_pausa"] == "intervencion_manual"


def test_una_intervencion_manual_vencida_se_muestra_activa(cliente_crm, usuario_crm, db):
    """El flag `modo_humano` sigue prendido en la base, pero la ventana ya
    pasó y el bot volvió a responder. Si el panel mostrara el flag crudo,
    diría "pausado" de una conversación que el bot está atendiendo."""
    login_crm(cliente_crm, usuario_crm)
    vencida_hace_rato = _ahora() - timedelta(minutes=config.pausa_humana_minutos + 30)
    conversacion = crear_conversacion(
        db,
        NUMERO_CON_NUEVE,
        [(RolMensaje.HUMANO, "te respondí hace mucho", 200)],
        modo_humano=True,
        motivo_pausa=MotivoPausa.INTERVENCION_MANUAL,
        modo_humano_desde=vencida_hace_rato,
    )

    datos = cliente_crm.get(f"/crm/api/conversaciones/{conversacion.id}/mensajes").json()["conversacion"]

    assert datos["pausado"] is False
    assert datos["motivo_pausa"] is None


def test_el_filtro_de_pausadas_solo_trae_las_que_siguen_pausadas(cliente_crm, usuario_crm, db):
    login_crm(cliente_crm, usuario_crm)
    crear_conversacion(db, "5492995550010", [(RolMensaje.USUARIO, "consulta normal", 3)])
    crear_conversacion(
        db,
        "5492995550011",
        [(RolMensaje.USUARIO, "quiero hablar con alguien", 2)],
        modo_humano=True,
        motivo_pausa=MotivoPausa.ESCALAMIENTO,
        modo_humano_desde=_ahora() - timedelta(minutes=10),
        escalada_en=_ahora() - timedelta(minutes=10),
    )
    crear_conversacion(
        db,
        "5492995550012",
        [(RolMensaje.HUMANO, "ya te respondí", 400)],
        modo_humano=True,
        motivo_pausa=MotivoPausa.INTERVENCION_MANUAL,
        modo_humano_desde=_ahora() - timedelta(minutes=config.pausa_humana_minutos + 60),
    )

    pausadas = cliente_crm.get("/crm/api/conversaciones?filtro=pausadas").json()
    todas = cliente_crm.get("/crm/api/conversaciones").json()

    assert [c["identificador_externo"] for c in pausadas["conversaciones"]] == ["5492995550011"]
    assert pausadas["total_pausadas"] == 1
    assert todas["total"] == 3
    # El contador de pausadas es el mismo mire donde mire el panel.
    assert todas["total_pausadas"] == 1


def test_pedir_una_conversacion_que_no_existe_da_404(cliente_crm, usuario_crm):
    login_crm(cliente_crm, usuario_crm)

    assert cliente_crm.get("/crm/api/conversaciones/9999/mensajes").status_code == 404
