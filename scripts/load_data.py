"""Carrega os CSVs brutos do Olist Brazilian E-Commerce Dataset num DuckDB local.

Le os 8 arquivos CSV esperados em ``data/raw/olist/`` e recria, em ordem que
respeita as dependencias de chave estrangeira, as tabelas correspondentes em
``data/warehouse.duckdb``. O script e idempotente: pode ser executado varias
vezes, sempre recriando as tabelas do zero a partir dos CSVs.

Uso:
    uv run python scripts/load_data.py
    uv run python scripts/load_data.py --raw-dir data/raw/olist --db-path data/warehouse.duckdb
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import duckdb
import structlog

logger = structlog.get_logger(__name__)

DEFAULT_RAW_DIR = Path("data/raw/olist")
DEFAULT_DB_PATH = Path("data/warehouse.duckdb")


@dataclass(frozen=True)
class TableSpec:
    """Descreve como uma tabela do warehouse e carregada a partir de um CSV."""

    name: str
    csv_filename: str
    ddl: str
    csv_columns: dict[str, str]


# Ordem de carga: tabelas sem dependencias primeiro, depois as que referenciam
# via FOREIGN KEY as anteriores. A ordem de DROP (em load_all) e a reversa.
TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        name="customers",
        csv_filename="olist_customers_dataset.csv",
        ddl="""
            CREATE TABLE customers (
                customer_id VARCHAR PRIMARY KEY,
                customer_unique_id VARCHAR,
                customer_zip_code_prefix VARCHAR,
                customer_city VARCHAR,
                customer_state VARCHAR
            )
        """,
        csv_columns={
            "customer_id": "VARCHAR",
            "customer_unique_id": "VARCHAR",
            "customer_zip_code_prefix": "VARCHAR",
            "customer_city": "VARCHAR",
            "customer_state": "VARCHAR",
        },
    ),
    TableSpec(
        name="sellers",
        csv_filename="olist_sellers_dataset.csv",
        ddl="""
            CREATE TABLE sellers (
                seller_id VARCHAR PRIMARY KEY,
                seller_zip_code_prefix VARCHAR,
                seller_city VARCHAR,
                seller_state VARCHAR
            )
        """,
        csv_columns={
            "seller_id": "VARCHAR",
            "seller_zip_code_prefix": "VARCHAR",
            "seller_city": "VARCHAR",
            "seller_state": "VARCHAR",
        },
    ),
    TableSpec(
        name="products",
        csv_filename="olist_products_dataset.csv",
        ddl="""
            CREATE TABLE products (
                product_id VARCHAR PRIMARY KEY,
                product_category_name VARCHAR,
                product_name_lenght INTEGER,
                product_description_lenght INTEGER,
                product_photos_qty INTEGER,
                product_weight_g INTEGER,
                product_length_cm INTEGER,
                product_height_cm INTEGER,
                product_width_cm INTEGER
            )
        """,
        csv_columns={
            "product_id": "VARCHAR",
            "product_category_name": "VARCHAR",
            "product_name_lenght": "INTEGER",
            "product_description_lenght": "INTEGER",
            "product_photos_qty": "INTEGER",
            "product_weight_g": "INTEGER",
            "product_length_cm": "INTEGER",
            "product_height_cm": "INTEGER",
            "product_width_cm": "INTEGER",
        },
    ),
    TableSpec(
        # Sem PRIMARY KEY: a granularidade real e uma amostra de coordenadas
        # por prefixo de CEP, com muitas combinacoes duplicadas (ver
        # docs/data_dictionary.md).
        name="geolocation",
        csv_filename="olist_geolocation_dataset.csv",
        ddl="""
            CREATE TABLE geolocation (
                geolocation_zip_code_prefix VARCHAR,
                geolocation_lat DOUBLE,
                geolocation_lng DOUBLE,
                geolocation_city VARCHAR,
                geolocation_state VARCHAR
            )
        """,
        csv_columns={
            "geolocation_zip_code_prefix": "VARCHAR",
            "geolocation_lat": "DOUBLE",
            "geolocation_lng": "DOUBLE",
            "geolocation_city": "VARCHAR",
            "geolocation_state": "VARCHAR",
        },
    ),
    TableSpec(
        name="orders",
        csv_filename="olist_orders_dataset.csv",
        ddl="""
            CREATE TABLE orders (
                order_id VARCHAR PRIMARY KEY,
                customer_id VARCHAR NOT NULL REFERENCES customers (customer_id),
                order_status VARCHAR,
                order_purchase_timestamp TIMESTAMP,
                order_approved_at TIMESTAMP,
                order_delivered_carrier_date TIMESTAMP,
                order_delivered_customer_date TIMESTAMP,
                order_estimated_delivery_date TIMESTAMP
            )
        """,
        csv_columns={
            "order_id": "VARCHAR",
            "customer_id": "VARCHAR",
            "order_status": "VARCHAR",
            "order_purchase_timestamp": "TIMESTAMP",
            "order_approved_at": "TIMESTAMP",
            "order_delivered_carrier_date": "TIMESTAMP",
            "order_delivered_customer_date": "TIMESTAMP",
            "order_estimated_delivery_date": "TIMESTAMP",
        },
    ),
    TableSpec(
        name="order_items",
        csv_filename="olist_order_items_dataset.csv",
        ddl="""
            CREATE TABLE order_items (
                order_id VARCHAR NOT NULL REFERENCES orders (order_id),
                order_item_id INTEGER NOT NULL,
                product_id VARCHAR NOT NULL REFERENCES products (product_id),
                seller_id VARCHAR NOT NULL REFERENCES sellers (seller_id),
                shipping_limit_date TIMESTAMP,
                price DOUBLE,
                freight_value DOUBLE,
                PRIMARY KEY (order_id, order_item_id)
            )
        """,
        csv_columns={
            "order_id": "VARCHAR",
            "order_item_id": "INTEGER",
            "product_id": "VARCHAR",
            "seller_id": "VARCHAR",
            "shipping_limit_date": "TIMESTAMP",
            "price": "DOUBLE",
            "freight_value": "DOUBLE",
        },
    ),
    TableSpec(
        name="order_payments",
        csv_filename="olist_order_payments_dataset.csv",
        ddl="""
            CREATE TABLE order_payments (
                order_id VARCHAR NOT NULL REFERENCES orders (order_id),
                payment_sequential INTEGER NOT NULL,
                payment_type VARCHAR,
                payment_installments INTEGER,
                payment_value DOUBLE,
                PRIMARY KEY (order_id, payment_sequential)
            )
        """,
        csv_columns={
            "order_id": "VARCHAR",
            "payment_sequential": "INTEGER",
            "payment_type": "VARCHAR",
            "payment_installments": "INTEGER",
            "payment_value": "DOUBLE",
        },
    ),
    TableSpec(
        # PRIMARY KEY composta: review_id sozinho nao e unico no dataset real
        # (algumas revisoes reaproveitam o mesmo review_id em pedidos
        # diferentes). Ver docs/data_dictionary.md.
        name="order_reviews",
        csv_filename="olist_order_reviews_dataset.csv",
        ddl="""
            CREATE TABLE order_reviews (
                review_id VARCHAR NOT NULL,
                order_id VARCHAR NOT NULL REFERENCES orders (order_id),
                review_score INTEGER,
                review_comment_title VARCHAR,
                review_comment_message VARCHAR,
                review_creation_date TIMESTAMP,
                review_answer_timestamp TIMESTAMP,
                PRIMARY KEY (review_id, order_id)
            )
        """,
        csv_columns={
            "review_id": "VARCHAR",
            "order_id": "VARCHAR",
            "review_score": "INTEGER",
            "review_comment_title": "VARCHAR",
            "review_comment_message": "VARCHAR",
            "review_creation_date": "TIMESTAMP",
            "review_answer_timestamp": "TIMESTAMP",
        },
    ),
)


def load_table(con: duckdb.DuckDBPyConnection, raw_dir: Path, spec: TableSpec) -> int:
    """Recria uma tabela e a popula a partir do CSV correspondente.

    Retorna o numero de linhas carregadas.
    """
    csv_path = raw_dir / spec.csv_filename
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV nao encontrado para a tabela '{spec.name}': {csv_path}")

    con.execute(f"DROP TABLE IF EXISTS {spec.name}")
    con.execute(spec.ddl)
    con.execute(
        f"INSERT INTO {spec.name} SELECT * FROM read_csv(?, header = true, columns = ?)",
        [str(csv_path), spec.csv_columns],
    )
    row_count = con.execute(f"SELECT count(*) FROM {spec.name}").fetchone()
    assert row_count is not None
    return int(row_count[0])


def load_all(raw_dir: Path = DEFAULT_RAW_DIR, db_path: Path = DEFAULT_DB_PATH) -> dict[str, int]:
    """Carrega todas as tabelas do Olist no DuckDB em ``db_path``.

    Tabelas sao recriadas (DROP + CREATE) em ordem que respeita as
    FOREIGN KEY, para que o script seja idempotente.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    row_counts: dict[str, int] = {}
    try:
        # Remove na ordem reversa para nao violar dependencias de FK ao
        # recriar as tabelas.
        for spec in reversed(TABLE_SPECS):
            con.execute(f"DROP TABLE IF EXISTS {spec.name}")

        for spec in TABLE_SPECS:
            row_counts[spec.name] = load_table(con, raw_dir, spec)
            logger.info("table_loaded", table=spec.name, rows=row_counts[spec.name])
    finally:
        con.close()
    return row_counts


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Diretorio com os CSVs brutos do Olist (default: data/raw/olist)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Caminho do arquivo DuckDB de destino (default: data/warehouse.duckdb)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    row_counts = load_all(raw_dir=args.raw_dir, db_path=args.db_path)
    total = sum(row_counts.values())
    logger.info("load_complete", db_path=str(args.db_path), total_rows=total, **row_counts)


if __name__ == "__main__":
    main()
