# AGENTS.md

Guia canônico de convenções deste repositório para qualquer agente de código (humano ou IA)
trabalhando aqui. Leia isto antes de alterar qualquer coisa.

## Visão geral do projeto

Agente de IA "data-aware" de portfólio: responde perguntas em linguagem natural sobre a base
de vendas Olist Brazilian E-Commerce consultando dados reais via tool-calling, nunca
inventando números. O escopo funcional (o que o agente responde e o que fica de fora) está
definido em [docs/architecture.md](docs/architecture.md).

### Stack

- **Python 3.12**, gerenciado por **uv** (`uv sync`, `uv run`).
- **Agno** como framework do agente (tool-calling + `response_model` Pydantic). Ver
  [docs/adrs/0001-escolha-do-agno.md](docs/adrs/0001-escolha-do-agno.md).
- **Pydantic** / **pydantic-settings** para validação de dados e configuração.
- **FastAPI** (+ `uvicorn`) para expor o agente via API.
- **DuckDB** como banco local (`data/warehouse.duckdb`), acessado via SQL controlado, nunca
  SQL livre sem validação. Ver
  [docs/adrs/0002-camada-de-dados.md](docs/adrs/0002-camada-de-dados.md).
- **Docker** / **docker-compose** para empacotamento e execução local.
- **Langfuse via OpenTelemetry** (`openinference-instrumentation-agno`,
  `opentelemetry-sdk`, `opentelemetry-exporter-otlp`) para observabilidade.
- **structlog** para logging estruturado.
- Qualidade: **ruff** (lint + format), **mypy** (type-check), **bandit** (segurança),
  **pytest** + **pytest-cov** (testes).

## Layout do repositório

```
pyproject.toml
Makefile
Dockerfile                     # empacotamento da API/agente
docker-compose.yml             # orquestração local (API + dependências)
docs/
  architecture.md              # escopo funcional do agente + decisões de arquitetura
  data_dictionary.md           # as 8 tabelas Olist: schema, relações, limitações
  eval_report.md               # relatório de avaliação do agente (qualidade das respostas)
  adrs/                        # Architecture Decision Records (ver TEMPLATE.md)
data/
  raw/olist/                   # CSVs brutos do Olist (não versionados, ver .gitignore)
  warehouse.duckdb             # banco DuckDB gerado por scripts/load_data.py (não versionado)
scripts/
  load_data.py                 # carga dos CSVs brutos no DuckDB
src/data_agent/
  config.py                    # Settings (pydantic-settings) lendo variáveis de ambiente
  db.py                        # conexão e helpers de acesso ao DuckDB
  schemas.py                   # modelos Pydantic (entrada/saída do agente e da API)
  prompts.py                   # system prompts / templates do agente
  agent.py                     # definição do agente Agno e suas tools
  observability.py             # setup de OpenTelemetry / Langfuse
  api.py                       # aplicação FastAPI
  tools/                       # tools de SQL controlado usadas pelo agente
tests/
```

`src/data_agent/` é construído incrementalmente ao longo do cronograma — nem todos os
arquivos listados acima existem desde o Dia 1. Este layout é o alvo; consulte o estado real do
repositório (`ls -R src/`) antes de assumir que um arquivo já existe.

## Como rodar em dev

```bash
uv sync                        # instala dependências (produção + dev)
cp .env.example .env           # preencha com valores reais (nunca commite segredos)
uv run python scripts/load_data.py   # carrega os CSVs de data/raw/olist/ no DuckDB
docker compose up              # sobe o serviço containerizado (quando aplicável)
```

## Variáveis de ambiente

Documentadas em [.env.example](.env.example) à medida que surgem no código. Nunca adicione
uma variável de ambiente sem documentá-la lá, com um comentário dizendo o que ela configura e
se já é consumida por algum código do repositório.

| Variável | Propósito |
|----------|-----------|
| `GROQ_API_KEY` | Chave do provedor de LLM (Groq) usado pelo Agno — ver [ADR-0006](docs/adrs/0006-troca-de-provedor-llm-para-groq.md). |
| `GROQ_TIMEOUT_SECONDS` | Timeout (segundos) da chamada ao modelo de linguagem. |
| `LOG_LEVEL` | Nível de log do structlog (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`). |
| `LANGFUSE_PUBLIC_KEY` | Public key do projeto Langfuse. |
| `LANGFUSE_SECRET_KEY` | Secret key do projeto Langfuse. |
| `LANGFUSE_HOST` | Host do Langfuse que recebe os traces via OTLP — ver [ADR-0007](docs/adrs/0007-observabilidade-com-langfuse.md). |
| `DUCKDB_PATH` | Caminho do arquivo DuckDB do warehouse local. |

## Checklist: antes de alterar / durante / depois de alterar

### Antes de alterar

- Leia [docs/architecture.md](docs/architecture.md) (seção Escopo) se a mudança tocar em
  quais perguntas o agente responde.
- Leia [docs/data_dictionary.md](docs/data_dictionary.md) se a mudança tocar em dados,
  schema ou queries.
- Verifique se existe um ADR relevante em `docs/adrs/` que já justifique (ou restrinja) a
  abordagem que você está prestes a tomar.
- Rode `git status` e confira que não há trabalho em progresso não commitado que sua mudança
  possa sobrescrever.

### Durante

- Siga o layout de pastas descrito acima — não crie módulos fora de `src/data_agent/` para
  código de produção.
- Toda nova variável de ambiente precisa entrar em `.env.example` no mesmo commit que a
  introduz.
- Decisões de arquitetura não triviais (troca de biblioteca, escolha de camada de dados,
  formato de saída do agente, etc.) devem virar um ADR em `docs/adrs/`, usando
  `docs/adrs/TEMPLATE.md`.
- Nunca faça o agente "calcular" ou "estimar" um número — todo valor numérico na resposta
  deve vir de uma consulta real às tabelas do warehouse.

### Depois de alterar

- Rode `make check-quick` (lint + type-check) **no mínimo a cada mudança**, antes de seguir
  para a próxima tarefa.
- Rode `make check` (lint + type-check + security-check + test-coverage) **antes de
  considerar qualquer dia do cronograma concluído**. Corrija tudo que falhar — não avance
  com checagens quebradas.
- Atualize a documentação relevante (`docs/architecture.md`, `docs/data_dictionary.md`,
  `AGENTS.md`) se a mudança alterou escopo, schema de dados ou convenções.
