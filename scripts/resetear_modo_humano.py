"""Desmarca modo_humano de una conversación por número de teléfono.

Entregable de spec-etapa2.md: hoy modo_humano se desmarca a mano en la base,
y hace falta seguido para poder probar los criterios de aceptación 5, 6 y 7
(el criterio 4 corta la conversación apenas escala, así que sin esto no se
puede seguir probando sobre la misma conversación).

Uso, desde chatbot-polo/:

    python scripts/resetear_modo_humano.py <telefono>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, crear_engine
from app.models import CANAL_WHATSAPP, Conversacion
from app.atencion import resolver_si_hay_abierta
from app.pausa import reactivar_bot


def resetear_modo_humano(identificador_externo: str, canal: str = CANAL_WHATSAPP) -> bool:
    """Devuelve True si encontró y reseteó la conversación, False si no existe.

    Los campos que se limpian los define `reactivar_bot` (app/pausa.py), que
    es lo mismo que usa el botón "Reactivar bot" del CRM: el script y el
    panel tienen que dejar la conversación en el mismo estado. Por la misma
    razón, si había una atención abierta, `resolver_si_hay_abierta` también
    avanza la marca de agrupado hasta el último mensaje entrante (ver
    `app.agrupamiento.avanzar_hasta_el_ultimo_entrante`): los mensajes que
    llegaron durante esa atención le pertenecen a ella, no vuelven al bot
    aunque quien la cierre sea la consola y no el panel.
    """
    db = SessionLocal()
    try:
        conversacion = (
            db.query(Conversacion).filter_by(canal=canal, identificador_externo=identificador_externo).first()
        )
        if conversacion is None:
            return False

        # Una atención abierta con el bot ya activo mentiría en el panel: se
        # cierra en el mismo commit, sin autor (no la resolvió nadie del CRM).
        resolver_si_hay_abierta(db, conversacion, usuario_id=None)
        reactivar_bot(conversacion)
        db.commit()
        return True
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Uso: python {sys.argv[0]} <telefono>")
        sys.exit(1)

    # app/db.py ya no crea el engine al importar (ver
    # spec-validacion-config-arranque.md): hay que pedirlo explícito antes
    # del primer SessionLocal().
    crear_engine()

    telefono_arg = sys.argv[1]
    if resetear_modo_humano(telefono_arg):
        print(f"modo_humano desmarcado para {telefono_arg}")
    else:
        print(f"No existe ninguna conversación para {telefono_arg}")
        sys.exit(1)
