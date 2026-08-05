"""Carrera de entregas concurrentes del mismo mensaje (ver spec-etapa1.md:
"los reintentos de Kapso son normales, no un error" y CLAUDE.md, dedup por
wa_message_id con IntegrityError atrapado).

Dos entregas del mismo webhook pueden pasar las dos el SELECT de duplicado
antes de que cualquiera haga commit. La constraint única de wa_message_id
tiene que ser la que decide, no la lectura previa.
"""

import threading

from app.db import SessionLocal
from app.main import buscar_o_crear_conversacion, procesar_mensaje_entrante
from app.models import Mensaje
from tests.conftest import TELEFONO_DE_PRUEBA

CANTIDAD_HILOS = 8


def test_entregas_concurrentes_del_mismo_mensaje_no_duplican(kapso_enviados):
    db = SessionLocal()
    buscar_o_crear_conversacion(db, "whatsapp", TELEFONO_DE_PRUEBA)
    db.close()

    barrera = threading.Barrier(CANTIDAD_HILOS)

    def entrega_concurrente():
        barrera.wait()
        procesar_mensaje_entrante(TELEFONO_DE_PRUEBA, "wamid.concurrente", "hola, esto llega varias veces")

    hilos = [threading.Thread(target=entrega_concurrente) for _ in range(CANTIDAD_HILOS)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    db = SessionLocal()
    guardados = db.query(Mensaje).filter_by(wa_message_id="wamid.concurrente").all()
    db.close()

    assert len(guardados) == 1
    # Solo la entrega que ganó la carrera llegó a responder.
    assert len(kapso_enviados) == 1
