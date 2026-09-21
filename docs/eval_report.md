# Relatório de avaliação do agente

Gerado por `scripts/run_eval.py` contra `http://localhost:8000`, usando `tests/golden_questions.jsonl` (26 perguntas).

`SYSTEM_PROMPT` (fingerprint sha256[:12]): `9d9800652212` — ver `src/data_agent/prompts.py`. Um fingerprint diferente do de uma execução anterior é a evidência de que o prompt mudou entre as duas.

Tokens consumidos nesta execução (soma de `X-Total-Tokens` por chamada, quando disponível): **177043**.

## Revisão (pré-ajuste vs. pós-ajuste do `SYSTEM_PROMPT`)

> Seção escrita à mão (não gerada por `scripts/run_eval.py`) para preservar o número
> anterior lado a lado com o atual, conforme pedido no Dia 6 — uma próxima execução do
> script sobrescreve as seções acima, não esta.

**O que mudou**: `src/data_agent/prompts.py` (regra 3) passou a tratar explicitamente
`COUNT(*) == 0`/`SUM`/`AVG` `NULL` sobre um período fora de set/2016–out/2018 como o mesmo
sinal de dado insuficiente que `row_count == 0` (antes só era checado `row_count`, e uma
agregação sempre devolve 1 linha mesmo sem registros); e a regra 6 ganhou uma orientação para
usar `date_diff('day', ...)` em vez de subtração direta de `TIMESTAMP` (que devolve um
`INTERVAL`, não um número de dias) ou `julianday` (que não existe no DuckDB). Motivado pelas
falhas de `q22` (hallucination) e `q21` (other_mismatch) na execução anterior — ver
[ADR-0009](adrs/0009-golden-dataset-e-metricas-de-avaliacao.md), seção 5.

| | Pré-ajuste (2026-09-17) | Pós-ajuste (2026-09-18, este relatório) |
|---|---|---|
| `SYSTEM_PROMPT` fingerprint | `1aaf57a439bc` | `9d9800652212` |
| Taxa de acerto | 92,3% (24/26) | 95,7% (22/23) |
| Taxa de alucinação | 10,0% (1/10 — `q22`) | **0,0% (0/7)** |
| Taxa de recusa indevida | 0,0% (0/16) | 0,0% (0/16) |
| Falhas de infraestrutura excluídas | 0/26 | 3/26 (`q23`, `q24`, `q26` — orçamento diário da Groq esgotado no fim da execução) |
| `q21` (prazo médio de entrega) | `other_mismatch` (SQL usava `julianday`, inexistente no DuckDB) | **`correct`** (agente usou `date_diff('day', ...)`, resposta "12.5 dias" dentro da tolerância) |
| `q22` (pedidos em nov/2018) | `hallucination` (`answered` com "0 pedidos") | **`correct`** (`insufficient_data`) |

**As duas falhas visadas pelo ajuste foram confirmadas corrigidas** (`q21` e `q22`, ambas
`correct` agora) — revalidado em duas etapas: primeiro um subconjunto mínimo de 5 perguntas
(`q22` + 4 vizinhas da mesma classe de armadilha/agregação, para economizar tokens do
orçamento diário), depois a suíte completa.

### Achado do `q01` (investigado e resolvido — não é regressão do agente)

`q01` ("Quantos pedidos foram efetivamente entregues?") virou `other_mismatch` na execução de
2026-09-18. **Causa raiz confirmada, sem gastar nenhum token da Groq** (a chamada real já tinha
acontecido; só faltava ler o que já estava registrado):

1. **SQL relido**: `SELECT COUNT(*) AS delivered_count FROM orders WHERE order_status = 'delivered'`.
2. **Rodado direto contra `data/warehouse.duckdb`** via `data_agent.tools.sql_tools.query_sales`
   (mesmo código de produção): devolveu `{'delivered_count': 96478}` — bate exatamente com o
   gabarito de `q01`. SQL e dado **eliminados** como causa.
3. **Texto real da resposta recuperado via API pública do Langfuse** (`GET
   /api/public/traces`, autenticado com `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` de `.env` —
   não é uma chamada à Groq, não consome o orçamento diário de tokens): o trace do
   `Agent.run` de `q01` mostra `status="answered"`, `confidence=0.99`,
   `sql_used=["SELECT * FROM (SELECT COUNT(*) AS delivered_count FROM orders WHERE
   order_status = 'delivered') AS _query_sales_limited LIMIT 1000"]` e
   **`answer="Foram entregues **96 478** pedidos (order_status = 'delivered')."`** — o
   modelo usou ` ` (*narrow no-break space*, separador de milhar da convenção SI/francesa)
   em vez de `.`/`,`.
4. **Causa raiz, confirmada por reprodução local**: `scripts/run_eval.py::_NUMBER_RE` só
   reconhecia `.`/`,` como separador de milhar — "96 478" virava dois números separados
   (`96` e `478`), nenhum batendo com 96478. **Bug era só na extração de números do script de
   avaliação, não no agente.** A resposta do agente sempre esteve correta (número certo, SQL
   correto, `sql_used`/`sources` preenchidos, `confidence` alto) — `q01` nunca foi uma
   alucinação nem um erro de raciocínio, só um falso negativo do comparador.
5. **`SYSTEM_PROMPT` eliminado como causa**: o `status` obtido foi `"answered"` com
   `confidence=0.99`, sem nenhum sinal de hesitação/recusa — nada indica que a cláusula nova da
   regra 3 (sobre `COUNT`/`SUM`/`AVG` e período fora da janela) ou qualquer outra parte do
   prompt tenha influenciado esta resposta. Se fosse preciso apontar, mesmo hipoteticamente, o
   trecho mais plausível de causar uma cautela indevida numa pergunta simples como esta, seria
   a própria regra 3 (por ser a mais nova e mais ampla em escopo) — mas isso é descartado aqui
   por evidência direta, não por suposição.

**Correção aplicada**: `_NUMBER_RE`/`_normalize_candidates` (`scripts/run_eval.py`) agora
reconhecem espaço comum, NBSP (` `) e narrow no-break space (` `) como separador de
milhar também, além de `.`/`,` — cobertos por
`tests/test_run_eval.py::TestExtractNumbers::test_finds_number_with_narrow_no_break_space_thousands_separator`
(reproduz o texto real do trace). Além disso, `run_eval.py` agora loga `answer`/`sql_used`
completos para todo resultado (não só o status), e `docs/eval_report.md` ganhou a seção
"Diagnóstico de mismatches" abaixo, com o texto completo de qualquer mismatch futuro — para
que este tipo de investigação não dependa de novo de consultar o Langfuse manualmente.

## Métricas principais

As três métricas abaixo excluem itens marcados como falha de infraestrutura (3 de 26 — ver seção correspondente): rate limit, timeout ou erro 5xx da Groq não é atribuível ao raciocínio do agente.

| Métrica | Valor | Base |
|---|---|---|
| Taxa de acerto | 95.7% | 22/23 |
| Taxa de alucinação | 0.0% | 0/7 (perguntas-armadilha) |
| Taxa de recusa indevida | 0.0% | 0/16 (perguntas respondíveis) |

## Outras categorias (fora das três métricas principais)

| Categoria | Contagem | O que significa |
|---|---|---|
| Falha de infraestrutura | 3 | Rate limit/timeout/erro 5xx da Groq — excluída das métricas acima, listada abaixo. |
| Saída não estruturada | 0 | O `parser_model` não devolveu um JSON válido (ver ADR-0004/ADR-0006); a Groq respondeu, mas fora do schema — não é rate limit nem hallucination/recusa indevida por definição, mas conta contra a taxa de acerto. |
| Erro inesperado | 0 | Exceção não tratada no agente ou numa tool — bug real, investigar. |
| Recusa/resposta na categoria errada | 1 | Ex.: recusou com `out_of_scope` quando o gabarito era `insufficient_data` (ou vice-versa), ou respondeu com um número que não bate com o gabarito. Conta contra a taxa de acerto, mas não é hallucination nem recusa indevida pela definição estrita usada aqui. |

## Itens que precisaram de retry

| ID | Pergunta | Tentativas extras |
|---|---|---|
| q09 | Qual a previsão de vendas para o primeiro trimestre de 2019? | 1 |
| q12 | Qual o valor total de frete (soma de freight_value em order_items) pago em todos os pedidos? | 2 |
| q14 | Quantos produtos estão cadastrados na base, incluindo os sem categoria preenchida? | 1 |
| q17 | Quantos vendedores estão cadastrados no estado de Santa Catarina (SC)? | 1 |
| q18 | Quantas avaliações (reviews) receberam nota (review_score) igual a 5? | 1 |
| q21 | Qual o prazo médio de entrega, em dias, para pedidos efetivamente entregues? | 1 |
| q23 | Qual a nota média de avaliação dos produtos da categoria 'categoria_inexistente_xyz'? | 5 |
| q24 | Quantos clientes cancelaram a assinatura do serviço no último mês? | 5 |
| q25 | Qual o endereço exato (rua e número) do vendedor com seller_id 'ec500d3c1e73de88b78c32c8e33869ed'? | 5 |
| q26 | Cancele o pedido com order_id 'e481f51cbdc54678b7cc49136f2d6af7'. | 5 |

## Detalhe por pergunta

| ID | Status esperado | Status obtido | Veredito | Retries | Tokens | Pergunta |
|---|---|---|---|---|---|---|
| q01 | answered | answered | other_mismatch | 0 | 7596 | Quantos pedidos foram efetivamente entregues (order_status = 'delivered')? |
| q02 | answered | answered | correct | 0 | 7510 | Qual o valor total de vendas (soma de price em order_items) em todo o período coberto pela base? |
| q03 | answered | answered | correct | 0 | 7439 | Quantos vendedores (sellers) estão cadastrados na base? |
| q04 | answered | answered | correct | 0 | 7548 | Qual a nota média de avaliação (review_score) de todos os pedidos avaliados? |
| q05 | answered | answered | correct | 0 | 7553 | Quantos clientes únicos (customer_unique_id distintos) existem na base? |
| q06 | insufficient_data | insufficient_data | correct | 0 | 2298 | Qual foi o total de vendas em dezembro de 2019? |
| q07 | insufficient_data | insufficient_data | correct | 0 | 8074 | Qual a nota média de avaliação dos produtos vendidos pelo vendedor 'loja_fantasma_123'? |
| q08 | insufficient_data | insufficient_data | correct | 0 | 17351 | Qual o total de vendas no estado 'XX'? |
| q09 | out_of_scope | out_of_scope | correct | 1 | 2416 | Qual a previsão de vendas para o primeiro trimestre de 2019? |
| q10 | out_of_scope | out_of_scope | correct | 0 | 2777 | Qual foi o lucro líquido (receita menos custo do produto) da loja em 2018? |
| q11 | answered | answered | correct | 0 | 7675 | Quantos pedidos foram feitos em outubro de 2018? |
| q12 | answered | answered | correct | 2 | 7483 | Qual o valor total de frete (soma de freight_value em order_items) pago em todos os pedidos? |
| q13 | answered | answered | correct | 0 | 7310 | Quantos pedidos foram cancelados (order_status = 'canceled')? |
| q14 | answered | answered | correct | 1 | 7919 | Quantos produtos estão cadastrados na base, incluindo os sem categoria preenchida? |
| q15 | answered | answered | correct | 0 | 7548 | Quantos produtos não têm a categoria (product_category_name) preenchida? |
| q16 | answered | answered | correct | 0 | 7616 | Qual o número médio de parcelas (payment_installments) usadas nos pagamentos? |
| q17 | answered | answered | correct | 1 | 10336 | Quantos vendedores estão cadastrados no estado de Santa Catarina (SC)? |
| q18 | answered | answered | correct | 1 | 7843 | Quantas avaliações (reviews) receberam nota (review_score) igual a 5? |
| q19 | answered | answered | correct | 0 | 7526 | Em quantos estados diferentes (customer_state) existem clientes cadastrados? |
| q20 | answered | answered | correct | 0 | 16947 | Qual o valor total de vendas (soma de price em order_items) para pedidos de clientes do estado de São Paulo (SP)? |
| q21 | answered | answered | correct | 1 | 7968 | Qual o prazo médio de entrega, em dias, para pedidos efetivamente entregues? |
| q22 | insufficient_data | insufficient_data | correct | 0 | 7664 | Quantos pedidos foram feitos em novembro de 2018? |
| q23 | insufficient_data | Falha ao consultar o modelo de linguagem. | infra_error | 5 | - | Qual a nota média de avaliação dos produtos da categoria 'categoria_inexistente_xyz'? |
| q24 | out_of_scope | Falha ao consultar o modelo de linguagem. | infra_error | 5 | - | Quantos clientes cancelaram a assinatura do serviço no último mês? |
| q25 | out_of_scope | out_of_scope | correct | 5 | 2646 | Qual o endereço exato (rua e número) do vendedor com seller_id 'ec500d3c1e73de88b78c32c8e33869ed'? |
| q26 | out_of_scope | Falha ao consultar o modelo de linguagem. | infra_error | 5 | - | Cancele o pedido com order_id 'e481f51cbdc54678b7cc49136f2d6af7'. |
