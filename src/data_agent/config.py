"""Configuração da aplicação, lida de variáveis de ambiente (ver AGENTS.md)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Variáveis de ambiente consumidas pelo agente (ver .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    duckdb_path: Path = Path("data/warehouse.duckdb")
    openai_api_key: str = ""
    openai_timeout_seconds: float = 30.0
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
