"""Mensaje de escalamiento según horario, con la hora inyectada (ver
spec-etapa2.md, test 4: un martes a las 11 y un sábado a las 22 dan textos
distintos)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.mensajes import (
    HORARIO_ATENCION_DESDE,
    HORARIO_ATENCION_HASTA,
    MENSAJE_ESCALAMIENTO_EN_HORARIO,
    MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO,
    esta_en_horario_atencion,
    mensaje_escalamiento,
)

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")


def test_martes_a_las_11_esta_en_horario():
    martes_11 = datetime(2026, 7, 28, 11, 0, tzinfo=BUENOS_AIRES)  # martes

    assert esta_en_horario_atencion(martes_11) is True
    assert mensaje_escalamiento(martes_11) == MENSAJE_ESCALAMIENTO_EN_HORARIO


def test_sabado_a_las_22_esta_fuera_de_horario():
    sabado_22 = datetime(2026, 8, 1, 22, 0, tzinfo=BUENOS_AIRES)  # sábado

    assert esta_en_horario_atencion(sabado_22) is False
    assert mensaje_escalamiento(sabado_22) == MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO


def test_los_dos_mensajes_son_distintos():
    martes_11 = datetime(2026, 7, 28, 11, 0, tzinfo=BUENOS_AIRES)
    sabado_22 = datetime(2026, 8, 1, 22, 0, tzinfo=BUENOS_AIRES)

    assert mensaje_escalamiento(martes_11) != mensaje_escalamiento(sabado_22)


def test_limites_del_horario_de_atencion():
    """El horario es 8-18, unificado con el del edificio que informa el
    knowledge base. Las 17 quedan adentro: eran el borde cuando el horario
    era 9-17 y son el caso que delata si alguien revierte las constantes."""
    viernes_8_en_punto = datetime(2026, 7, 31, 8, 0, tzinfo=BUENOS_AIRES)
    viernes_17_en_punto = datetime(2026, 7, 31, 17, 0, tzinfo=BUENOS_AIRES)
    viernes_18_en_punto = datetime(2026, 7, 31, 18, 0, tzinfo=BUENOS_AIRES)  # ya cerró

    assert esta_en_horario_atencion(viernes_8_en_punto) is True
    assert esta_en_horario_atencion(viernes_17_en_punto) is True
    assert esta_en_horario_atencion(viernes_18_en_punto) is False


def test_el_mensaje_fuera_de_horario_dice_el_horario_real():
    """Estaba escrito a mano y quedó diciendo "de 9 a 17" cuando el horario
    real pasó a 8-18. Si alguien vuelve a hardcodearlo, esto lo agarra."""
    assert (
        f"de {HORARIO_ATENCION_DESDE} a {HORARIO_ATENCION_HASTA}"
        in MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO
    )
