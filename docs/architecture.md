# Arquitetura

## Escopo

Este documento define o que o agente de IA data-aware deve e não deve responder. Ele é a
referência para decidir, em qualquer dia do projeto, se uma pergunta ou uma feature está
dentro do que foi combinado.

### Perguntas que o agente deve responder

O agente consulta a base Olist (via tools de SQL controlado sobre o DuckDB) para responder
perguntas em linguagem natural sobre:

- **Vendas / faturamento**: total de vendas (soma de `price` em `order_items`) por categoria
  de produto, por estado (do cliente ou do vendedor), por vendedor, por período (mês/ano),
  ou combinações dessas dimensões.
- **Logística / entrega**: prazo médio de entrega (diferença entre
  `order_delivered_customer_date` e `order_purchase_timestamp`), comparação entre prazo
  estimado (`order_estimated_delivery_date`) e prazo real, taxa de atraso, por estado ou por
  período.
- **Satisfação do cliente**: nota média de avaliação (`review_score`), distribuição de notas,
  por categoria de produto, por vendedor, por período.
- **Pagamentos**: forma de pagamento mais usada (`payment_type`), número médio de parcelas,
  valor médio de pagamento, por período ou por forma de pagamento.
- **Catálogo / produtos**: quantidade de produtos por categoria, características físicas
  agregadas (peso, dimensões) quando relevante para a pergunta.
- Perguntas que combinam as dimensões acima (ex.: "qual a nota média de avaliação dos pedidos
  entregues com atraso no estado de SP?").

Todas as respostas devem vir de consultas reais aos dados carregados em
`data/warehouse.duckdb` — o agente nunca deve inventar números. Quando a base não tem dado
suficiente para responder com confiança, o agente deve dizer isso explicitamente em vez de
estimar.

### Fora de escopo (explicitamente)

- **Previsão / forecasting**: o agente não projeta vendas futuras, demanda futura ou
  tendências fora do período coberto pelos dados (set/2016 a out/2018). Ver
  [data_dictionary.md](data_dictionary.md) para as limitações do período.
- **Dados fora da base Olist**: preços de concorrentes, dados de mercado externos, notícias,
  cotação de câmbio, ou qualquer informação que não esteja nas 8 tabelas carregadas.
- **Geolocalização de precisão**: o agente não localiza um cliente ou vendedor em um endereço
  exato — a tabela `geolocation` é uma amostra de coordenadas por prefixo de CEP, não um
  geocodificador de endereço completo.
- **Conteúdo textual livre de reviews**: o agente não resume, opina sobre ou cita o texto de
  `review_comment_message` / `review_comment_title` como fonte de verdade qualitativa — a
  análise é sobre os campos estruturados (nota, datas, valores).
- **Ações de escrita**: o agente é somente leitura. Ele não cria, atualiza ou cancela pedidos,
  não altera dados no warehouse.
- **Identificação de pessoas físicas**: `customer_unique_id` e `customer_id` são
  identificadores anonimizados/hasheados; o agente não tenta reidentificar clientes.

## Fluxo de ponta a ponta

Todo o desenho abaixo — por que Agno, por que DuckDB com SQL controlado em vez de tools
granulares, por que três mecanismos de anti-alucinação, por que `insufficient_data`/
`out_of_scope` voltam como `200`, por que Groq com `parser_model`, por que Langfuse via
OpenTelemetry, por que rate limit + UI estática na mesma imagem — está justificado e
detalhado em `docs/adrs/`; este documento só amarra as peças na ordem em que uma requisição
realmente passa por elas.

Antes de qualquer requisição: no boot do container, `data_agent.warehouse_fetch` garante que
`data/warehouse.duckdb` exista (baixando de um bucket S3 privado se necessário), e só então o
`uvicorn` sobe — ver [ADR-0010](adrs/0010-hospedagem-do-demo-publico.md).

```
Usuário
  │  GET / (UI de chat) ou POST /ask (pergunta em linguagem natural)
  ▼
FastAPI (src/data_agent/api.py)
  │
  ├─▶ GET /health, GET / e demais assets ─▶ StaticFiles (static/index.html)
  │                                          — não passa pelo rate limiter nem pelo Agent
  │
  └─▶ POST /ask
        │  SlowAPIMiddleware: rate limit por IP (5/minute) → 429 antes de chamar o Agent
        │  se excedido (ver ADR-0010)
        ▼
        valida AskRequest (Pydantic) → 422 se malformado
        ▼
Agent do Agno (src/data_agent/agent.py, build_agent())
  │  SYSTEM_PROMPT (src/data_agent/prompts.py, já inclui o schema das 8 tabelas
  │  como texto) + tools [query_sales]
  │
  ├─▶ tool: query_sales(sql)       ──▶ tools/guardrails.py (só SELECT, só as 8
  │     (0..N chamadas, decididas       tabelas do Olist, LIMIT, timeout)
  │      pelo próprio modelo)                │
  │                                          ▼
  │                                  DuckDB read-only (data/warehouse.duckdb,
  │                                  via src/data_agent/db.py)
  │
  │  loop de tools termina; resposta em texto ainda não estruturada
  ▼
parser_model (2º Groq, menor — openai/gpt-oss-20b —, sem tools, supports_json_schema_outputs=True)
  │  estrutura a resposta final como AgentAnswer (Pydantic)
  ▼
response_model=AgentAnswer validado pelo FastAPI
  │  status="answered" | "insufficient_data" | "out_of_scope" → sempre 200
  │  (só falha de infraestrutura real vira 4xx/5xx — ver ADR-0005)
  ▼
Usuário (JSON: status, answer, confidence, sql_used, sources)

Em paralelo, toda a árvore acima (chamada ao Agent, cada tool call, cada
chamada de modelo — principal e parser_model) gera spans via
AgnoInstrumentor → OTLP → Langfuse (src/data_agent/observability.py).
```

### Passo a passo

0. **Boot do container → `data_agent.warehouse_fetch`**: antes do `uvicorn` subir, garante que
   `data/warehouse.duckdb` exista (baixando de um bucket S3 privado se ainda não existir no
   caminho de `Settings.duckdb_path`) — sai com código != 0 e não deixa o `uvicorn` subir se
   faltar variável de ambiente ou o download falhar. Localmente, o volume de
   `docker-compose.yml` faz o arquivo já existir e este passo nem chega a baixar nada — ver
   [ADR-0010](adrs/0010-hospedagem-do-demo-publico.md).
1. **Usuário → FastAPI**: `GET /health` e `GET /` (e demais assets estáticos) são servidos por
   `fastapi.staticfiles.StaticFiles` (montado em `/`, ver `static/index.html`) e nunca chegam a
   `POST /ask` — a UI de chat estática e a API vivem na mesma imagem/processo, sem CORS entre
   domínios (ver ADR-0010). Só `POST /ask` segue o caminho abaixo.
2. **`POST /ask` → rate limiter**: `SlowAPIMiddleware` (`slowapi`, `Limiter(key_func=get_remote_address)`)
   aplica `5/minute` por IP antes de qualquer outra coisa — excedido, devolve `429` sem chamar
   o Agent nem gastar tokens do provedor. Existe porque esta é a única rota que dispara uma
   chamada real (e paga) ao provedor de LLM, e o demo é público — ver ADR-0010.
3. **`POST /ask` → `AskRequest`**: o corpo é validado como `AskRequest`
   (`src/data_agent/schemas.py`) pelo próprio FastAPI antes de `ask()` rodar — uma
   `question` ausente, vazia ou de tipo errado já volta `422` sem chegar ao agente.
4. **`api.py` → `Agent` do Agno**: `get_agent()` (cacheado por `@lru_cache`) devolve a
   instância única do processo, construída por `build_agent()`. O modelo principal recebe
   `SYSTEM_PROMPT` como instruções — que já inclui o schema das 8 tabelas como texto (nomes,
   tipos, relações, extraído de `docs/data_dictionary.md`), já que é estático e não muda em
   runtime — e a tool `query_sales` como única fonte de dados — ver
   [ADR-0001](adrs/0001-escolha-do-agno.md) (por que Agno) e
   [ADR-0004](adrs/0004-estrategia-anti-alucinacao.md) (por que esse grounding é
   não-negociável). Até 2026-09-21, o schema era descoberto via uma tool `get_schema`
   separada, chamada em runtime; removida em 2026-09-22 (otimização de latência) porque
   custava um turno inteiro de ida-e-volta à Groq para redescobrir, a cada pergunta, algo que
   nunca muda — ver [ADR-0006](adrs/0006-troca-de-provedor-llm-para-groq.md), seção
   "Otimização de latência". A função `get_schema` (`tools/sql_tools.py`) continua existindo
   para depuração manual, só não é mais uma tool ativa do agente principal.
5. **Loop de tool-calling**: o modelo decide, sozinho, quantas vezes chamar
   `query_sales(sql)` (rodar a consulta) — o schema já veio no prompt, não precisa mais de
   uma tool à parte para descobri-lo. Cada `sql` passa por `tools/guardrails.py` antes de
   tocar o DuckDB: só uma instrução `SELECT`
   (incluindo `WITH`/`UNION`), restrita às 8 tabelas do Olist (validado pelo plano lógico
   real do DuckDB, não por regex), com `LIMIT` e timeout garantidos — ver
   [ADR-0002](adrs/0002-camada-de-dados.md) e [ADR-0003](adrs/0003-sql-controlado-vs-tools-granulares.md).
   A conexão (`src/data_agent/db.py`) é somente leitura como segunda camada de defesa.
6. **`query_sales` → DuckDB → `ToolQueryResult`**: o resultado (linhas, colunas, `row_count`,
   SQL efetivamente executado) volta para o modelo como conteúdo da tool call — nunca como
   um valor que o modelo "calcula" por conta própria.
7. **Fim do loop de tools → `parser_model`**: como o modelo principal (Groq) sempre expõe
   tools, ele nunca recebe `response_format` na mesma chamada (a API da Groq rejeita essa
   combinação); um segundo `Groq`, sem tools, faz uma chamada extra só para estruturar a
   resposta final como `AgentAnswer` — ver [ADR-0006](adrs/0006-troca-de-provedor-llm-para-groq.md).
8. **`AgentAnswer` validado → resposta HTTP**: `response_model=AgentAnswer` garante a forma
   da saída. `status="insufficient_data"` (dado não coberto/zero linhas/erro de tool) e
   `status="out_of_scope"` (tipo de pergunta fora do escopo funcional definido acima) são
   respostas de produto, não erros — voltam em `200`, como `"answered"`. Só uma falha real de
   infraestrutura (timeout do modelo → `504`; erro de provedor, incluindo rate limit,
   disfarçado ou não de exceção → `502`) vira erro HTTP — ver
   [ADR-0005](adrs/0005-insufficient-data-como-resposta-valida.md).
9. **Trace no Langfuse**: em paralelo a todo o resto, `configure_observability()`
   (`src/data_agent/observability.py`, chamada uma única vez na importação de `api.py`)
   instrumenta o `Agent` via OpenTelemetry/OpenInference — cada `POST /ask` gera uma árvore
   de spans completa (chamada ao modelo principal, cada tool call com o SQL executado, a
   chamada ao `parser_model`) exportada para o Langfuse via OTLP — ver
   [ADR-0007](adrs/0007-observabilidade-com-langfuse.md).

### Como isso é validado

`scripts/run_eval.py` roda `tests/golden_questions.jsonl` (perguntas respondíveis +
perguntas-armadilha) contra uma API já no ar e mede taxa de acerto, taxa de alucinação e taxa
de recusa indevida, excluindo falhas de infraestrutura — ver
[ADR-0009](adrs/0009-golden-dataset-e-metricas-de-avaliacao.md) e os números atuais em
[docs/eval_report.md](eval_report.md). O empacotamento (`Dockerfile` + `docker-compose.yml`,
build reprodutível via `uv.lock`) está descrito em
[ADR-0008](adrs/0008-reprodutibilidade-com-uv-lock.md).
