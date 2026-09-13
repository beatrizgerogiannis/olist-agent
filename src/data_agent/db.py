"""Conexão com o warehouse DuckDB (ver docs/adrs/0002-camada-de-dados.md)."""

from __future__ import annotations

from pathlib import Path

import duckdb

from data_agent.config import get_settings


def get_connection(db_path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """Abre uma conexão somente leitura com o warehouse DuckDB.

    O agente nunca escreve no warehouse; abrir a conexão em modo read-only
    faz qualquer tentativa de escrita falhar na própria camada do banco,
    complementando a validação em tools/guardrails.py.
    """
    path = Path(db_path) if db_path is not None else get_settings().duckdb_path
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo DuckDB não encontrado em: {path}")
    return duckdb.connect(str(path), read_only=True)
