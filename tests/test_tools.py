from pathlib import Path

import duckdb
import pytest
from load_data import load_all

from data_agent.schemas import ToolQueryResult
from data_agent.tools.guardrails import DEFAULT_ROW_LIMIT, QueryTimeoutError, SqlGuardrailError
from data_agent.tools.sql_tools import get_schema, query_sales

# Número de linhas de order_items no fixture `raw_dir` abaixo — usado para
# comparar contra `row_count` de forma independente de como `query_sales`
# calcula esse campo internamente (ver docs/adrs/0003, seção de testes).
ORDER_ITEMS_ROW_COUNT = 2


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
        "geolocation_state\n01037,-23.5,-46.6,sao paulo,SP\n"
    )
    (raw / "olist_orders_dataset.csv").write_text(
        "order_id,customer_id,order_status,order_purchase_timestamp,order_approved_at,"
        "order_delivered_carrier_date,order_delivered_customer_date,"
        "order_estimated_delivery_date\n"
        "o1,c1,delivered,2017-10-02 10:56:33,2017-10-02 11:07:15,2017-10-04 19:55:00,"
        "2017-10-10 21:25:13,2017-10-18 00:00:00\n"
        "o2,c2,delivered,2017-11-02 10:56:33,2017-11-02 11:07:15,2017-11-04 19:55:00,"
        "2017-11-10 21:25:13,2017-11-18 00:00:00\n"
    )
    (raw / "olist_order_items_dataset.csv").write_text(
        "order_id,order_item_id,product_id,seller_id,shipping_limit_date,price,freight_value\n"
        "o1,1,p1,s1,2017-09-19 09:45:35,58.90,13.29\n"
        "o2,1,p1,s1,2017-10-19 09:45:35,20.00,5.00\n"
    )
    (raw / "olist_order_payments_dataset.csv").write_text(
        "order_id,payment_sequential,payment_type,payment_installments,payment_value\n"
        "o1,1,credit_card,8,99.33\n"
        "o2,1,boleto,1,25.00\n"
    )
    (raw / "olist_order_reviews_dataset.csv").write_text(
        "review_id,order_id,review_score,review_comment_title,review_comment_message,"
        "review_creation_date,review_answer_timestamp\n"
        "r1,o1,4,,,2018-01-18 00:00:00,2018-01-18 21:46:59\n"
        "r2,o2,5,,,2018-02-18 00:00:00,2018-02-18 21:46:59\n"
    )
    return raw


@pytest.fixture
def db_path(raw_dir: Path, tmp_path: Path) -> Path:
    path = tmp_path / "warehouse.duckdb"
    load_all(raw_dir=raw_dir, db_path=path)
    return path


def test_query_sales_blocks_syntactically_invalid_sql(db_path: Path) -> None:
    with pytest.raises(SqlGuardrailError):
        query_sales("SELECT FROM WHERE (((", db_path=db_path)


def test_query_sales_valid_query_returns_data(db_path: Path) -> None:
    result = query_sales(
        "SELECT order_id, price FROM order_items ORDER BY order_id", db_path=db_path
    )

    assert isinstance(result, ToolQueryResult)
    assert result.columns == ["order_id", "price"]
    assert result.rows == [
        {"order_id": "o1", "price": 58.90},
        {"order_id": "o2", "price": 20.00},
    ]
    assert result.row_count == ORDER_ITEMS_ROW_COUNT


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE order_items",
        "DELETE FROM order_items",
        "UPDATE order_items SET price = 0",
        "INSERT INTO order_items VALUES ('x', 1, 'p1', 's1', NULL, 1.0, 1.0)",
        "SELECT * FROM order_items; DROP TABLE order_items",
        "SELECT * FROM order_items; SELECT * FROM customers",
        # Não são DDL/DML, mas eram cobertos pela antiga blocklist de
        # keywords e precisam continuar bloqueados agora que a validação de
        # "instrução única e somente-leitura" é feita pelo tipo do nó raiz
        # que o sqlglot devolve (allowlist), não por palavras-chave no texto.
        "ATTACH ':memory:' AS x",
        "PRAGMA table_info('customers')",
        "SET memory_limit='1GB'",
        "CALL some_proc()",
        "VACUUM",
        "GRANT ALL ON customers TO someone",
        "COPY customers TO '/tmp/exfil.csv'",
    ],
)
def test_query_sales_blocks_ddl_and_dml(db_path: Path, sql: str) -> None:
    with pytest.raises(SqlGuardrailError):
        query_sales(sql, db_path=db_path)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM secret_table",
        "SELECT * FROM order_items JOIN secret_table ON true",
        # Identificadores entre aspas duplas resolvem para a mesma tabela real
        # via o plano do DuckDB, mas não batem com um regex de identificador
        # "nu" — checagem de regressão para o bypass encontrado em revisão
        # (ver docs/adrs/0003-sql-controlado-vs-tools-granulares.md).
        'SELECT * FROM "information_schema"."tables"',
        "SELECT * FROM duckdb_tables()",
        'SELECT * FROM "duckdb_tables"()',
    ],
)
def test_query_sales_blocks_tables_outside_allowlist(db_path: Path, sql: str) -> None:
    with pytest.raises(SqlGuardrailError):
        query_sales(sql, db_path=db_path)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM outra_tabela",
        "WITH cte AS (SELECT id FROM outra_tabela) SELECT * FROM cte",
        "SELECT customer_id FROM customers UNION SELECT id FROM outra_tabela",
        "SELECT * FROM (SELECT * FROM outra_tabela) t",
    ],
)
def test_query_sales_blocks_real_table_outside_allowlist(db_path: Path, sql: str) -> None:
    """Regressão: as queries acima envolvem uma tabela que EXISTE de fato no
    warehouse (mas fora da allowlist do Olist), para garantir que o branch
    real de ``_check_allowed_tables`` (comparar o SEQ_SCAN do plano do DuckDB
    contra ``ALLOWED_TABLES``, em ``tools/guardrails.py``) é quem bloqueia a
    query — e não o `except duckdb.Error` genérico que dispara para uma
    tabela que sequer existe (caso de
    ``test_query_sales_blocks_tables_outside_allowlist`` acima, onde
    ``secret_table`` nunca é criada no warehouse e o DuckDB já rejeita a
    query no ``EXPLAIN`` com um erro de binder antes de qualquer comparação
    contra a allowlist). Sem uma tabela real fora da allowlist, esse branch
    específico (allowlist mismatch de uma fonte de dados que existe de
    verdade) fica sem cobertura de teste.
    """
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE TABLE outra_tabela (id INTEGER)")
        con.execute("INSERT INTO outra_tabela VALUES (1)")
    finally:
        con.close()

    with pytest.raises(SqlGuardrailError, match="Tabela\\(s\\) fora da allowlist"):
        query_sales(sql, db_path=db_path)


def test_query_sales_count_star_on_allowed_table_is_accepted(db_path: Path) -> None:
    """Regressão: falso positivo encontrado como achado colateral da validação
    manual do Dia 5 (observabilidade). ``COUNT(*)``/``COUNT(coluna)`` sem
    filtro sobre uma tabela inteira é reescrito pelo otimizador **físico** do
    DuckDB para um nó ``COLUMN_DATA_SCAN`` (lê só metadados de zonemap, não a
    coluna de verdade), que não carrega o nome da tabela em ``extra_info`` —
    sem a correção em ``_check_allowed_tables`` (ler o plano **lógico**,
    pré-otimização, em vez do físico — ver a docstring dessa função para os
    detalhes da investigação), isso era bloqueado como falso positivo:

        >>> from data_agent.tools.sql_tools import query_sales
        >>> query_sales("SELECT COUNT(*) AS c FROM sellers")
        SqlGuardrailError: Fonte de dados não permitida em SQL somente-leitura:
        ['COLUMN_DATA_SCAN'].

    reproduzido de verdade contra ``data/warehouse.duckdb`` (``sellers`` tem
    3095 linhas lá) antes desta correção, não só inferido lendo o código.
    """
    result = query_sales("SELECT COUNT(*) AS seller_count FROM sellers", db_path=db_path)

    assert result.rows == [{"seller_count": 1}]


@pytest.mark.parametrize(
    "sql,expected_rows",
    [
        ("SELECT COUNT(seller_id) AS c FROM sellers", [{"c": 1}]),
        (
            "SELECT seller_state, COUNT(*) AS c FROM sellers GROUP BY seller_state",
            [{"seller_state": "SP", "c": 1}],
        ),
        ("SELECT COUNT(*) AS c FROM sellers WHERE seller_state = 'SP'", [{"c": 1}]),
        (
            "SELECT COUNT(*) AS c FROM order_items oi JOIN orders o ON oi.order_id = o.order_id",
            [{"c": ORDER_ITEMS_ROW_COUNT}],
        ),
        ("SELECT SUM(price) AS s FROM order_items", [{"s": 78.90}]),
        ("SELECT AVG(price) AS a FROM order_items", [{"a": 39.45}]),
    ],
)
def test_query_sales_count_star_variants_are_accepted(
    db_path: Path, sql: str, expected_rows: list[dict[str, object]]
) -> None:
    """Regressão complementar ao teste acima: ``COUNT(*)``/``COUNT(coluna)``
    combinado com ``GROUP BY``/``WHERE``/``JOIN`` entre tabelas da allowlist,
    e outras agregações comuns (``SUM``/``AVG`` de uma coluna específica), não
    devem regredir com a correção. Nenhuma dessas variações passava pelo
    ``COLUMN_DATA_SCAN`` mesmo antes da correção (o otimizador só usa esse
    atalho para ``COUNT(*)``/``COUNT(coluna)`` sem filtro sobre uma tabela
    inteira — confirmado comparando o plano de cada uma delas durante a
    investigação, ver docstring de ``_check_allowed_tables``), mas a correção
    trocou o mecanismo de validação inteiro (plano lógico em vez de físico),
    então vale confirmar que esses formatos continuam resolvendo para o
    ``SEQ_SCAN`` esperado e não passam a ser bloqueados por engano.
    """
    result = query_sales(sql, db_path=db_path)

    assert result.rows == expected_rows


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) AS c FROM outra_tabela",
        "SELECT COUNT(id) AS c FROM outra_tabela",
        # Um predicado sempre-falso faz o otimizador físico do DuckDB
        # substituir a leitura inteira por um `EMPTY_RESULT` constante, sem
        # nome de tabela nenhum — um segundo bug (bypass real da allowlist,
        # mais sério que o falso positivo do COUNT(*) acima) encontrado
        # durante a mesma investigação: antes desta correção, `outra_tabela`
        # (fora da allowlist) NÃO era bloqueada quando a query tinha esse
        # filtro, porque `EMPTY_RESULT` já estava isento da validação
        # (pensado para casos legítimos como `SELECT 1`, que não lê tabela
        # nenhuma) — reproduzido contra um warehouse real antes desta
        # correção (ver docstring de `_check_allowed_tables`).
        "SELECT * FROM outra_tabela WHERE 1 = 0",
    ],
)
def test_query_sales_blocks_real_table_outside_allowlist_via_optimizer_rewrites(
    db_path: Path, sql: str
) -> None:
    """Garante que a correção do falso positivo de ``COUNT(*)`` (ler o plano
    lógico em vez do físico) não abriu uma brecha de segurança: uma tabela que
    existe de fato no warehouse, mas fora da allowlist do Olist, continua
    bloqueada mesmo nos dois formatos de query que o otimizador físico do
    DuckDB reescreve para um nó sem nome de tabela (``COLUMN_DATA_SCAN``/
    ``EMPTY_RESULT``) — os mesmos dois formatos que motivaram a correção.
    """
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE TABLE outra_tabela (id INTEGER)")
        con.execute("INSERT INTO outra_tabela VALUES (1)")
    finally:
        con.close()

    with pytest.raises(SqlGuardrailError, match="Tabela\\(s\\) fora da allowlist"):
        query_sales(sql, db_path=db_path)


@pytest.mark.parametrize(
    "sql",
    [
        # Valor de string contendo uma palavra que estava na antiga blocklist
        # textual de keywords (ex. "call") — checagem baseada em regex sobre
        # o texto bruto bloqueava isso incorretamente por não distinguir
        # conteúdo de string de sintaxe SQL real (ver docs/adrs/0003).
        "SELECT * FROM order_items WHERE seller_id = 'call'",
        # Valor de string contendo um ';' — checagem baseada em
        # `sql.split(";")` contava isso como duas instruções e rejeitava a
        # query inteira (ver docs/adrs/0003).
        "SELECT * FROM order_items WHERE order_id = 'a;b'",
    ],
)
def test_query_sales_string_literal_content_does_not_trigger_false_positive(
    db_path: Path, sql: str
) -> None:
    result = query_sales(sql, db_path=db_path)

    assert isinstance(result, ToolQueryResult)
    assert result.row_count == 0


def test_query_sales_without_limit_gets_one_applied(db_path: Path) -> None:
    result = query_sales("SELECT * FROM order_items", db_path=db_path)

    assert "LIMIT" in result.sql.upper()
    assert result.row_count == ORDER_ITEMS_ROW_COUNT


def test_query_sales_with_explicit_limit_is_still_respected(db_path: Path) -> None:
    result = query_sales("SELECT * FROM order_items LIMIT 1", db_path=db_path)

    assert result.row_count == 1


def test_get_schema_describes_all_olist_tables(db_path: Path) -> None:
    tables = get_schema(db_path=db_path)

    assert [table.name for table in tables] == [
        "customers",
        "sellers",
        "products",
        "geolocation",
        "orders",
        "order_items",
        "order_payments",
        "order_reviews",
    ]
    order_items = next(t for t in tables if t.name == "order_items")
    assert {c.name for c in order_items.columns} == {
        "order_id",
        "order_item_id",
        "product_id",
        "seller_id",
        "shipping_limit_date",
        "price",
        "freight_value",
    }


def test_get_schema_logs_and_reraises_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # structlog (sem integração com o `logging` stdlib) imprime direto no
    # stdout por padrão — não passa por `caplog`. `capsys` é o jeito correto de
    # observar essas linhas num teste (ver AGENTS.md: confirmar comportamento
    # por teste, não só por leitura do código). Não fixamos o formato exato
    # (console ou JSON depende de qual processor está configurado no momento
    # em que o teste roda — ver `data_agent.api._configure_structlog`), só que
    # o evento de falha foi de fato emitido.
    missing_db_path = tmp_path / "does-not-exist.duckdb"

    with pytest.raises(FileNotFoundError):
        get_schema(db_path=missing_db_path)

    assert "get_schema_failed" in capsys.readouterr().out


@pytest.fixture
def slow_query_db_path(tmp_path: Path) -> Path:
    """DuckDB com uma `order_items` grande o bastante para um cross join lento.

    O objetivo não é o volume de dados em si, mas garantir que
    ``SELECT count(*) FROM order_items a, order_items b, order_items c``
    (produto cartesiano) demore mais que o timeout configurado no teste.
    """
    path = tmp_path / "slow_warehouse.duckdb"
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE order_items AS SELECT range AS order_id FROM range(200000)")
    finally:
        con.close()
    return path


def test_query_sales_enforces_timeout(slow_query_db_path: Path) -> None:
    with pytest.raises(QueryTimeoutError):
        query_sales(
            "SELECT count(*) FROM order_items a, order_items b, order_items c",
            db_path=slow_query_db_path,
            timeout_seconds=0.2,
        )


@pytest.fixture
def large_query_db_path(tmp_path: Path) -> Path:
    """DuckDB com uma `order_items` maior que ``DEFAULT_ROW_LIMIT``.

    Usado para provar que um ``LIMIT`` presente apenas numa subquery interna
    (não na cláusula externa) não escapa do teto de linhas do guard-rail —
    regressão de um bypass real: checar por regex se "LIMIT" aparecia em
    qualquer parte do texto fazia o guard-rail pular o teto por completo
    sempre que a query continha um ``LIMIT`` interno não relacionado (ver
    docs/adrs/0003-sql-controlado-vs-tools-granulares.md).
    """
    path = tmp_path / "large_warehouse.duckdb"
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE order_items AS SELECT range AS order_id, range AS price FROM range(5000)"
        )
    finally:
        con.close()
    return path


def test_query_sales_inner_limit_does_not_bypass_outer_row_cap(
    large_query_db_path: Path,
) -> None:
    # `one_row` tem seu próprio LIMIT 1, mas o CROSS JOIN com `order_items`
    # devolveria as 5000 linhas da tabela se o guard-rail confiasse na
    # presença textual de "LIMIT" em vez de sempre aplicar um teto real.
    result = query_sales(
        "WITH one_row AS (SELECT 1 AS x FROM order_items LIMIT 1) "
        "SELECT oi.order_id, oi.price FROM order_items oi, one_row",
        db_path=large_query_db_path,
    )

    assert result.row_count == DEFAULT_ROW_LIMIT
