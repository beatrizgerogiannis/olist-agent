# ADR-0007: Observabilidade com Langfuse via OpenTelemetry

| Campo      | Valor              |
|------------|--------------------|
| **Status** | Aceito             |
| **Data**   | 2026-09-15         |

## Contexto

Até o Dia 4, `structlog` (`data_agent/api.py`, `data_agent/tools/sql_tools.py`) já loga em JSON
estruturado o início/fim de cada `POST /ask` e de cada execução de `get_schema`/`query_sales`
(incluindo o SQL cru executado) — mas isso é log de linha, não rastreamento: não há como ver, de
um lugar só, a árvore completa de uma pergunta (prompt enviado ao modelo → tool calls que ele
decidiu fazer → SQL rodado por cada uma → resposta final estruturada), nem métricas como tokens
consumidos ou latência por etapa. Para um agente cuja premissa central é "nunca inventar
números" ([ADR-0004](0004-estrategia-anti-alucinacao.md)), poder auditar exatamente o que o
modelo viu e fez em cada resposta — não só o resultado final — é necessário tanto para depurar
comportamento errado quanto para confiar no sistema em produção.

O projeto já usa [Agno](0001-escolha-do-agno.md) como framework do agente, e o Agno tem
integração de primeira classe com **Langfuse** via `openinference-instrumentation-agno` (pacote
da família OpenInference, que instrumenta frameworks de agente/LLM usando **OpenTelemetry**
padrão, não um SDK proprietário do Langfuse). Essa dependência já estava antecipada em
`pyproject.toml` e no layout de `AGENTS.md` (`observability.py — setup de OpenTelemetry /
Langfuse`) desde o início do projeto.

## Decisão

Usar **Langfuse** (tier gratuito do Langfuse Cloud, região US — `LANGFUSE_HOST` em
`.env.example`) como backend de observabilidade, instrumentado via **OpenTelemetry padrão**, não
via um SDK proprietário do Langfuse (o pacote `langfuse` já está em `pyproject.toml`, mas não é
importado em nenhum lugar do código — só o protocolo OTLP e a instrumentação OpenInference são
usados).

`data_agent/observability.py::configure_observability()` monta esse caminho:

1. Um `opentelemetry.sdk.trace.TracerProvider` (com `Resource` identificando o serviço como
   `"data-agent"`).
2. Um `OTLPSpanExporter` (protocolo HTTP, pacote `opentelemetry-exporter-otlp`) apontado para
   `<LANGFUSE_HOST>/api/public/otel/v1/traces` — o endpoint OTLP documentado pelo Langfuse —
   autenticado via HTTP Basic Auth (`base64(LANGFUSE_PUBLIC_KEY:LANGFUSE_SECRET_KEY)`, o esquema
   de auth que o Langfuse exige nesse endpoint, não um Bearer token).
3. `AgnoInstrumentor().instrument(tracer_provider=tracer_provider)` (pacote
   `openinference-instrumentation-agno`) — instrumenta automaticamente `Agent.run`/`.arun` (do
   modelo principal e do `parser_model` — ver [ADR-0006](0006-troca-de-provedor-llm-para-groq.md)),
   toda chamada de modelo (`Model.invoke`/`.ainvoke`), e `FunctionCall.execute`/`.aexecute` (as
   tools `get_schema`/`query_sales`), sem precisar instrumentar manualmente cada ponto em
   `tools/sql_tools.py` ou `agent.py`.

`configure_observability()` é chamada uma única vez, na importação de `data_agent/api.py` —
mesmo padrão já usado por `_configure_structlog` nesse módulo — antes de qualquer request,
garantindo que a instrumentação já está ativa quando o primeiro `Agent` é construído
(`get_agent()`, cacheado por `@lru_cache`).

As credenciais (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`) são lidas
exclusivamente via `Settings` (`data_agent/config.py`), nunca hardcoded — mesma regra já seguida
por `GROQ_API_KEY` e documentada em `AGENTS.md`.

### Por que Langfuse

- **Foco em LLM/agentes**, não observabilidade genérica de infraestrutura: traces do Langfuse já
  entendem o vocabulário de um agente (prompt, tool call, tokens de entrada/saída, custo
  estimado, latência por etapa) em vez de só "span genérico com atributos arbitrários" — o
  dashboard já vem pronto para o que este projeto precisa auditar (rastrear cada número
  respondido até a query que o gerou).
- **Suporte nativo ao Agno via OpenInference** (`openinference-instrumentation-agno`): não é
  preciso escrever nenhuma instrumentação manual — um pacote mantido pela comunidade
  OpenInference/Arize já sabe extrair prompt, tool calls, SQL executado (via
  `tools/sql_tools.py`) e tokens de dentro da execução do `Agent` do Agno.
- **Tier gratuito** suficiente para o volume de um projeto de portfólio (testes manuais, uso
  ocasional) — sem custo de infraestrutura própria para rodar um coletor/backend de
  observabilidade.

### Por que instrumentar via OpenTelemetry padrão, não um SDK proprietário do Langfuse

O Langfuse também oferece um SDK Python próprio (`langfuse`, já listado em `pyproject.toml` desde
o início do projeto) com decorators/context managers específicos da API do Langfuse. A decisão
foi **não usar esse SDK para instrumentação** — usar só o exportador OTLP genérico do
`opentelemetry-sdk`/`opentelemetry-exporter-otlp` mais o instrumentador OpenInference, que fala
o protocolo OTLP padrão, não uma API proprietária do Langfuse:

- **Portabilidade de backend**: `configure_observability()` só conhece `TracerProvider`,
  `OTLPSpanExporter` e `AgnoInstrumentor` — nenhum desses três é específico do Langfuse. Trocar de
  backend de observabilidade (outro produto compatível com OTLP — Honeycomb, Grafana Tempo, um
  coletor OpenTelemetry self-hosted, etc.) seria mudar `LANGFUSE_HOST`/as credenciais para o
  endpoint/auth do novo backend, sem tocar `AgnoInstrumentor` nem reescrever nenhuma
  instrumentação — a troca de biblioteca (`langfuse` → outro SDK proprietário) não existe, porque
  nunca existiu essa dependência para começo de conversa.
- **Menos superfície de acoplamento a uma API que pode mudar**: um SDK proprietário amarra o
  código da aplicação a decisões de design específicas de um produto (decorators, nomes de
  classe, versionamento de API própria). OTLP é um protocolo padronizado (CNCF) com garantias de
  estabilidade mais fortes do que a API interna de qualquer produto de observabilidade
  individual.
- **A própria Langfuse recomenda esse caminho** para integrações com frameworks que já têm
  instrumentação OpenInference/OpenTelemetry pronta (como o Agno) — usar o SDK proprietário faria
  sentido para instrumentação manual ponto a ponto, que não é o caso aqui.

O trade-off aceito: o SDK `langfuse` tem funcionalidades além de tracing (datasets, prompt
management, scoring de avaliações) que não estão disponíveis usando só OTLP — se o projeto
precisar delas no futuro, aí sim valeria revisitar essa decisão.

## Consequências

### Positivas

- Cada `POST /ask` agora gera uma árvore de spans completa no Langfuse: o `run` do agente, a
  chamada ao modelo principal (com tool calls), a execução de cada tool (`get_schema`/
  `query_sales`, com o SQL de fato executado como atributo do span — o mesmo SQL que já ia para o
  log do `structlog`, agora também correlacionável num trace), e a chamada extra ao
  `parser_model` que estrutura a resposta final (ver ADR-0006) — visível como um span de modelo
  separado, deixando o custo dessa chamada extra (latência, tokens) explícito no trace, não
  escondido.
- Tokens consumidos e latência por chamada de modelo aparecem automaticamente (extraídos pelo
  `AgnoInstrumentor` da resposta do provedor), sem nenhum código deste projeto precisar calcular
  ou logar isso manualmente.
- Trocar de framework de agente no futuro não quebraria a observabilidade por completo: como a
  base é `TracerProvider`/OTLP padrão, só o instrumentador (`AgnoInstrumentor`) seria trocado por
  um equivalente do novo framework — o exportador e a config de Langfuse continuam os mesmos.

### Negativas / Trade-offs

- **Nenhuma validação de credenciais em tempo de importação.** `configure_observability()` nunca
  levanta exceção por causa de `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` erradas ou
  `LANGFUSE_HOST` inacessível — a exportação roda em background (`BatchSpanProcessor`) e falhas
  viram só log do `opentelemetry-exporter-otlp`, nunca um erro visível em `POST /ask`. Isso é
  deliberado (uma falha de observabilidade não deve derrubar o agente) e foi verificado de
  verdade, não só assumido a partir do design do `BatchSpanProcessor`: apontando
  `LANGFUSE_HOST` para um domínio inexistente
  (`https://this-host-does-not-exist.invalid`) e rodando `POST /ask` contra a API local, a
  requisição respondeu normalmente (502, pelo motivo de sempre — nesse teste específico, rate
  limit da Groq — não por causa do Langfuse) em ~6s, com o exportador só logando
  `Transient error ... NameResolutionError ... encountered while exporting span batch,
  retrying` em background, sem nenhuma exceção subir até `data_agent/api.py`. Isso também é
  coberto, mesmo que incidentalmente, pela suíte de testes de CI: o `.env` mockado em
  `.github/workflows/pr-quality-checks.yml` já usa `LANGFUSE_HOST=http://localhost:3000` (nada
  escutando nessa porta durante o job de testes), então todo teste de `POST /ask` em
  `tests/test_api.py` já roda, a cada execução de CI, com o exportador OTLP incapaz de
  entregar spans — e sempre passou. Ainda assim, a única forma de confirmar que os traces
  chegam de verdade (com credenciais válidas) é olhar o dashboard do Langfuse manualmente
  (feito neste Dia 5, rodando `tests/golden_questions.jsonl` + perguntas extras contra a API
  local) — não há teste automatizado que prove isso com credenciais reais, e um erro de
  configuração (chave revogada, host errado apontando para outro Langfuse) passaria
  silenciosamente por `make check`.
- **Menos funcionalidade que o SDK proprietário do Langfuse** ofereceria (ver seção anterior) —
  aceito conscientemente pela portabilidade.
- **Spans em texto livre podem conter dado sensível** pela mesma razão já registrada em
  [ADR-0005](0005-insufficient-data-como-resposta-valida.md) para os logs do `structlog`: o SQL
  cru (com literais vindos da pergunta do usuário) vira atributo de span, visível no dashboard do
  Langfuse. Aceitável hoje porque o dataset Olist é sintético/anonimizado — a mesma ressalva do
  ADR-0005 sobre trocar de fonte de dados se aplica aqui também.
