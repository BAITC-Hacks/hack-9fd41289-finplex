"""Конфигурация. Секреты — из окружения, не в коде."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Order Planner API"
    app_version: str = "0.1.0"
    cors_origins: str = "http://localhost:3100,http://127.0.0.1:3100"

    max_upload_bytes: int = 30 * 1024 * 1024  # 30 MB (Excel-выгрузки)

    # Параметры расчёта (можно менять на фронте)
    lead_time_months: float = 1.5
    safety_months: float = 0.5

    # LLM (опционально; без ключа — template-fallback)
    llm_enabled: bool = True
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    llm_max_output_tokens: int = 600
    llm_temperature: float = 0.2

    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
