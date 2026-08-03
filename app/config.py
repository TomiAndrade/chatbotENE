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


def _cargar_config() -> Config:
    return Config(
        kapso_api_key=os.getenv("KAPSO_API_KEY", ""),
        kapso_phone_number_id=os.getenv("KAPSO_PHONE_NUMBER_ID", ""),
        kapso_webhook_secret=os.getenv("KAPSO_WEBHOOK_SECRET", ""),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./bot.db"),
        proveedor_ia=os.getenv("PROVEEDOR_IA", "fijo"),
        debug=os.getenv("DEBUG", "false").lower() == "true",
        modelo=os.getenv("MODELO", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        base_url=os.getenv("BASE_URL", ""),
        openai_compat_api_key=os.getenv("OPENAI_COMPAT_API_KEY", ""),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")),
        historial_max_mensajes=int(os.getenv("HISTORIAL_MAX_MENSAJES", "20")),
        historial_dias_validez=int(os.getenv("HISTORIAL_DIAS_VALIDEZ", "7")),
        limite_mensajes_hora=int(os.getenv("LIMITE_MENSAJES_HORA", "30")),
    )


config = _cargar_config()
