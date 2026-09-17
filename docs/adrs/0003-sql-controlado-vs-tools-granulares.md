# ADR-0003: SQL controlado e genérico vs. tools granulares por métrica

| Campo      | Valor              |
|------------|--------------------|
| **Status** | Aceito             |
| **Data**   | 2026-09-09         |

## Contexto

ADR-0002 já decidiu que o agente acessa o warehouse via "tools de SQL controlado", mas não
detalhou a forma dessas tools. Havia duas abordagens concretas para o Dia 2:

1. **Uma tool genérica `query_sales(sql: str)`**: o agente (LLM) monta a query SQL e uma
   camada de guard-rails (`tools/guardrails.py`) valida que é somente-leitura, restrita às 8
   tabelas do Olist, com `LIMIT` e timeout, antes de executar.
2. **Tools granulares por métrica**, ex.: `get_total_sales(vendedor, período)`,
   `get_avg_delivery_time(estado, período)`, `get_avg_review_score(categoria, período)` — cada
   uma com uma query SQL fixa escrita à mão, parametrizada apenas nos filtros que o autor da
   tool previu.

## Decisão

Usar **SQL controlado e genérico** (`query_sales`), com uma camada de guard-rails dedicada
(`tools/guardrails.py`) em vez de tools granulares por métrica.

Motivos:

- **Cobertura do escopo funcional**: `docs/architecture.md` lista várias dimensões
  combináveis (vendas por categoria/estado/vendedor/período, prazo de entrega, satisfação,
  pagamentos, catálogo). Cobrir todas as combinações com tools granulares exigiria uma
  explosão combinatória de funções (`get_total_sales_by_categoria_e_estado`,
  `get_avg_review_by_vendedor_e_periodo`, ...) ou parâmetros genéricos demais para continuar
  "seguras" (ex.: um parâmetro `group_by: str` livre já reintroduz o mesmo problema que as
  tools granulares deveriam evitar).
- **Perguntas combinadas exigem flexibilidade real**: o exemplo do próprio
  `docs/architecture.md` ("nota média de avaliação dos pedidos entregues com atraso no estado
  de SP") cruza três dimensões (avaliação, atraso, estado) — exatamente o tipo de pergunta
  onde tools granulares ficam engessadas e SQL é a ferramenta natural.
- **Guard-rails compensam o risco**: o risco de SQL genérico é o agente gerar algo destrutivo
  ou fora de escopo. Isso é mitigado por `tools/guardrails.py` (bloqueia DDL/DML, restringe às
  8 tabelas via allowlist, injeta `LIMIT`, aplica timeout) somado a uma conexão DuckDB aberta
  em modo *read-only* (`src/data_agent/db.py`) — defesa em duas camadas independentes contra
  escrita acidental ou maliciosa.
- **Auditabilidade não é perdida**: `ToolQueryResult.sql` e `AgentAnswer.sql_used`
  (`src/data_agent/schemas.py`) guardam a query efetivamente executada, então SQL genérico
  continua tão rastreável quanto uma tool granular seria.

Tools granulares foram descartadas porque o ganho de segurança que ofereceriam (superfície de
ataque menor, parâmetros já validados por tipo) é redundante com o que os guard-rails já
entregam para este projeto, enquanto o custo de manutenção (uma função nova a cada nova
combinação de dimensões que o agente precisa responder) cresce mais rápido que o valor
entregue, dado o número de dimensões combináveis do escopo.

## Consequências

### Positivas

- Uma única tool cobre qualquer combinação de dimensões dentro do schema das 8 tabelas, sem
  precisar prever de antemão todo cruzamento de filtros que o agente vai precisar.
- Guard-rails ficam centralizados em um módulo (`tools/guardrails.py`), testável isoladamente
  (`tests/test_tools.py`), em vez de espalhados/reimplicados em cada tool granular.
- Evoluir o escopo funcional (nova dimensão de análise) não exige nova tool nem novo deploy de
  código — apenas uma pergunta diferente para o mesmo `query_sales`.

### Negativas / Trade-offs

- A primeira versão do guard-rail de allowlist de tabelas validava por regex sobre o texto SQL
  (`FROM`/`JOIN <identificador>`). Isso se mostrou insuficiente na prática: identificadores
  entre aspas duplas (ex. `FROM "information_schema"."tables"`) e funções de tabela (ex.
  `duckdb_tables()`) não batem com um regex de identificador simples e bypassavam a allowlist
  por completo — reproduzido executando `query_sales('SELECT * FROM "duckdb_tables"()')`
  contra o warehouse real, que retornou o DDL de todas as tabelas. A allowlist hoje usa o
  plano de execução real do DuckDB (`EXPLAIN (FORMAT JSON)`), que já resolve aspas, aliases e
  CTEs para os nomes de tabela físicos, e valida cada fonte de dado do plano por allowlist
  (não blocklist): qualquer nó do plano que não seja uma leitura de uma das 8 tabelas (ou uma
  reprojeção sem tabela, tipo `SELECT 1`) é rejeitado por padrão. Ainda assim, isso continua
  sendo menos estático do que a garantia de tipos que uma tool granular com parâmetros
  tipados daria.
- O agente (LLM) pode gerar SQL sintaticamente inválido ou logicamente errado (ex.: join
  errado) que os guard-rails não detectam, porque eles validam segurança/escopo, não
  corretude semântica da query — algo que uma tool granular com lógica fixa não sofreria.
- Exige manter os guard-rails atualizados se o schema mudar (nova tabela, tabela renomeada),
  enquanto tools granulares "quebram" de forma mais óbvia (erro de tipo/assinatura) quando o
  schema muda.
- A primeira versão da checagem de "uma única instrução, somente-leitura" (antes da checagem
  de allowlist de tabelas descrita acima) também era puramente textual: contava instruções via
  `sql.split(";")` e procurava uma blocklist de palavras-chave (`INSERT`, `DROP`, `ATTACH`,
  `CALL`, ...) com regex sobre o texto bruto do SQL. Isso se mostrou insuficiente pelo motivo
  oposto do bypass de tabelas: em vez de deixar passar algo perigoso, bloqueava queries
  `SELECT` legítimas e seguras sempre que um literal de string continha, por coincidência, um
  `;` ou uma das palavras da blocklist — reproduzido com
  `query_sales("SELECT * FROM order_items WHERE seller_id = 'call'")` (bloqueada por conter
  `'call'` dentro de uma string) e
  `query_sales("SELECT * FROM order_items WHERE order_id = 'a;b'")` (bloqueada por conter `;`
  dentro de uma string). Um checker de texto bruto não distingue sintaxe de conteúdo de
  string. A correção troca essa checagem por um parser de SQL de verdade (`sqlglot`): a query é
  parseada e validada pela estrutura real da árvore (é uma única instrução, e o nó raiz é um
  `SELECT`/`WITH`/`UNION`/`INTERSECT`/`EXCEPT` — allowlist de tipos de nó, não blocklist de
  palavras-chave), o que resolve literais de string corretamente e elimina essa classe de
  falso-positivo, mantendo o mesmo comportamento de falha fechada para qualquer instrução que
  não seja somente-leitura (ver `tests/test_tools.py`). É o mesmo padrão já usado em
  `_check_allowed_tables`: confiar na estrutura real (parser/plano), não em uma aproximação
  textual, tanto para não deixar passar algo perigoso quanto para não bloquear algo legítimo.
- Uma terceira rodada no mesmo padrão: `_check_allowed_tables` validava o plano **físico** (o
  que `EXPLAIN (FORMAT JSON)` devolve por padrão) do DuckDB, mas o otimizador físico reescreve
  alguns padrões de leitura de tabela para operadores que não carregam mais o nome da tabela —
  achado colateral da validação manual do Dia 5 (observabilidade,
  [ADR-0007](0007-observabilidade-com-langfuse.md)): `query_sales("SELECT COUNT(*) AS c FROM
  sellers")`, uma tabela normalmente permitida, era bloqueada como falso positivo
  (`SqlGuardrailError: Fonte de dados não permitida ... ['COLUMN_DATA_SCAN']`), porque
  `COUNT(*)`/`COUNT(coluna)` sem filtro sobre uma tabela inteira vira um nó físico
  `COLUMN_DATA_SCAN` (lê só metadados de zonemap) sem `Table` em `extra_info`. Investigar isso
  expôs um segundo bug, mais sério — um bypass real, não um falso positivo: `SELECT * FROM
  <tabela fora da allowlist> WHERE 1=0` não era bloqueado, porque um predicado sempre-falso
  vira um `EMPTY_RESULT` constante (sem tabela nenhuma), e `EMPTY_RESULT` já era ignorado por
  design (pensado para `SELECT 1`). Ambos reproduzidos contra um warehouse real antes da
  correção. A correção troca a fonte de verdade de `_check_allowed_tables` do plano físico para
  o plano **lógico** (pré-otimização, obtido com `PRAGMA explain_output='all'`): confirmado
  comparando os dois planos lado a lado para mais de 15 formatos de query que o plano lógico
  preserva o `SEQ_SCAN` com a tabela real nos dois casos acima, e continua idêntico ao plano
  físico em todo formato de query já coberto por teste (ver a docstring de
  `_check_allowed_tables` para o comparativo completo, e
  `tests/test_tools.py::test_query_sales_count_star_on_allowed_table_is_accepted`,
  `test_query_sales_count_star_variants_are_accepted` e
  `test_query_sales_blocks_real_table_outside_allowlist_via_optimizer_rewrites` para as
  regressões — a última confirma que a correção do falso positivo não abriu a brecha do
  `EMPTY_RESULT`, cobrindo os dois bugs com o mesmo teste que motivou a mudança).
