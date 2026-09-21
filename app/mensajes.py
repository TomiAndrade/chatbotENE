"""Textos que le llegan al usuario. Separados de la lógica para poder
ajustarlos sin tocar código (ver spec-etapa2.md, sección "Mensaje de
escalamiento").
"""

from datetime import datetime

HORARIO_ATENCION_DESDE = 8
HORARIO_ATENCION_HASTA = 18
DIAS_HABILES = range(0, 5)  # lunes=0 ... domingo=6, según datetime.weekday()

MENSAJE_ESCALAMIENTO_EN_HORARIO = (
    "Tu consulta pasó a una persona del equipo, en breve te responden por acá."
)

# El horario se interpola desde las constantes a propósito: cuando estaba
# escrito a mano el texto siguió diciendo "de 9 a 17" después de que el
# horario real pasara a 8-18, y el bot terminó contradiciéndose solo (decía
# un horario si se lo preguntaban y otro al escalar).
MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO = (
    "Tu consulta quedó registrada. Nuestro horario de atención es de lunes a "
    f"viernes de {HORARIO_ATENCION_DESDE} a {HORARIO_ATENCION_HASTA}, y en ese "
    "horario te responde una persona del equipo."
)

MENSAJE_LIMITE_ALCANZADO = (
    "Recibimos muchos mensajes tuyos en poco tiempo. Esperá un rato antes de "
    "escribir de nuevo."
)

MENSAJE_ERROR_GENERICO = (
    "Perdón, tuvimos un problema para responderte. Ya avisamos a una persona "
    "del equipo para que te contacte."
)

# Variante de MENSAJE_ERROR_GENERICO para cuando ESCALAMIENTO_HABILITADO=false
# (ver spec-derivacion.md): sin bandeja de entrada no hay a quién pasarle la
# conversación, así que prometer "ya avisamos a una persona" es mentirle al
# usuario. En vez de eso se le da el canal por el que sí lo van a atender.
#
# El mail está acá y en prompts/system-prompt.md — si cambia, cambian los dos.
MAIL_RECEPCION = "recepcion.ene.pctnqn@gmail.com"

MENSAJE_ERROR_SIN_ESCALAMIENTO = (
    "Perdón, tuvimos un problema para responderte. Probá de nuevo en un rato, "
    f"o escribinos a {MAIL_RECEPCION} y te respondemos por ahí."
)

MENSAJE_ERROR_TRANSITORIO = (
    "Perdón, tuvimos un problema técnico. Probá de nuevo en un momento."
)

# Textos fijos para adjuntos no soportados (ver specs/spec-adjuntos-no-soportados.md,
# entrega 1.1 de roadmap-bot-crm.md). No llaman al modelo ni escalan por sí
# solos: solo piden la consulta por escrito. `image`/`document`/`audio` tienen
# texto propio porque son los tipos más comunes en WhatsApp; cualquier otro
# `type` (video, sticker, ubicación, contacto, interactivo, etc.) o su
# ausencia usa el genérico.
MENSAJE_ADJUNTO_IMAGEN = (
    "Por ahora no puedo ver imágenes. Contame tu consulta por escrito y te ayudo."
)

MENSAJE_ADJUNTO_DOCUMENTO = (
    "Por ahora no puedo abrir documentos. Contame tu consulta por escrito y te ayudo."
)

MENSAJE_ADJUNTO_AUDIO = (
    "Por ahora no puedo escuchar audios. Contame tu consulta por escrito y te ayudo."
)

MENSAJE_ADJUNTO_GENERICO = (
    "Por ahora no puedo procesar este tipo de mensaje. Contame tu consulta por "
    "escrito y te ayudo."
)

_MENSAJES_ADJUNTO_POR_TIPO = {
    "image": MENSAJE_ADJUNTO_IMAGEN,
    "document": MENSAJE_ADJUNTO_DOCUMENTO,
    "audio": MENSAJE_ADJUNTO_AUDIO,
}


def mensaje_adjunto_no_soportado(tipo: str | None) -> str:
    """El texto fijo que corresponde a un `messages[].type` que no es
    `text`. `tipo` viene de la metadata real del webhook, nunca de una
    coincidencia textual con el marcador guardado en el mensaje."""
    return _MENSAJES_ADJUNTO_POR_TIPO.get(tipo, MENSAJE_ADJUNTO_GENERICO)


def esta_en_horario_atencion(ahora: datetime) -> bool:
    """`ahora` tiene que venir ya convertido a la zona horaria del polo
    (ver config.timezone). Los feriados no se contemplan en esta etapa
    (deuda conocida, ver spec-etapa2.md)."""
    return ahora.weekday() in DIAS_HABILES and HORARIO_ATENCION_DESDE <= ahora.hour < HORARIO_ATENCION_HASTA


def mensaje_escalamiento(ahora: datetime) -> str:
    if esta_en_horario_atencion(ahora):
        return MENSAJE_ESCALAMIENTO_EN_HORARIO
    return MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO
