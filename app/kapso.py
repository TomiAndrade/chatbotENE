"""Cliente de la API de Kapso (https://docs.kapso.ai) y verificación de webhooks.

Referencias consultadas:
- Enviar mensaje: docs.kapso.ai/api/meta/whatsapp/messages/send-a-message
- Firma de webhooks: docs.kapso.ai/docs/platform/webhooks/security
"""

import hashlib
import hmac
import logging
import time

import httpx

from app.config import config

logger = logging.getLogger("kapso")

BASE_URL = "https://api.kapso.ai/meta/whatsapp/v24.0"
MAX_REINTENTOS = 3
ESPERA_INICIAL_SEGUNDOS = 1.0
WHATSAPP_MAX_CARACTERES = 4096


def _conviene_reintentar(error: httpx.HTTPError) -> bool:
    """Reintentar solo lo que puede salir distinto la próxima vez.

    - Errores de red (timeout, DNS, conexión cortada): sí, son transitorios.
    - 429 (rate limit) y 5xx (problema del lado de Kapso): sí.
    - El resto de los 4xx: no. Una API key inválida o un payload mal armado
      van a fallar igual las tres veces; reintentar solo agrega demora.
    """
    if isinstance(error, httpx.RequestError):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        codigo = error.response.status_code
        return codigo == 429 or codigo >= 500
    return False


class KapsoClient:
    """Encapsula las llamadas HTTP a Kapso. Por ahora, solo enviar texto."""

    def __init__(self) -> None:
        self._http = httpx.Client(timeout=10.0)

    def cerrar(self) -> None:
        self._http.close()

    def enviar_mensaje_texto(self, telefono: str, texto: str) -> dict:
        if len(texto) > WHATSAPP_MAX_CARACTERES:
            logger.warning(
                "Mensaje de %s caracteres supera el límite de WhatsApp (%s), se trunca",
                len(texto), WHATSAPP_MAX_CARACTERES,
            )
            texto = texto[:WHATSAPP_MAX_CARACTERES]

        url = f"{BASE_URL}/{config.kapso_phone_number_id}/messages"
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": config.kapso_api_key,
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": "text",
            "text": {"body": texto},
        }

        ultimo_error: Exception | None = None
        for intento in range(1, MAX_REINTENTOS + 1):
            if config.debug:
                logger.info("Kapso request -> POST %s | payload=%s", url, payload)
            try:
                respuesta = self._http.post(url, json=payload, headers=headers)
                respuesta.raise_for_status()
                if config.debug:
                    logger.info("Kapso response <- %s", respuesta.text)
                return respuesta.json()
            except httpx.HTTPError as error:
                ultimo_error = error

                if not _conviene_reintentar(error):
                    detalle = ""
                    if isinstance(error, httpx.HTTPStatusError):
                        detalle = f" | respuesta: {error.response.text}"
                    logger.error("Kapso rechazó el mensaje, no se reintenta: %s%s", error, detalle)
                    raise

                logger.warning(
                    "Error al enviar mensaje a Kapso (intento %s/%s): %s",
                    intento, MAX_REINTENTOS, error,
                )
                if intento < MAX_REINTENTOS:
                    time.sleep(ESPERA_INICIAL_SEGUNDOS * (2 ** (intento - 1)))

        raise ultimo_error


def verificar_firma_webhook(cuerpo_crudo: bytes, firma_recibida: str | None) -> bool:
    """Valida el header X-Webhook-Signature: HMAC-SHA256 del body crudo con el
    secreto configurado en el dashboard de Kapso, como hex plano (sin prefijo).

    Sin secreto configurado el webhook queda abierto: cualquiera que conozca la
    URL puede inyectar mensajes falsos. Por eso solo se deja pasar con
    DEBUG=true, para poder levantar el proyecto antes de tener el secreto del
    sandbox a mano. Con DEBUG=false se rechaza todo.
    """
    if not config.kapso_webhook_secret:
        if not config.debug:
            logger.error(
                "KAPSO_WEBHOOK_SECRET vacío y DEBUG=false: se rechaza el webhook. "
                "Configurá el secreto del dashboard de Kapso en .env."
            )
            return False
        logger.warning(
            "KAPSO_WEBHOOK_SECRET vacío: no se está verificando la firma del webhook "
            "(permitido solo porque DEBUG=true)"
        )
        return True

    if not firma_recibida:
        return False

    firma_esperada = hmac.new(
        config.kapso_webhook_secret.encode("utf-8"),
        cuerpo_crudo,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(firma_esperada, firma_recibida)
