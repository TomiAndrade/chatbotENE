"""Helpers para armar y firmar payloads de webhook como los manda Kapso."""

import hashlib
import hmac


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
