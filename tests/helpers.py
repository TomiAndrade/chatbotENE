"""Helpers para armar y firmar payloads de webhook como los manda la Cloud
API de Meta, y para fijar el reloj que ve el código que arma el aviso de
escalamiento."""

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


def firmar_meta(cuerpo: bytes, secreto: str) -> str:
    """Firma en el formato exacto del header X-Hub-Signature-256 de Meta:
    con el prefijo "sha256=", no solo el hexdigest."""
    return "sha256=" + hmac.new(secreto.encode("utf-8"), cuerpo, hashlib.sha256).hexdigest()


def mensaje_meta_texto(wa_message_id: str, telefono: str, texto: str) -> dict:
    return {
        "from": telefono,
        "id": wa_message_id,
        "timestamp": "1735689600",
        "type": "text",
        "text": {"body": texto},
    }


def payload_meta_mensajes(mensajes: list[dict], phone_number_id: str = "000000000000") -> dict:
    """Un payload de webhook de Meta con uno o más `messages` en el mismo
    `entry`/`changes` (spec-meta-cloud-api.md, sección 2)."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba_de_prueba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"profile": {"name": "Test"}, "wa_id": m.get("from")} for m in mensajes],
                            "messages": mensajes,
                        },
                    }
                ],
            }
        ],
    }


def payload_meta_texto(wa_message_id: str, telefono: str, texto: str) -> dict:
    """El caso común de los tests: un solo mensaje de texto."""
    return payload_meta_mensajes([mensaje_meta_texto(wa_message_id, telefono, texto)])


def payload_meta_statuses(wa_message_id: str, telefono: str, status: str = "delivered") -> dict:
    """Payload de confirmación de entrega: `value` trae `statuses[]` en vez
    de `messages[]` (spec-meta-cloud-api.md, sección 2). Hay que descartarlo
    explícitamente, no procesar nada."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba_de_prueba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "000000000000"},
                            "statuses": [{"id": wa_message_id, "status": status, "recipient_id": telefono}],
                        },
                    }
                ],
            }
        ],
    }
