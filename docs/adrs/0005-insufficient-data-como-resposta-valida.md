# ADR-0005: `insufficient_data` (e `out_of_scope`) como resposta válida em HTTP 200

| Campo      | Valor              |
|------------|--------------------|
| **Status** | Aceito             |
| **Data**   | 2026-09-13         |

## Contexto

`docs/adrs/0004-estrategia-anti-alucinacao.md` já estabelece que `status="insufficient_data"`
e `status="out_of_scope"` são recusas estruturadas e esperadas do agente — não uma falha do
LLM, mas o comportamento correto sempre que a pergunta não pode ser respondida com confiança
a partir dos dados carregados (regra 3 e 4 de `prompts.SYSTEM_PROMPT`). Ao expor isso via
`POST /ask` (`src/data_agent/api.py`) no Dia 4, era preciso decidir como essas duas categorias
de `status` aparecem na API HTTP: como parte do corpo `200` (uma instância normal de
`AgentAnswer`) ou como um código de erro (`4xx`/`5xx`) com um corpo de erro separado.

A tentação mais simples seria mapear `insufficient_data` para algo como `404 Not Found` ("o
dado não foi encontrado") ou `422 Unprocessable Entity` ("a pergunta não pôde ser processada").
Isso pareceria mais "RESTful" à primeira vista, mas confunde duas coisas categoricamente
diferentes: uma falha de infraestrutura que impede o sistema de responder (o modelo não
respondeu a tempo, uma exceção não tratada escapou do agente, o corpo da requisição é
inválido) e uma resposta de produto que o agente deu com sucesso, cujo conteúdo é "não tenho
dado suficiente para responder isso com confiança".

## Decisão

`status="insufficient_data"` e `status="out_of_scope"` são tratados como qualquer outro valor
de `AgentAnswer.status` (junto com `"answered"`): `POST /ask` devolve `200 OK` com o
`AgentAnswer` completo no corpo, validado por `response_model=AgentAnswer`. O cliente da API
decide o que fazer com a recusa (mostrar `answer` ao usuário, logar, agregar em métricas) do
mesmo jeito que decidiria o que fazer com uma resposta `"answered"` — não precisa tratar um
caminho de erro HTTP separado para um comportamento esperado e frequente do produto.

Um código de erro HTTP (`4xx`/`5xx`) em `src/data_agent/api.py` fica reservado para o que
realmente impede o sistema de produzir uma `AgentAnswer` — situações em que não é o agente
decidindo recusar, é a infraestrutura ao redor dele falhando:

- **Corpo da requisição malformado → `422`**: `AskRequest` (`schemas.py`) já rejeita, via
  validação Pydantic do próprio FastAPI, uma requisição sem `question`, com `question` vazia
  (`min_length=1`) ou de tipo errado, antes mesmo de `ask()` rodar.
- **Timeout do modelo → `504`**: `agent.run()` propaga `agno.exceptions.ModelProviderError`
  quando a chamada ao provedor OpenAI falha; `ask()` inspeciona `exc.__cause__` para
  distinguir um `openai.APITimeoutError` (o timeout configurado em
  `Settings.openai_timeout_seconds`, repassado a `OpenAIChat(timeout=...)` em
  `data_agent/agent.py`, estourou) de outro erro de provedor (rate limit, autenticação, erro
  5xx da API) — o primeiro vira `504`, o segundo `502`.
- **Falha inesperada do agente ou de uma tool → `502`**: `tools/sql_tools.py` (`get_schema`,
  `query_sales`) não é chamado diretamente por `ask()` — é o `Agent` do Agno que invoca essas
  tools internamente, e uma exceção que uma tool levanta (`SqlGuardrailError`,
  `QueryTimeoutError`, um erro do DuckDB) normalmente **não** escapa até `api.py`: o próprio
  Agno captura essa exceção dentro da execução da tool, devolve o erro como resultado da
  chamada para o modelo, e é aí que a regra 3 do `SYSTEM_PROMPT` entra em ação — o LLM vê o
  erro e decide retornar `status="insufficient_data"` normalmente, dentro de um `200`. Isso
  não é só uma leitura do código-fonte do Agno (`agno/tools/function.py`): está confirmado por
  teste, com um `Agent` real e as tools reais (não um agente/resultado falso) forçando uma
  exceção de verdade (`FileNotFoundError` de `data_agent/db.py`), em
  `tests/test_agent.py::test_agent_run_when_a_real_tool_raises_does_not_propagate_the_exception`
  — isso transforma a alegação num contrato protegido por regressão, não numa observação
  pontual. O `except Exception` genérico em `ask()` existe como rede de segurança para o caso
  (não esperado, mas não impossível) de uma exceção escapar de qualquer forma dessa camada —
  por exemplo, um bug no Agno, ou o próprio parse de `output_schema` falhando e `agent.run()`
  devolvendo uma `str` crua em vez de uma `AgentAnswer` (comportamento confirmado em
  `tests/test_agent.py::test_agent_run_with_invalid_json_logs_warning_and_returns_raw_string`
  e replicado aqui pela checagem `isinstance(run_output.content, AgentAnswer)`).

Todas essas chamadas (o início e o fim de cada `POST /ask`, e cada execução de
`get_schema`/`query_sales`) são logadas em JSON estruturado via `structlog`
(`_configure_structlog` em `api.py`, chamado na importação do módulo), nunca com
`print`/`logging` básico — isso é o que vai permitir cruzar essas linhas com os traces do
Langfuse no Dia 5. Isso inclui o SQL cru completo executado (`logger.info("query_sales_started",
sql=sql)` em `tools/sql_tools.py`), com literais que vieram indiretamente da pergunta do
usuário (ex.: um nome de vendedor ou uma UF citados na pergunta e usados pelo LLM num `WHERE`).
Isso é aceitável hoje porque o Olist Brazilian E-Commerce Dataset é sintético/anonimizado (ver
`docs/data_dictionary.md`) — não há PII real nas 8 tabelas, então não há dado sensível de
verdade vazando para os logs. **Essa é uma decisão consciente, não uma omissão**: se este
projeto um dia trocar de fonte de dados para algo com PII real (nomes, endereços, dados de
pagamento reais), `query_sales_started`/`query_sales_completed`/`query_sales_failed` em
`tools/sql_tools.py` precisam truncar ou redigir literais de string do SQL antes de logar —
nada nesta base de código detectaria essa mudança de sensibilidade automaticamente.

## Consequências

### Positivas

- Um cliente da API distingue "o agente respondeu, e a resposta é uma recusa" de "o sistema
  falhou" sem precisar inspecionar o corpo de um erro HTTP para descobrir qual dos dois
  aconteceu — o código de status HTTP já significa exatamente uma coisa: `2xx` é "o agente
  rodou e produziu uma `AgentAnswer` válida" (seja ela `answered`, `insufficient_data` ou
  `out_of_scope`), `4xx`/`5xx` é "o sistema não conseguiu nem chegar a esse ponto".
- Reforça, na camada de API, a mesma decisão de produto já registrada em ADR-0004: "não sei" é
  o resultado central do produto (o agente lida com uma base de dados finita e perguntas fora
  do período/escopo são esperadas), não uma exceção. Tratar isso como erro HTTP incentivaria
  quem consome a API a tratar recusas como bug a ser suprimido/retentado, em vez de como
  informação a ser mostrada ao usuário final.
- `tests/test_api.py` verifica isso explicitamente: uma pergunta respondível e uma pergunta
  armadilha (que o agente decide recusar) chegam ambas como `200`, diferindo só no campo
  `status` do corpo — nenhuma delas é tratada como erro pelo teste nem pelo cliente HTTP.

### Negativas / Trade-offs

- Um cliente HTTP ingênuo que só olha o código de status (sem ler o corpo) não percebe, por
  esse sinal isolado, que a pergunta não foi respondida de fato — precisa necessariamente
  inspecionar `AgentAnswer.status` para saber se a resposta é utilizável. Isso é aceito como
  parte do contrato: `response_model=AgentAnswer` documenta isso explicitamente no schema
  OpenAPI gerado pelo FastAPI, e não há hoje um segundo agente/serviço no repositório
  consumindo essa API que precisasse dessa mudança de comportamento.
- A distinção entre "falha real de infraestrutura" (erro HTTP) e "recusa decidida pelo agente"
  (200 com `insufficient_data`/`out_of_scope`) depende de as exceções das tools continuarem
  sendo capturadas pelo Agno antes de chegarem à API — se uma versão futura do Agno mudar esse
  comportamento (deixar de capturar exceções de tools), o `except Exception` genérico em
  `ask()` passaria a devolver `502` para casos que hoje viram `insufficient_data` em `200`,
  silenciosamente mudando esse contrato sem nenhuma mudança de código deste repositório.
