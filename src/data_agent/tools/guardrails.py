"""Guard-rails de SQL controlado (ver docs/adrs/0002-camada-de-dados.md).

Toda query executada contra o warehouse passa por aqui antes de chegar ao
DuckDB: só uma única instrução ``SELECT`` (incluindo ``WITH``/CTE, ou
``UNION``/``INTERSECT``/``EXCEPT`` de ``SELECT``s), apenas nas 8 tabelas do
Olist, com um ``LIMIT`` garantido e um timeout de execução.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

# Ordem espelha a apresentação em docs/data_dictionary.md.
TABLE_ORDER: tuple[str, ...] = (
    "customers",
    "sellers",
    "products",
    "geolocation",
    "orders",
    "order_items",
    "order_payments",
    "order_reviews",
)
ALLOWED_TABLES: frozenset[str] = frozenset(TABLE_ORDER)

DEFAULT_ROW_LIMIT = 1000
DEFAULT_QUERY_TIMEOUT_SECONDS = 10.0

_SQL_DIALECT = "duckdb"

# Tipos de nó-raiz que representam uma consulta somente-leitura: um único
# SELECT (o sqlglot já resolve ``WITH ... SELECT`` como um Select com a CTE
# anexada) ou uma combinação de SELECTs via UNION/INTERSECT/EXCEPT.
# Allowlist, não blocklist: qualquer outro tipo de instrução (INSERT, UPDATE,
# DELETE, DROP, ALTER, CREATE, ATTACH, PRAGMA, COPY, SET, CALL, VACUUM, ...)
# é rejeitado por padrão — inclusive comandos que o sqlglot não modela
# explicitamente e cai em ``exp.Command``, que também não está nesta
# allowlist. Quem efetivamente decide quais tabelas/fontes de dados a query
# acessa é ``_check_allowed_tables``, a partir do plano real gerado pelo
# DuckDB — ver comentário lá para o porquê de não confiar só na estrutura
# estática (nem do parser, nem de regex) para essa parte.
_ALLOWED_STATEMENT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
)

# Nomes de nó "folha" (sem filhos) no plano do DuckDB que não leem de uma
# tabela real e por isso não precisam passar pela allowlist: ``DUMMY_SCAN``
# (ex.: ``SELECT 1``) e re-leituras de uma CTE já materializada/decorrelacionada
# (``CTE_SCAN`` — o ``SEQ_SCAN`` real por trás dela é validado separadamente,
# como outro nó do mesmo plano). Confirmado que ambos aparecem no plano
# **lógico** (que ``_check_allowed_tables`` lê — ver sua docstring) para
# ``SELECT 1`` e para CTEs comuns/recursivas, respectivamente.
# ``EMPTY_RESULT``, ``REC_CTE_SCAN`` e ``DELIM_SCAN`` são otimizações do
# planejador **físico** (não observadas no plano lógico em nenhum dos
# formatos de query testados nesta investigação — ver docstring de
# ``_check_allowed_tables``); mantidos aqui só como rede de segurança caso
# algum formato de query não testado ainda os produza no plano lógico.
_NO_SOURCE_LEAF_NODES: frozenset[str] = frozenset(
    {"DUMMY_SCAN", "EMPTY_RESULT", "CTE_SCAN", "REC_CTE_SCAN", "DELIM_SCAN"}
)


class SqlGuardrailError(ValueError):
    """Uma query violou algum guard-rail e foi bloqueada antes de executar."""


class QueryTimeoutError(TimeoutError):
    """A execução da query excedeu o timeout configurado."""


def _check_allowed_tables(con: duckdb.DuckDBPyConnection, statement: str) -> None:
    """Garante que ``statement`` só lê das tabelas do Olist, via o plano real.

    Extrair nomes de tabela de SQL por regex (``FROM/JOIN <identificador>``)
    parece suficiente, mas não é: identificadores entre aspas duplas (ex.
    ``FROM "information_schema"."tables"``) ou funções de tabela (ex.
    ``duckdb_tables()``, ``read_csv(...)``) não batem com um regex de
    identificador simples e passariam pela allowlist sem serem detectados —
    confirmado executando esse bypass contra o warehouse real antes desta
    correção. Em vez disso, pedimos ao próprio DuckDB o plano de execução, que
    já resolve aspas, aliases e CTEs para os nomes de tabela reais, e
    validamos cada nó-folha do plano: um ``SEQ_SCAN`` precisa apontar para uma
    tabela permitida, e qualquer outro tipo de fonte de dados (função de
    tabela, leitura de arquivo, catálogo do sistema) é bloqueada por padrão —
    allowlist, não blocklist.

    Lê especificamente o **plano lógico, pré-otimização** (``PRAGMA
    explain_output='all'`` + a chave ``"logical_plan"`` do resultado de
    ``EXPLAIN (FORMAT JSON)``), não o plano físico (o que ``EXPLAIN`` devolve
    por padrão). O otimizador físico do DuckDB reescreve alguns padrões de
    leitura de tabela para operadores que não carregam mais o nome da tabela
    em ``extra_info`` — descoberto como achado colateral da validação manual
    do Dia 5 (observabilidade), reproduzido contra o warehouse real:
    ``query_sales("SELECT COUNT(*) AS c FROM sellers")`` (uma tabela
    normalmente permitida) era bloqueada com ``SqlGuardrailError: Fonte de
    dados não permitida em SQL somente-leitura: ['COLUMN_DATA_SCAN']`` — um
    falso positivo, porque ``COUNT(*)``/``COUNT(coluna)`` sem filtro sobre uma
    tabela inteira vira um nó físico ``COLUMN_DATA_SCAN`` (lê só metadados de
    zonemap, não a coluna de verdade) sem nenhum ``Table`` em ``extra_info``,
    então caía no branch de "fonte de dados não reconhecida" em vez de ser
    validado contra ``ALLOWED_TABLES`` como um ``SEQ_SCAN`` normal. Investigar
    isso também expôs um segundo problema, mais sério, na mesma família:
    ``SELECT * FROM outra_tabela WHERE 1=0`` (uma tabela fora da allowlist)
    **não era bloqueada** — o otimizador físico já sabe que o predicado é
    sempre falso e substitui a leitura inteira por um ``EMPTY_RESULT``
    constante, também sem nome de tabela, e ``EMPTY_RESULT`` já estava (e
    continua) na lista de nós "sem fonte" abaixo (``_NO_SOURCE_LEAF_NODES``),
    pensada para casos como ``SELECT 1`` (que legitimamente não lê tabela
    nenhuma). Comparando ``physical_plan`` com ``logical_plan`` lado a lado
    para mais de 15 formatos de query (``COUNT(*)``/``COUNT(coluna)`` com e
    sem ``WHERE``/``GROUP BY``/``JOIN``/CTE/``UNION``/window function/
    subquery correlacionada/CTE recursiva/predicado sempre-falso) contra um
    warehouse real: o plano lógico (capturado antes dessas duas otimizações)
    preserva o ``SEQ_SCAN`` com a tabela real em todos os casos — inclusive
    nos dois falsos positivos/negativos acima — e continua idêntico ao plano
    físico em todos os outros formatos de query já cobertos pelos testes
    existentes (``SELECT *``, ``information_schema``, ``duckdb_tables()``,
    CTE comum, agregação com ``WHERE``/``GROUP BY``). Ver
    ``tests/test_tools.py`` para os casos de regressão de ambos os problemas,
    e docs/adrs/0002-camada-de-dados.md para a nota registrando esta correção.
    """
    try:
        con.execute("PRAGMA explain_output='all'")
        rows = con.execute(f"EXPLAIN (FORMAT JSON) {statement}").fetchall()
    except duckdb.Error as exc:
        raise SqlGuardrailError(f"Query inválida: {exc}") from exc

    plan_by_name = dict(rows)
    plan: list[dict[str, Any]] = json.loads(plan_by_name["logical_plan"])

    disallowed_tables: set[str] = set()
    disallowed_sources: set[str] = set()

    def visit(node: dict[str, Any]) -> None:
        children = node.get("children", [])
        for child in children:
            visit(child)
        if children:
            return

        name = node.get("name", "")
        if name == "SEQ_SCAN":
            table = str(node.get("extra_info", {}).get("Table", ""))
            table_name = table.rsplit(".", maxsplit=1)[-1]
            if table_name not in ALLOWED_TABLES:
                disallowed_tables.add(table_name or table)
        elif name not in _NO_SOURCE_LEAF_NODES:
            disallowed_sources.add(name)

    for node in plan:
        visit(node)

    if disallowed_tables:
        raise SqlGuardrailError(
            f"Tabela(s) fora da allowlist do Olist: {sorted(disallowed_tables)}. "
            f"Tabelas permitidas: {sorted(ALLOWED_TABLES)}."
        )
    if disallowed_sources:
        raise SqlGuardrailError(
            f"Fonte de dados não permitida em SQL somente-leitura: {sorted(disallowed_sources)}."
        )


def _parse_single_readonly_statement(sql: str) -> exp.Expression:
    """Parseia ``sql`` e garante que é uma única instrução somente-leitura.

    Usa o sqlglot para entender a estrutura real da query, em vez de checar
    palavras-chave ou contar ``;`` no texto bruto: um checker textual não
    distingue sintaxe de conteúdo dentro de um literal de string, então um
    filtro de negócio legítimo como ``WHERE seller_id = 'call'`` (contém uma
    palavra que antes estava numa blocklist de keywords) ou
    ``WHERE order_id = 'a;b'`` (contém um ``;`` que antes era interpretado
    como separador de instruções) era bloqueado incorretamente — bug
    reproduzido contra um warehouse real e corrigido nesta função (ver
    docs/adrs/0003-sql-controlado-vs-tools-granulares.md). Um parser de
    verdade sabe que esse texto está dentro de uma string, não é SQL.
    """
    try:
        parsed = sqlglot.parse(sql, read=_SQL_DIALECT)
    except SqlglotError as exc:
        raise SqlGuardrailError(f"Query inválida: {exc}") from exc

    # `sqlglot.parse` devolve um nó por instrução separada por ``;``, mas um
    # ``;`` sobrando (à direita, duplicado, ou seguido só de comentário) vira
    # um nó vazio (`None` ou `exp.Semicolon`) — descartamos esses fillers
    # antes de contar quantas instruções reais existem, para não pontuar
    # `"SELECT 1;"` ou `"SELECT 1;; "` como múltiplas instruções.
    statements = [
        stmt for stmt in parsed if stmt is not None and not isinstance(stmt, exp.Semicolon)
    ]

    if len(statements) != 1:
        raise SqlGuardrailError("Apenas uma única instrução SQL é permitida por chamada.")

    statement = statements[0]
    if not isinstance(statement, _ALLOWED_STATEMENT_TYPES):
        raise SqlGuardrailError(
            "Somente instruções SELECT (incluindo WITH/CTE, ou UNION/INTERSECT/EXCEPT de "
            f"SELECTs) são permitidas; recebido: {type(statement).__name__}."
        )

    return statement


def sanitize_query(
    con: duckdb.DuckDBPyConnection, sql: str, *, default_limit: int = DEFAULT_ROW_LIMIT
) -> str:
    """Valida ``sql`` e devolve a query pronta para execução (com LIMIT).

    Levanta :class:`SqlGuardrailError` se a query não for um único ``SELECT``
    somente-leitura restrito às tabelas do Olist. Precisa de uma conexão
    (``con``) porque a validação de tabelas usa o plano real do DuckDB — ver
    :func:`_check_allowed_tables`.

    O teto de linhas é sempre aplicado envolvendo a query (reserializada a
    partir da árvore validada por :func:`_parse_single_readonly_statement`)
    numa subquery com ``LIMIT`` externo, em vez de checar por regex se algum
    ``LIMIT`` já existe no texto: um ``LIMIT`` em qualquer subquery/CTE
    interna, sem relação com a cardinalidade real da query externa (ex. um
    ``CROSS JOIN`` de ``order_items`` com uma CTE ``LIMIT 1`` só para
    produzir uma linha "coringa" — ver
    ``test_query_sales_inner_limit_does_not_bypass_outer_row_cap`` em
    ``tests/test_tools.py``), fazia a checagem por regex achar que a query já
    estava limitada e pular o teto por completo, devolvendo a tabela inteira
    — reproduzido contra um warehouse real antes desta correção. Um ``LIMIT``
    explícito na query original continua sendo respeitado normalmente, pois
    ele já restringe as linhas disponíveis para o ``LIMIT`` externo antes de
    este precisar agir.
    """
    parsed_statement = _parse_single_readonly_statement(sql)
    statement = parsed_statement.sql(dialect=_SQL_DIALECT)

    _check_allowed_tables(con, statement)

    # `statement` veio da árvore validada por _parse_single_readonly_statement
    # (só SELECT/UNION/INTERSECT/EXCEPT) e por _check_allowed_tables acima;
    # `default_limit` é um `int` (não uma string vinda de fora) — nada não
    # validado é interpolado aqui, então o alerta do bandit (B608) é falso
    # positivo.
    return f"SELECT * FROM ({statement}) AS _query_sales_limited LIMIT {default_limit}"  # nosec B608


def execute_guarded(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    default_limit: int = DEFAULT_ROW_LIMIT,
    timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
) -> str:
    """Sanitiza ``sql`` e a executa em ``con`` com um timeout.

    O resultado fica disponível em ``con`` (via ``fetchall``/``description``),
    como de costume na API do DuckDB. Retorna a query efetivamente executada
    (útil para auditoria/logging).
    """
    safe_sql = sanitize_query(con, sql, default_limit=default_limit)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(con.execute, safe_sql)
        try:
            future.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            con.interrupt()
            raise QueryTimeoutError(
                f"Query excedeu o timeout de {timeout_seconds}s: {safe_sql!r}"
            ) from exc

    return safe_sql
