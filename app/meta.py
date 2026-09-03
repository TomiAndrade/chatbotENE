"""Cliente de la Cloud API de Meta (https://developers.facebook.com/docs/whatsapp)
y verificación de webhooks.

Copiado de app/kapso.py y adaptado (ver specs/spec-meta-cloud-api.md, sección
1): la política de reintentos, el truncado a 4096 caracteres y el manejo de
errores son los mismos, están bien y aplican igual. Lo que cambia es la URL,
el header de autenticación, el header y formato de la firma, y que ahora hay
que responder al challenge GET de verificación.
"""

import hashlib
import hmac
import logging
import time

import httpx

from app.config import config

logger = logging.getLogger("meta")

MAX_REINTENTOS = 3
ESPERA_INICIAL_SEGUNDOS = 1.0
WHATSAPP_MAX_CARACTERES = 4096
PREFIJO_FIRMA = "sha256="


def _conviene_reintentar(error: httpx.HTTPError) -> bool:
    """Reintentar solo lo que puede salir distinto la próxima vez.

    - Errores de red (timeout, DNS, conexión cortada): sí, son transitorios.
    - 429 (rate limit) y 5xx (problema del lado de Meta): sí.
    - El resto de los 4xx: no. Un token inválido o un payload mal armado
      van a fallar igual las tres veces; reintentar solo agrega demora.
    """
    if isinstance(error, httpx.RequestError):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        codigo = error.response.status_code
        return codigo == 429 or codigo >= 500
    return False


class MetaClient:
    """Encapsula las llamadas HTTP a la Cloud API de Meta. Por ahora, solo
    enviar texto."""

    def __init__(self) -> None:
        self._http = httpx.Client(timeout=10.0)

    def cerrar(self) -> None:
        self._http.close()

    def enviar_mensaje_texto(self, telefono: str, texto: str) -> dict:
        """`telefono` va tal cual llega del webhook (`messages[].from`), sin
        normalizar — ver spec-meta-cloud-api.md sección 3. Para Argentina el
        "9" puede estar o no, y no siempre coincide con cómo la persona tiene
        guardado el número: reformatearlo rompe la búsqueda de conversación o
        el envío."""
        if len(texto) > WHATSAPP_MAX_CARACTERES:
            logger.warning(
                "Mensaje de %s caracteres supera el límite de WhatsApp (%s), se trunca",
                len(texto), WHATSAPP_MAX_CARACTERES,
            )
            texto = texto[:WHATSAPP_MAX_CARACTERES]

        url = f"https://graph.facebook.com/{config.meta_api_version}/{config.meta_phone_number_id}/messages"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.meta_access_token}",
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
                logger.info("Meta request -> POST %s | payload=%s", url, payload)
            try:
                respuesta = self._http.post(url, json=payload, headers=headers)
                respuesta.raise_for_status()
                if config.debug:
                    logger.info("Meta response <- %s", respuesta.text)
                return respuesta.json()
            except httpx.HTTPError as error:
                ultimo_error = error

                if not _conviene_reintentar(error):
                    detalle = ""
                    if isinstance(error, httpx.HTTPStatusError):
                        detalle = f" | respuesta: {error.response.text}"
                    logger.error("Meta rechazó el mensaje, no se reintenta: %s%s", error, detalle)
                    raise

                logger.warning(
                    "Error al enviar mensaje a Meta (intento %s/%s): %s",
                    intento, MAX_REINTENTOS, error,
                )
                if intento < MAX_REINTENTOS:
                    time.sleep(ESPERA_INICIAL_SEGUNDOS * (2 ** (intento - 1)))

        raise ultimo_error


def verificar_firma_webhook(cuerpo_crudo: bytes, firma_recibida: str | None) -> bool:
    """Valida el header X-Hub-Signature-256: HMAC-SHA256 del body crudo con el
    App Secret de la app de Meta, con el valor recibido en formato
    "sha256=<hex>".

    Sin secreto configurado el webhook queda abierto: cualquiera que conozca
    la URL puede inyectar mensajes falsos. Por eso solo se deja pasar con
    DEBUG=true; con DEBUG=false se rechaza todo (mismo criterio que
    app.kapso.verificar_firma_webhook).
    """
    if not config.meta_app_secret:
        if not config.debug:
            logger.error(
                "META_APP_SECRET vacío y DEBUG=false: se rechaza el webhook. "
                "Configurá el App Secret de la app de Meta en .env."
            )
            return False
        logger.warning(
            "META_APP_SECRET vacío: no se está verificando la firma del webhook "
            "(permitido solo porque DEBUG=true)"
        )
        return True

    if not firma_recibida or not firma_recibida.startswith(PREFIJO_FIRMA):
        return False

    firma_recibida = firma_recibida[len(PREFIJO_FIRMA):]

    firma_esperada = hmac.new(
        config.meta_app_secret.encode("utf-8"),
        cuerpo_crudo,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(firma_esperada, firma_recibida)


def verificar_challenge(hub_mode: str | None, hub_verify_token: str | None) -> bool:
    """True si el GET de verificación de Meta trae el modo y el verify token
    esperados. El llamador es responsable de devolver `hub.challenge` como
    texto plano si esto da True, o 403 si da False."""
    return hub_mode == "subscribe" and hub_verify_token == config.meta_verify_token
