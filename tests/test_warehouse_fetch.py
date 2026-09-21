from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from data_agent import warehouse_fetch
from data_agent.config import Settings
from data_agent.warehouse_fetch import WarehouseFetchError, ensure_warehouse, main

_ALL_ENV_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_REGION",
    "S3_BUCKET_NAME",
    "S3_OBJECT_KEY",
)


def _set_all_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test_access_key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test_secret_key")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_OBJECT_KEY", "warehouse.duckdb")


class _FakeS3Client:
    """Substituto de um client S3 do boto3 — só o método usado por ``ensure_warehouse``."""

    def __init__(
        self, *, content: bytes | None = b"fake duckdb bytes", error: Exception | None = None
    ):
        self._content = content
        self._error = error
        self.calls: list[tuple[str, str, str]] = []

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        self.calls.append((bucket, key, filename))
        if self._content is not None:
            Path(filename).write_bytes(self._content)
        if self._error is not None:
            raise self._error


def _fake_boto3(client: _FakeS3Client) -> Any:
    return SimpleNamespace(client=lambda service_name, **kwargs: client)


def test_ensure_warehouse_skips_download_when_file_already_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Caso do volume montado em docker-compose.yml local — o arquivo já existe,
    # então nenhuma variável AWS_*/S3_* deveria ser exigida.
    db_path = tmp_path / "warehouse.duckdb"
    db_path.write_bytes(b"already here")
    fake_client = _FakeS3Client()
    monkeypatch.setattr(warehouse_fetch, "boto3", _fake_boto3(fake_client))

    ensure_warehouse(db_path)

    assert db_path.read_bytes() == b"already here"
    assert fake_client.calls == []


def test_ensure_warehouse_raises_when_env_vars_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "warehouse.duckdb"
    for name in _ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(WarehouseFetchError) as exc_info:
        ensure_warehouse(db_path)

    for name in _ALL_ENV_VARS:
        assert name in str(exc_info.value)
    assert not db_path.exists()


def test_ensure_warehouse_downloads_and_atomically_replaces_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    db_path = tmp_path / "nested" / "warehouse.duckdb"
    fake_client = _FakeS3Client(content=b"real warehouse bytes")
    monkeypatch.setattr(warehouse_fetch, "boto3", _fake_boto3(fake_client))

    ensure_warehouse(db_path)

    assert db_path.read_bytes() == b"real warehouse bytes"
    assert not db_path.with_name(db_path.name + ".part").exists()
    assert fake_client.calls == [("test-bucket", "warehouse.duckdb", str(db_path) + ".part")]


def test_ensure_warehouse_cleans_up_partial_file_on_download_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    db_path = tmp_path / "warehouse.duckdb"
    error = ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "GetObject")
    fake_client = _FakeS3Client(content=b"partial bytes", error=error)
    monkeypatch.setattr(warehouse_fetch, "boto3", _fake_boto3(fake_client))

    with pytest.raises(WarehouseFetchError) as exc_info:
        ensure_warehouse(db_path)

    assert "test-bucket" in str(exc_info.value)
    assert not db_path.exists()
    assert not db_path.with_name(db_path.name + ".part").exists()


def test_main_exits_nonzero_when_fetch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    missing_db_path = tmp_path / "does-not-exist" / "warehouse.duckdb"
    fake_settings = Settings(_env_file=None, duckdb_path=missing_db_path)
    monkeypatch.setattr(warehouse_fetch, "get_settings", lambda: fake_settings)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1


def test_main_succeeds_without_exit_when_file_already_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "warehouse.duckdb"
    db_path.write_bytes(b"already here")
    monkeypatch.setattr(
        warehouse_fetch, "get_settings", lambda: Settings(_env_file=None, duckdb_path=db_path)
    )

    main()  # não deve levantar SystemExit
