from pathlib import Path

import pytest

from data_agent.config import Settings, get_settings
from data_agent.db import get_connection


def test_settings_reads_duckdb_path_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUCKDB_PATH", "/tmp/custom-warehouse.duckdb")

    settings = Settings()

    assert settings.duckdb_path == Path("/tmp/custom-warehouse.duckdb")


def test_settings_default_duckdb_path_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DUCKDB_PATH", raising=False)

    # `_env_file=None` isola o teste do `.env` real do repositório (que já
    # define DUCKDB_PATH=data/warehouse.duckdb, o mesmo valor do default) —
    # sem isso, o teste passaria mesmo que o default do campo estivesse
    # errado, mascarado pelo valor do .env.
    settings = Settings(_env_file=None)

    assert settings.duckdb_path == Path("data/warehouse.duckdb")


def test_get_settings_returns_settings_instance() -> None:
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert isinstance(settings, Settings)
    finally:
        get_settings.cache_clear()


def test_get_settings_caches_across_calls() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


def test_get_connection_raises_when_warehouse_file_missing(tmp_path: Path) -> None:
    missing_db_path = tmp_path / "does-not-exist.duckdb"

    with pytest.raises(FileNotFoundError):
        get_connection(missing_db_path)
