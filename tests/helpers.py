"""Helpers para armar y firmar payloads de webhook como los manda Kapso, y
para fijar el reloj que ve el código que arma el aviso de escalamiento."""

import hashlib
import hmac
from datetime import datetime

from app.mensajes import (
    MENSAJE_ESCALAMIENTO_EN_HORARIO,
    MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO,
)

# Los dos textos posibles del aviso de escalamiento. Sirve para afirmar que lo
# que salió es efectivamente el aviso, en los tests donde no importa cuál de
# los dos: comparar contra `mensajes.mensaje_escalamiento(...)` no serviría,
# porque si esa función devolviera cualquier cosa los dos lados del assert
# cambiarían igual y el test pasaría lo mismo.
AVISOS_DE_ESCALAMIENTO = frozenset(
    {MENSAJE_ESCALAMIENTO_EN_HORARIO, MENSAJE_ESCALAMIENTO_FUERA_DE_HORARIO}
)


class RelojFijo:
    """Reemplazo de `datetime` que siempre responde el mismo instante.

    Se usa con `monkeypatch.setattr(main_mod, "datetime", RelojFijo(...))`
    para poder afirmar cuál de los dos avisos de escalamiento corresponde sin
    depender de cuándo se corra la suite.

    Se le pasa un instante **aware**, y `now(tz)` lo convierte a la zona que
    le pidan. Esa conversión es la parte importante: si el código hiciera
    `datetime.now()` pelado en vez de `datetime.now(config.timezone)`, en un
    servidor en UTC leería una hora distinta a la del polo y elegiría el aviso
    equivocado.
    """

    def __init__(self, instante: datetime) -> None:
        assert instante.tzinfo is not None, "el instante tiene que ser aware"
        self._instante = instante

    def now(self, tz=None) -> datetime:
        if tz is None:
            # Un `datetime.now()` sin zona: devuelve la hora del servidor, sin
            # convertir. Es justamente el bug que estos tests tienen que
            # detectar, así que se lo deja fallar de forma realista.
            return self._instante.replace(tzinfo=None)
        return self._instante.astimezone(tz)


def firmar(cuerpo: bytes, secreto: str) -> str:
    return hmac.new(secreto.encode("utf-8"), cuerpo, hashlib.sha256).hexdigest()


def payload_mensaje_texto(wa_message_id: str, telefono: str, texto: str) -> dict:
    return {
        "message": {
            "id": wa_message_id,
            "from": telefono,
            "type": "text",
            "text": {"body": texto},
        },
        "conversation": {"phone_number": telefono},
    }
