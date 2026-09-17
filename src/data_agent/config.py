"""Configuração da aplicação, lida de variáveis de ambiente (ver AGENTS.md)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Variáveis de ambiente consumidas pelo agente (ver .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    duckdb_path: Path = Path("data/warehouse.duckdb")
    groq_api_key: str = ""
    groq_timeout_seconds: float = 30.0
    log_level: str = "INFO"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://us.cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
