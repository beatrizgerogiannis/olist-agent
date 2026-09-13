from pathlib import Path

import duckdb
import pytest

from data_agent.db import get_connection


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (1)")
    finally:
        con.close()
    return path


def test_get_connection_allows_reads(db_path: Path) -> None:
    con = get_connection(db_path)
    try:
        assert con.execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        con.close()


def test_get_connection_rejects_write_at_db_layer(db_path: Path) -> None:
    """Regressão: get_connection precisa devolver uma conexão que já bloqueia
    escritas na própria camada do DuckDB (``read_only=True``), como segunda
    linha de defesa independente do guard-rail de ``tools/guardrails.py`` (ver
    docs/adrs/0002-camada-de-dados.md e
    docs/adrs/0003-sql-controlado-vs-tools-granulares.md). Chama
    ``con.execute`` diretamente, sem passar por ``query_sales``/
    ``execute_guarded``, para provar que essa camada funciona isoladamente —
    se o guard-rail de SQL um dia regredir e deixar passar uma escrita, esta
    camada ainda precisa bloqueá-la.
    """
    con = get_connection(db_path)
    try:
        with pytest.raises(duckdb.Error):
            con.execute("INSERT INTO t VALUES (2)")
        with pytest.raises(duckdb.Error):
            con.execute("CREATE TABLE other (y INTEGER)")
        with pytest.raises(duckdb.Error):
            con.execute("DROP TABLE t")
    finally:
        con.close()
