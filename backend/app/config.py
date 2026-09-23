"""Configuration. Default exposure is local and external LLM calls are opt-in."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    app_name: str = "Order Planner API"
    app_version: str = "0.2.0"
    cors_origins: str = "http://localhost:3100,http://127.0.0.1:3100"
    api_key: str = ""
    database_path: str = str(Path(__file__).resolve().parents[1] / "runtime" / "planner.sqlite3")
    lead_time_months: float = 1.5
    safety_months: float = 0.5
    llm_enabled: bool = False
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 10
    llm_max_output_tokens: int = 400
    llm_temperature: float = 0.2

    def cors_origins_list(self):
        return [v.strip() for v in self.cors_origins.split(",") if v.strip()]


_settings = None


def get_settings():
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
