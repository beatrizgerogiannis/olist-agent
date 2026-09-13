from pathlib import Path

import duckdb
import pytest
from load_data import TABLE_SPECS, load_all, load_table


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()

    (raw / "olist_customers_dataset.csv").write_text(
        "customer_id,customer_unique_id,customer_zip_code_prefix,customer_city,customer_state\n"
        "c1,u1,01037,sao paulo,SP\n"
        "c2,u2,14409,franca,SP\n"
    )
    (raw / "olist_sellers_dataset.csv").write_text(
        "seller_id,seller_zip_code_prefix,seller_city,seller_state\ns1,13023,campinas,SP\n"
    )
    (raw / "olist_products_dataset.csv").write_text(
        "product_id,product_category_name,product_name_lenght,product_description_lenght,"
        "product_photos_qty,product_weight_g,product_length_cm,product_height_cm,"
        "product_width_cm\np1,perfumaria,40,287,1,225,16,10,14\n"
    )
    (raw / "olist_geolocation_dataset.csv").write_text(
        "geolocation_zip_code_prefix,geolocation_lat,geolocation_lng,geolocation_city,"
        "geolocation_state\n01037,-23.5,-46.6,sao paulo,SP\n01037,-23.5,-46.6,sao paulo,SP\n"
    )
    (raw / "olist_orders_dataset.csv").write_text(
        "order_id,customer_id,order_status,order_purchase_timestamp,order_approved_at,"
        "order_delivered_carrier_date,order_delivered_customer_date,"
        "order_estimated_delivery_date\n"
        "o1,c1,delivered,2017-10-02 10:56:33,2017-10-02 11:07:15,2017-10-04 19:55:00,"
        "2017-10-10 21:25:13,2017-10-18 00:00:00\n"
    )
    (raw / "olist_order_items_dataset.csv").write_text(
        "order_id,order_item_id,product_id,seller_id,shipping_limit_date,price,freight_value\n"
        "o1,1,p1,s1,2017-09-19 09:45:35,58.90,13.29\n"
    )
    (raw / "olist_order_payments_dataset.csv").write_text(
        "order_id,payment_sequential,payment_type,payment_installments,payment_value\n"
        "o1,1,credit_card,8,99.33\n"
    )
    (raw / "olist_order_reviews_dataset.csv").write_text(
        "review_id,order_id,review_score,review_comment_title,review_comment_message,"
        "review_creation_date,review_answer_timestamp\n"
        "r1,o1,4,,,2018-01-18 00:00:00,2018-01-18 21:46:59\n"
    )
    return raw


def test_load_all_creates_all_tables_with_expected_row_counts(
    raw_dir: Path, tmp_path: Path
) -> None:
    db_path = tmp_path / "warehouse.duckdb"

    row_counts = load_all(raw_dir=raw_dir, db_path=db_path)

    assert row_counts == {
        "customers": 2,
        "sellers": 1,
        "products": 1,
        "geolocation": 2,
        "orders": 1,
        "order_items": 1,
        "order_payments": 1,
        "order_reviews": 1,
    }


def test_load_all_preserves_foreign_keys(raw_dir: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "warehouse.duckdb"
    load_all(raw_dir=raw_dir, db_path=db_path)

    con = duckdb.connect(str(db_path))
    try:
        joined = con.execute(
            """
            SELECT o.order_id, c.customer_state, oi.product_id, oi.seller_id
            FROM orders o
            JOIN customers c ON c.customer_id = o.customer_id
            JOIN order_items oi ON oi.order_id = o.order_id
            JOIN products p ON p.product_id = oi.product_id
            JOIN sellers s ON s.seller_id = oi.seller_id
            """
        ).fetchall()
    finally:
        con.close()

    assert joined == [("o1", "SP", "p1", "s1")]


def test_load_all_rejects_order_item_with_unknown_order(raw_dir: Path, tmp_path: Path) -> None:
    with (raw_dir / "olist_order_items_dataset.csv").open("a") as f:
        f.write("does-not-exist,1,p1,s1,2017-09-19 09:45:35,10.0,1.0\n")

    db_path = tmp_path / "warehouse.duckdb"

    with pytest.raises(duckdb.ConstraintException):
        load_all(raw_dir=raw_dir, db_path=db_path)


def test_load_all_zip_code_prefix_keeps_leading_zero(raw_dir: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "warehouse.duckdb"
    load_all(raw_dir=raw_dir, db_path=db_path)

    con = duckdb.connect(str(db_path))
    try:
        zip_prefix = con.execute(
            "SELECT customer_zip_code_prefix FROM customers WHERE customer_id = 'c1'"
        ).fetchone()
    finally:
        con.close()

    assert zip_prefix == ("01037",)


def test_load_table_raises_when_csv_missing(tmp_path: Path) -> None:
    empty_raw_dir = tmp_path / "empty"
    empty_raw_dir.mkdir()
    con = duckdb.connect(":memory:")
    spec = next(s for s in TABLE_SPECS if s.name == "customers")

    try:
        with pytest.raises(FileNotFoundError):
            load_table(con, empty_raw_dir, spec)
    finally:
        con.close()
