"""Textos que le llegan al usuario. Separados de la lógica para poder
ajustarlos sin tocar código (ver spec-etapa2.md, sección "Mensaje de
escalamiento").
"""

from datetime import datetime

HORARIO_ATENCION_DESDE = 9
HORARIO_ATENCION_HASTA = 17
DIAS_HABILES = range(0, 5)  # lunes=0 ... domingo=6, según datetime.weekday()

MENSAJE_ESCALAMIENTO_EN_HORARIO = (
    "Tu consulta pasó a una persona del equipo, en breve te responden por acá."
)

MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO = (
    "Tu consulta quedó registrada. Nuestro horario de atención es de lunes a "
    "viernes de 9 a 17, y en ese horario te responde una persona del equipo."
)

MENSAJE_LIMITE_ALCANZADO = (
    "Recibimos muchos mensajes tuyos en poco tiempo. Esperá un rato antes de "
    "escribir de nuevo."
)

MENSAJE_ERROR_GENERICO = (
    "Perdón, tuvimos un problema para responderte. Ya avisamos a una persona "
    "del equipo para que te contacte."
)

MENSAJE_ERROR_TRANSITORIO = (
    "Perdón, tuvimos un problema técnico. Probá de nuevo en un momento."
)


def esta_en_horario_atencion(ahora: datetime) -> bool:
    """`ahora` tiene que venir ya convertido a la zona horaria del polo
    (ver config.timezone). Los feriados no se contemplan en esta etapa
    (deuda conocida, ver spec-etapa2.md)."""
    return ahora.weekday() in DIAS_HABILES and HORARIO_ATENCION_DESDE <= ahora.hour < HORARIO_ATENCION_HASTA


def mensaje_escalamiento(ahora: datetime) -> str:
    if esta_en_horario_atencion(ahora):
        return MENSAJE_ESCALAMIENTO_EN_HORARIO
    return MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO
