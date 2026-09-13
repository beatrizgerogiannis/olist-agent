"""Tools de SQL controlado usadas pelo agente (ver docs/adrs/0002-camada-de-dados.md)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from data_agent.db import get_connection
from data_agent.schemas import ToolQueryResult
from data_agent.tools.guardrails import DEFAULT_QUERY_TIMEOUT_SECONDS, TABLE_ORDER, execute_guarded

# Descrições curadas a partir de docs/data_dictionary.md — mantenha em sincronia
# se o dicionário de dados mudar. As colunas e tipos vêm direto do DuckDB, para
# nunca ficarem defasados em relação ao schema real carregado.
_TABLE_DESCRIPTIONS: dict[str, str] = {
    "customers": (
        "1 linha por customer_id (gerado por pedido, não por pessoa). "
        "Para contar clientes únicos, use customer_unique_id."
    ),
    "sellers": "1 linha por vendedor.",
    "products": (
        "1 linha por produto. product_category_name pode ser NULL "
        "(~610 produtos sem categoria no dataset completo)."
    ),
    "geolocation": (
        "Amostra de coordenadas por prefixo de CEP (geolocation_zip_code_prefix), sem chave "
        "primária e sem relação 1:1 com customers/sellers — não usar para localizar um "
        "cliente/vendedor específico."
    ),
    "orders": (
        "1 linha por pedido. Datas de entrega podem ser NULL; para prazo médio de entrega, "
        "filtrar order_status = 'delivered' e order_delivered_customer_date IS NOT NULL."
    ),
    "order_items": (
        "1 linha por item de pedido; um pedido pode ter múltiplos itens e vendedores. "
        "'Total de vendas' é a soma de price (frete em freight_value, separadamente)."
    ),
    "order_payments": (
        "1 linha por transação/parcela de pagamento; um pedido pode ter mais de uma "
        "(payment_sequential > 1)."
    ),
    "order_reviews": (
        "1 linha por avaliação. review_id sozinho não é único — a chave real é "
        "(review_id, order_id)."
    ),
}


class ColumnInfo(BaseModel):
    """Uma coluna de uma tabela do warehouse."""

    name: str
    type: str


class TableInfo(BaseModel):
    """Descrição de uma tabela disponível para consulta."""

    name: str
    description: str
    columns: list[ColumnInfo]


def get_schema(*, db_path: str | Path | None = None) -> list[TableInfo]:
    """Descreve as tabelas/colunas disponíveis para consulta via ``query_sales``."""
    con = get_connection(db_path)
    try:
        tables: list[TableInfo] = []
        for table_name in TABLE_ORDER:
            rows = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [table_name],
            ).fetchall()
            columns = [ColumnInfo(name=col_name, type=col_type) for col_name, col_type in rows]
            tables.append(
                TableInfo(
                    name=table_name,
                    description=_TABLE_DESCRIPTIONS[table_name],
                    columns=columns,
                )
            )
        return tables
    finally:
        con.close()


def query_sales(
    sql: str,
    *,
    db_path: str | Path | None = None,
    timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
) -> ToolQueryResult:
    """Executa ``sql`` (somente leitura, validada por tools/guardrails.py) no warehouse."""
    con = get_connection(db_path)
    try:
        safe_sql = execute_guarded(con, sql, timeout_seconds=timeout_seconds)
        assert con.description is not None
        columns = [description[0] for description in con.description]
        rows = [dict(zip(columns, row, strict=True)) for row in con.fetchall()]
        return ToolQueryResult(sql=safe_sql, columns=columns, rows=rows, row_count=len(rows))
    finally:
        con.close()
