# data-agent

Agente de IA "data-aware" de portfólio que responde perguntas em linguagem natural sobre a
base de vendas Olist Brazilian E-Commerce, consultando dados reais via tool-calling — nunca
inventando números.

## O problema

Um agente de IA que "chuta" números plausíveis quando não sabe a resposta é inútil (ou pior,
perigoso) em produção: uma resposta errada com aparência de confiança é mais difícil de pegar
do que um erro óbvio. Este projeto existe para testar, de ponta a ponta, uma arquitetura onde
isso não pode acontecer por construção — não só por instrução no prompt:

- Toda afirmação numérica precisa vir de uma linha efetivamente devolvida por uma consulta SQL
  real, executada nesta mesma requisição, contra o warehouse (nunca calculada, estimada ou
  "lembrada" pelo modelo).
- Quando os dados não cobrem a pergunta (período fora da janela coberta, entidade inexistente,
  métrica que não existe nas tabelas), o agente recusa explicitamente (`insufficient_data`/
  `out_of_scope`) em vez de aproximar.
- Essa alegação é medida, não só afirmada: `scripts/run_eval.py` roda um dataset de perguntas
  (incluindo armadilhas desenhadas para induzir alucinação) contra a API real e reporta taxa de
  acerto, taxa de alucinação e taxa de recusa indevida — ver [Resultados da avaliação](#resultados-da-avaliação)
  abaixo.

O desenho completo (por que Agno, por que DuckDB com SQL controlado, por que três mecanismos
de anti-alucinação independentes, por que Groq, por que Langfuse) está em
[docs/architecture.md](docs/architecture.md), com o fluxo de ponta a ponta e um diagrama. As
convenções deste repositório (stack, layout, checklist de contribuição) estão em
[AGENTS.md](AGENTS.md).

## Como rodar

Requer Docker e um `data/warehouse.duckdb` gerado localmente. `docker-compose.yml` monta esse
arquivo como volume — como ele já existe no caminho esperado quando o container sobe, o passo
de boot que busca o warehouse no S3 (`data_agent.warehouse_fetch`, ver
[ADR-0010](docs/adrs/0010-hospedagem-do-demo-publico.md)) nem chega a disparar localmente, e as
variáveis `AWS_*`/`S3_*` não precisam estar preenchidas para rodar localmente.

```bash
uv sync                              # instala dependências (produção + dev)
cp .env.example .env                 # preencha GROQ_API_KEY (e, opcionalmente, LANGFUSE_*)
uv run python scripts/load_data.py   # carrega os CSVs de data/raw/olist/ no DuckDB
docker compose up --build            # sobe a API em http://localhost:8000
```

Abra `http://localhost:8000` no navegador para a interface de chat (`static/index.html`,
servida pela própria API — ver [ADR-0010](docs/adrs/0010-hospedagem-do-demo-publico.md)), ou
chame a API diretamente:

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Qual o valor total de vendas em todo o período coberto pela base?"}'
```

`GET /health` expõe um health check simples (usado pelo `HEALTHCHECK` do `Dockerfile` e do
`docker-compose.yml`). Sem `GROQ_API_KEY` real em `.env`, a API sobe normalmente, mas
`POST /ask` falha ao chamar o modelo. `POST /ask` também tem rate limiting por IP (`5/minute`,
via `slowapi`) — devolve `429` acima disso, mesmo localmente.

## Resultados da avaliação

Gerados por `uv run python scripts/run_eval.py` contra `tests/golden_questions.jsonl` (26
perguntas: respondíveis + armadilhas de período fora da janela, entidade inexistente e métrica
fora de escopo). Números completos, por pergunta, em [docs/eval_report.md](docs/eval_report.md).

| Métrica | Valor | Base |
|---|---|---|
| Taxa de acerto | 95,7% | 22/23 |
| Taxa de alucinação | 0,0% | 0/7 (perguntas-armadilha) |
| Taxa de recusa indevida | 0,0% | 0/16 (perguntas respondíveis) |

As três métricas excluem falhas de infraestrutura (rate limit/timeout/erro do provedor Groq —
3/26 nesta execução), que não são atribuíveis ao raciocínio do agente. A metodologia completa,
incluindo o ajuste de `SYSTEM_PROMPT` motivado pela execução anterior (92,3%/10%/0%) e a
investigação de cada mismatch, está em
[ADR-0009](docs/adrs/0009-golden-dataset-e-metricas-de-avaliacao.md).

## Limitações conhecidas

Detalhadas em [docs/data_dictionary.md](docs/data_dictionary.md) (as 8 tabelas, schema,
relações e as armadilhas específicas de cada uma). Resumo:

1. **Sem dados após outubro de 2018** — os pedidos cobrem 04/set/2016 a 17/out/2018; nenhuma
   pergunta sobre período posterior pode ser respondida.
2. **`customer_id` é por pedido, não por pessoa** — contar clientes únicos exige
   `customer_unique_id`.
3. **`geolocation` é aproximada por prefixo de CEP**, sem chave primária e sem relação 1:1 com
   `customers`/`sellers` — não é um geocodificador de endereço exato.
4. **~3% dos pedidos não têm data de entrega ao cliente preenchida** — métricas de prazo de
   entrega só devem considerar pedidos efetivamente entregues.
5. **~0,6% dos produtos não têm categoria preenchida.**
6. **`review_id` sozinho não é chave única** — a chave real é (`review_id`, `order_id`).
7. **O texto livre de reviews não é usado como fonte de verdade quantitativa** pelo agente (ver
   [Escopo](docs/architecture.md#escopo) em `docs/architecture.md`).

Também fora de escopo por definição do produto (não por limitação técnica de dado): previsão/
forecasting, dados externos ao dataset Olist, geolocalização de precisão e qualquer ação de
escrita (o agente é somente leitura) — ver a seção "Fora de escopo" em
[docs/architecture.md](docs/architecture.md).

## Deploy público (Render)

Passo a passo para publicar este demo no [Render](https://render.com) a partir do
`Dockerfile` existente (mesma imagem usada localmente — nenhum segundo serviço, ver
[ADR-0010](docs/adrs/0010-hospedagem-do-demo-publico.md) para o porquê do Render em vez de
Railway/Fly.io/Hugging Face Spaces, e o raciocínio dos guard-rails abaixo).

1. **Crie o bucket S3 e o usuário/role IAM, e envie o warehouse — uma vez, fora do Render.** O
   plano free do Render não tem disco persistente, e `data/warehouse.duckdb` (~140MB) nunca é
   commitado (`.gitignore`) — `data_agent.warehouse_fetch` baixa esse arquivo de um bucket S3
   privado no **boot do container** (não durante o build — ver
   [ADR-0010](docs/adrs/0010-hospedagem-do-demo-publico.md), seção "Atualização 2", para o
   porquê de runtime em vez de build-time). Isto é feito uma única vez, direto no console/CLI
   da AWS, não por este repositório:
   1. Crie um bucket S3 **privado** (bloqueie todo acesso público).
   2. Crie um usuário (ou role) IAM só com permissão de leitura no objeto do warehouse — anexe
      esta policy (troque `SEU_BUCKET` pelo nome real do bucket):
      ```json
      {
        "Version": "2012-10-17",
        "Statement": [
          {
            "Sid": "ReadOlistWarehouseObject",
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::SEU_BUCKET/warehouse.duckdb"
          }
        ]
      }
      ```
   3. Gere uma access key para esse usuário e envie o arquivo:
      ```bash
      uv run python scripts/load_data.py   # gera data/warehouse.duckdb, se ainda não existir
      aws s3 cp data/warehouse.duckdb s3://SEU_BUCKET/warehouse.duckdb --region SUA_REGIAO
      ```
2. **(Opcional) valide localmente antes do Render**: preencha
   `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_REGION`/`S3_BUCKET_NAME`/`S3_OBJECT_KEY` no
   seu `.env` (ver [.env.example](.env.example)), remova/comente o volume de
   `data/warehouse.duckdb` em `docker-compose.yml` (ou renomeie o arquivo local) para forçar o
   fetch do S3 a disparar, e rode `docker compose up --build` — isso prova que a credencial IAM
   tem a permissão certa antes de repetir o mesmo preenchimento no Render.
3. **Crie o Web Service**: no painel do Render, "New +" → "Web Service" → conecte este
   repositório. O Render detecta `render.yaml` na raiz e propõe o blueprint (runtime Docker,
   `Dockerfile` na raiz, plano free, health check em `/health`) — revise e confirme.
4. **Configure as variáveis de ambiente como secrets no painel do Render — nunca commitadas**:
   `render.yaml` marca `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`,
   `S3_BUCKET_NAME`, `GROQ_API_KEY`, `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY` com
   `sync: false`, o que faz o Render pedir esses valores na criação do serviço em vez de aceitar
   algum valor deste repositório. Preencha com os mesmos valores do seu `.env` local. Estas são
   env vars normais do serviço (runtime), não build args — o `Dockerfile` não precisa mais
   delas para buildar a imagem.
5. **Deploy**: o Render builda o `Dockerfile` (sem tocar no S3 — o fetch só acontece quando o
   container sobe) e inicia o serviço; no boot, `data_agent.warehouse_fetch` baixa o warehouse
   usando as variáveis do passo anterior antes do `uvicorn` subir — acompanhe os logs até o
   health check em `/health` ficar verde. A URL pública
   (`https://<nome-do-serviço>.onrender.com`) já serve tanto a interface de chat (`/`) quanto a
   API (`/ask`). Se o boot falhar, o log mostra exatamente por quê (variável ausente ou erro do
   S3 — ver `data_agent/warehouse_fetch.py`); o container não chega a responder `/health`, então
   não existe um estado "no ar mas quebrado" nesse cenário.
6. **Sobre proteção de custo: este projeto usa o tier gratuito da Groq deliberadamente.**
   "Spend Limits" (teto de gasto real) exige tier pago da Groq e não está disponível aqui — o
   tier gratuito não gera cobrança monetária, só nega a requisição quando a cota de uso
   (RPM/TPM/TPD) é excedida, funcionando como proteção de custo por construção. O rate
   limiting por IP em `POST /ask` (`5/minute`, ver
   [ADR-0010](docs/adrs/0010-hospedagem-do-demo-publico.md), seção "Atualização 3") é a segunda
   camada real, reduzindo o quanto um único visitante/bot esgota essa cota compartilhada — mas
   não impede abuso distribuído. Se este projeto migrar para tier pago no futuro, configurar um
   "Spend Limit" real volta a ser um pré-requisito antes de divulgar o link de novo.

O plano free do Render "dorme" o serviço após um período de inatividade — a UI já avisa
visivelmente que a primeira resposta pode levar até 1 minuto (cold start) enquanto o container
sobe de novo. **O que sabemos e o que não sabemos sobre esse cold start desde a mudança para
fetch em runtime:**
- **Sabemos** que o plano free do Render não tem disco persistente — qualquer arquivo escrito
  pelo processo em execução (como o warehouse baixado) não sobrevive a menos que o mesmo
  container continue rodando.
- **Não sabemos**, a partir deste ambiente de desenvolvimento, se o Render *reaproveita a mesma
  instância de container* ao acordar de um período "dormindo" (nesse caso o warehouse já
  baixado sobreviveria) ou se *provisiona um container novo a cada wake-up* (nesse caso todo
  wake-up refaz o download do S3, somando esse tempo ao cold start já lento). Assuma o pior
  caso (novo download a cada wake-up) até confirmar o comportamento real — a UI já avisa "até 1
  minuto" com essa margem em mente.

## Decisões de arquitetura (ADRs)

| ADR | Decisão |
|---|---|
| [0001](docs/adrs/0001-escolha-do-agno.md) | Uso do Agno como framework de agente |
| [0002](docs/adrs/0002-camada-de-dados.md) | DuckDB local com SQL controlado como camada de dados |
| [0003](docs/adrs/0003-sql-controlado-vs-tools-granulares.md) | SQL controlado e genérico vs. tools granulares por métrica |
| [0004](docs/adrs/0004-estrategia-anti-alucinacao.md) | Estratégia anti-alucinação (grounding + escopo documentado + recusa estruturada) |
| [0005](docs/adrs/0005-insufficient-data-como-resposta-valida.md) | `insufficient_data` (e `out_of_scope`) como resposta válida em HTTP 200 |
| [0006](docs/adrs/0006-troca-de-provedor-llm-para-groq.md) | Troca do provedor de LLM de OpenAI para Groq |
| [0007](docs/adrs/0007-observabilidade-com-langfuse.md) | Observabilidade com Langfuse via OpenTelemetry |
| [0008](docs/adrs/0008-reprodutibilidade-com-uv-lock.md) | Reprodutibilidade de build via `uv.lock` e `uv sync --frozen` |
| [0009](docs/adrs/0009-golden-dataset-e-metricas-de-avaliacao.md) | Dataset golden com perguntas-armadilha como critério de sucesso |
| [0010](docs/adrs/0010-hospedagem-do-demo-publico.md) | Hospedagem do demo público no Render, UI estática na mesma imagem, guard-rails de custo/abuso |

## Desenvolvimento

Convenções completas (stack, layout do repositório, variáveis de ambiente, checklist antes/
durante/depois de alterar) em [AGENTS.md](AGENTS.md). Resumo rápido:

```bash
make check-quick   # lint + type-check — a cada mudança
make check         # lint + type-check + security-check + test-coverage — antes de considerar
                    # um dia do cronograma concluído
```
