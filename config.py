"""
Central configuration loaded from environment variables / .env file.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql://tripenforce:tripenforce@localhost:5432/tripenforce"

    # Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-haiku-4-5-20251001"

    # Duffel
    duffel_api_key: str = ""
    duffel_api_base: str = "https://api.duffel.com"

    # Amadeus (second provider, decommissions July 17 2026) — https://developers.amadeus.com
    amadeus_api_key: str = ""
    amadeus_api_secret: str = ""

    # App
    debug: bool = False
    log_level: str = "INFO"
    app_title: str = "TripEnforce"
    app_version: str = "1.0.0"


settings = Settings()
