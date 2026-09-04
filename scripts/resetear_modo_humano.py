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

from app.db import SessionLocal
from app.models import CANAL_WHATSAPP, Conversacion


def resetear_modo_humano(identificador_externo: str, canal: str = CANAL_WHATSAPP) -> bool:
    """Devuelve True si encontró y reseteó la conversación, False si no existe."""
    db = SessionLocal()
    try:
        conversacion = (
            db.query(Conversacion).filter_by(canal=canal, identificador_externo=identificador_externo).first()
        )
        if conversacion is None:
            return False

        conversacion.modo_humano = False
        conversacion.motivo_pausa = None
        conversacion.modo_humano_desde = None
        conversacion.resumen_escalamiento = None
        conversacion.escalada_en = None
        db.commit()
        return True
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Uso: python {sys.argv[0]} <telefono>")
        sys.exit(1)

    telefono_arg = sys.argv[1]
    if resetear_modo_humano(telefono_arg):
        print(f"modo_humano desmarcado para {telefono_arg}")
    else:
        print(f"No existe ninguna conversación para {telefono_arg}")
        sys.exit(1)
