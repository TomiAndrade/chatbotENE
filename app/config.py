"""Carga de variables de entorno desde .env."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    kapso_api_key: str
    kapso_phone_number_id: str
    kapso_webhook_secret: str
    meta_phone_number_id: str
    meta_access_token: str
    meta_app_secret: str
    meta_verify_token: str
    meta_api_version: str
    database_url: str
    proveedor_ia: str
    debug: bool
    modelo: str
    anthropic_api_key: str
    base_url: str
    openai_compat_api_key: str
    timezone: ZoneInfo
    historial_max_mensajes: int
    historial_dias_validez: int
    limite_mensajes_hora: int
    pausa_humana_minutos: int
    escalamiento_habilitado: bool


def _cargar_config() -> Config:
    return Config(
        kapso_api_key=os.getenv("KAPSO_API_KEY", ""),
        kapso_phone_number_id=os.getenv("KAPSO_PHONE_NUMBER_ID", ""),
        kapso_webhook_secret=os.getenv("KAPSO_WEBHOOK_SECRET", ""),
        meta_phone_number_id=os.getenv("META_PHONE_NUMBER_ID", ""),
        meta_access_token=os.getenv("META_ACCESS_TOKEN", ""),
        meta_app_secret=os.getenv("META_APP_SECRET", ""),
        meta_verify_token=os.getenv("META_VERIFY_TOKEN", ""),
        meta_api_version=os.getenv("META_API_VERSION", "v23.0"),
        # Sin default: ver spec-validacion-config-arranque.md. Que falten
        # cae en un string vacío, que validar_config() trata como ausente
        # (nunca en SQLite ni en "fijo" silenciosos).
        database_url=os.getenv("DATABASE_URL", ""),
        proveedor_ia=os.getenv("PROVEEDOR_IA", ""),
        debug=os.getenv("DEBUG", "false").lower() == "true",
        modelo=os.getenv("MODELO", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        base_url=os.getenv("BASE_URL", ""),
        openai_compat_api_key=os.getenv("OPENAI_COMPAT_API_KEY", ""),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")),
        historial_max_mensajes=int(os.getenv("HISTORIAL_MAX_MENSAJES", "20")),
        historial_dias_validez=int(os.getenv("HISTORIAL_DIAS_VALIDEZ", "7")),
        limite_mensajes_hora=int(os.getenv("LIMITE_MENSAJES_HORA", "30")),
        # 120, no los 30 que sugiere Kapso: con 30 el bot puede despertarse y
        # escribir encima de una conversación humana en curso (ver
        # spec-pausa-por-intervencion-humana.md, sección 5). A validar con
        # uso real.
        pausa_humana_minutos=int(os.getenv("PAUSA_HUMANA_MINUTOS", "120")),
        # Sin bandeja de entrada no hay quién reciba un escalamiento (ver
        # specs/spec-derivacion.md): apagado hasta que exista una. La
        # herramienta y modo_humano siguen implementados, solo dejan de
        # ofrecerse al modelo.
        escalamiento_habilitado=os.getenv("ESCALAMIENTO_HABILITADO", "false").lower() == "true",
    )


config = _cargar_config()
