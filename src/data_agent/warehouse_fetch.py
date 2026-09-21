"""Garante que ``data/warehouse.duckdb`` exista antes do uvicorn subir.

Chamado uma única vez pelo ``CMD`` do ``Dockerfile``, no boot do container — nunca durante um
request (ver docs/adrs/0010-hospedagem-do-demo-publico.md, seção "Atualização 2"). Se o
arquivo já existir no caminho de ``Settings.duckdb_path`` (caso do volume montado em
``docker-compose.yml`` local), não baixa nada. Caso contrário (deploy no Render, que não tem
disco persistente), baixa de um bucket S3 privado usando credenciais lidas de variáveis de
ambiente em runtime — nunca embutidas na imagem.

Uso: ``python -m data_agent.warehouse_fetch`` (via ``CMD`` do ``Dockerfile``, antes do
``uvicorn``). Sai com código de saída != 0 e loga o motivo se qualquer variável obrigatória
estiver ausente ou o download falhar — propositalmente não deixa o ``uvicorn`` subir sobre um
warehouse ausente/corrompido (ver ADR-0010).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError

from data_agent.config import get_settings

logger = structlog.get_logger(__name__)

# Nomes de variável de ambiente exigidos só por este módulo — não são lidos por
# data_agent.config.Settings (não são configuração da aplicação FastAPI, só deste passo de
# boot; ver .env.example).
_ENV_AWS_ACCESS_KEY_ID = "AWS_ACCESS_KEY_ID"
_ENV_AWS_SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"
_ENV_AWS_REGION = "AWS_REGION"
_ENV_S3_BUCKET_NAME = "S3_BUCKET_NAME"
_ENV_S3_OBJECT_KEY = "S3_OBJECT_KEY"

_REQUIRED_ENV_VARS = (
    _ENV_AWS_ACCESS_KEY_ID,
    _ENV_AWS_SECRET_ACCESS_KEY,
    _ENV_AWS_REGION,
    _ENV_S3_BUCKET_NAME,
    _ENV_S3_OBJECT_KEY,
)


class WarehouseFetchError(RuntimeError):
    """Falha fatal ao garantir o warehouse — deve interromper o boot do container."""


def _read_required_env() -> dict[str, str]:
    values = {name: os.environ.get(name, "") for name in _REQUIRED_ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise WarehouseFetchError(
            "Variáveis de ambiente ausentes para baixar o warehouse do S3: "
            f"{', '.join(missing)}. Ver .env.example para o que cada uma configura."
        )
    return values


def ensure_warehouse(duckdb_path: Path) -> None:
    """Garante que ``duckdb_path`` exista, baixando do S3 se necessário.

    Não sobrescreve um arquivo já presente (nem verifica sua integridade) — o caso comum de
    "já presente" é o volume montado em ``docker-compose.yml`` local, que deve sempre valer
    sobre o que estiver no bucket. Escreve para um arquivo temporário e só o promove ao
    caminho final (``Path.replace``, atômico) depois de um download completo, para nunca
    deixar um arquivo parcial/corrompido no caminho que ``data_agent.db`` vai abrir.
    """
    if duckdb_path.exists():
        logger.info("warehouse_fetch_skipped_already_present", path=str(duckdb_path))
        return

    env = _read_required_env()
    bucket = env[_ENV_S3_BUCKET_NAME]
    key = env[_ENV_S3_OBJECT_KEY]

    logger.info("warehouse_fetch_started", path=str(duckdb_path), bucket=bucket, key=key)

    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = duckdb_path.with_name(duckdb_path.name + ".part")

    client = boto3.client(
        "s3",
        aws_access_key_id=env[_ENV_AWS_ACCESS_KEY_ID],
        aws_secret_access_key=env[_ENV_AWS_SECRET_ACCESS_KEY],
        region_name=env[_ENV_AWS_REGION],
    )
    try:
        client.download_file(bucket, key, str(tmp_path))
    except (ClientError, BotoCoreError, OSError) as exc:
        tmp_path.unlink(missing_ok=True)
        raise WarehouseFetchError(
            f"Falha ao baixar s3://{bucket}/{key} para {duckdb_path}: {exc}"
        ) from exc

    tmp_path.replace(duckdb_path)
    logger.info(
        "warehouse_fetch_completed",
        path=str(duckdb_path),
        size_bytes=duckdb_path.stat().st_size,
    )


def main() -> None:
    try:
        ensure_warehouse(get_settings().duckdb_path)
    except WarehouseFetchError as exc:
        logger.error("warehouse_fetch_failed", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
