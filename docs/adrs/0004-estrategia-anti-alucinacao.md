# ADR-0004: Estratégia anti-alucinação (grounding + escopo documentado + recusa estruturada)

| Campo      | Valor              |
|------------|--------------------|
| **Status** | Aceito             |
| **Data**   | 2026-09-13         |

## Contexto

`docs/architecture.md` já deixa explícito que "o agente nunca deve inventar números" e
que, "quando a base não tem dado suficiente para responder com confiança, o agente
deve dizer isso explicitamente em vez de estimar". Até o Dia 2, isso era só uma
intenção documentada: `tools/sql_tools.py` e `tools/guardrails.py` garantem que
qualquer SQL executado é seguro e restrito às 8 tabelas do Olist, mas nada ainda
impedia o LLM de, no Dia 3, ignorar o resultado das tools e responder com um número
"chutado" — ou de devolver texto livre inconsistente ("acho que não tenho certeza,
mas talvez...") em vez de uma recusa que o resto do sistema (API, testes, avaliação)
consiga tratar de forma programática.

A tentação mais simples seria resolver isso só no texto do system prompt, pedindo
"seja honesto, não invente números". Isso não é confiável sozinho: um LLM pode seguir
essa instrução na maioria dos casos e ainda assim, ocasionalmente, preencher uma
lacuna com um valor plausível (é exatamente o comportamento que a instrução tenta
evitar, e nada no restante do sistema detectaria a falha se ela acontecer).

## Decisão

Combinar três mecanismos independentes, de forma que cada um cubra o ponto onde os
outros dois podem falhar:

1. **Grounding obrigatório nas tools** (`src/data_agent/agent.py` +
   `src/data_agent/prompts.py`): o `Agent` do Agno só tem acesso a `get_schema` e
   `query_sales` (`tools/sql_tools.py`) como fonte de dados, e `SYSTEM_PROMPT` proíbe
   explicitamente qualquer valor numérico na resposta que não venha literalmente de
   uma linha devolvida por `query_sales` nesta execução — calcular, estimar ou
   "lembrar" um número de treinamento é tratado como regra não-negociável, na mesma
   categoria de gravidade que inventar o número diretamente.
2. **`docs/data_dictionary.md` como fonte de verdade sobre "fora de escopo"**: o
   prompt referencia o dicionário de dados (período coberto, `customer_id` vs.
   `customer_unique_id`, limitações de `geolocation`, `order_reviews`, etc.) para que
   a decisão de recusar não dependa do juízo do LLM sobre o que "parece" fora de
   escopo — é uma lista fechada e versionada no repositório, a mesma usada para
   escrever os `tests/golden_questions.jsonl`.
3. **Recusa estruturada via campo `status`, garantida pelo `response_format` da API
   do modelo — não por retry do Agno** (`src/data_agent/schemas.py` +
   `output_schema=AgentAnswer` em `agent.py`): o enforcement real de hoje vem de
   `OpenAIChat.supports_native_structured_outputs=True`, que faz o Agno enviar o
   JSON Schema de `AgentAnswer` como `response_format` estrito na própria chamada à
   API da OpenAI — é a API do modelo que recusa gerar algo fora desse schema, antes
   mesmo da resposta voltar para o processo do agente. O papel do Agno aqui
   (`convert_response_to_structured_format` em `agno/agent/_response.py`) é só
   fazer o parse do JSON devolvido para uma instância de `AgentAnswer`; **não há
   validação com retry no lado do Agno** — se esse parse falhar (ex.: um provider
   sem structured outputs nativos, ou uma resposta que a API não conseguiu conter
   no schema), o Agno só loga um warning e deixa o conteúdo cru (string) passar
   para quem chamou o agente, sem levantar exceção (`Agent.retries` tem default
   `0` e `build_agent` não configura isso). Esse comportamento está confirmado por
   teste, não só por leitura do código-fonte do Agno — ver
   `tests/test_agent.py::test_agent_run_with_invalid_json_logs_warning_and_returns_raw_string`.

## Consequências

### Positivas

- Os três mecanismos falham de formas diferentes, então um erro em um deles não
  necessariamente produz uma alucinação: se o LLM ignorar a regra de grounding do
  prompt (mecanismo 1) mas tentar retornar `status="answered"` sem ter chamado
  `query_sales` nesta execução, isso ainda pode ser auditado depois via `sql_used`
  vazio — um `answered` com `sql_used == []` é um sinal de falha detectável
  programaticamente.
- `tests/golden_questions.jsonl` já existe com 10 perguntas e `expected_status`
  definido — as 5 respondíveis têm `expected_value` conferido manualmente contra o
  warehouse real (`data/warehouse.duckdb`) antes de serem gravadas no arquivo. Hoje
  isso é só um dataset estático: nada no repositório o consome ainda (não há
  `scripts/run_eval.py` nem lógica que rode essas perguntas contra o agente). A
  malha de avaliação automatizada — um script que executa o agente de verdade para
  cada pergunta e compara `expected_status`/`expected_value` contra a resposta real,
  com o resultado agregado em `docs/eval_report.md` — é trabalho planejado para o
  Dia 6 do cronograma, não uma camada ativa desta estratégia hoje.
- `status` como campo estruturado (em vez de o agente "decidir" recusar em texto
  livre) permite que qualquer consumidor da API (frontend, avaliação automatizada,
  logging) trate `insufficient_data`/`out_of_scope` de forma programática, sem
  precisar fazer parsing de linguagem natural para detectar uma recusa.
- `docs/data_dictionary.md` já existia como documentação; reaproveitá-lo como fonte
  de verdade do prompt evita duplicar (e divergir) a lista de limitações em dois
  lugares.
- Separar `SYSTEM_PROMPT` em `prompts.py` (em vez de inline em `agent.py`) permite
  versionar mudanças de regras de recusa isoladamente e referenciá-las a partir dos
  golden questions sem reler o código de wiring do agente.

### Negativas / Trade-offs

- Nenhum dos três mecanismos impede o LLM de gerar SQL sintaticamente válido mas
  semanticamente errado (ex. um `JOIN` errado que ainda devolve linhas) e reportar
  `status="answered"` com um número tecnicamente "vindo de uma tool", porém incorreto
  — isso já era uma limitação conhecida de ADR-0003 e esta estratégia não a resolve.
  Isso só será efetivamente checado quando a malha de avaliação automatizada (Dia 6)
  existir; até lá, `tests/golden_questions.jsonl` é só um gabarito manual, não uma
  checagem executável contra o agente.
- A distinção entre `insufficient_data` (dado não coberto/zero linhas/erro de tool) e
  `out_of_scope` (tipo de pergunta que o agente não responde por definição) depende
  de o LLM aplicar corretamente a regra do prompt — é um julgamento semântico, não uma
  validação sintática como a do `Literal`. `tests/golden_questions.jsonl` documenta o
  `expected_status` esperado para os casos conhecidos, mas, enquanto não existir a
  malha de avaliação do Dia 6 que rode o agente de verdade contra esse arquivo, essa
  expectativa não é verificada automaticamente — e a fronteira entre as duas
  categorias pode ficar ambígua em perguntas novas de qualquer forma.
- O mecanismo 3 depende do provedor de modelo suportar structured outputs nativos
  (`supports_native_structured_outputs=True`, hoje verdadeiro para `OpenAIChat`). Se
  `agent.py` trocar para um modelo sem esse suporte, a garantia de schema some
  silenciosamente: passa a depender só do parse best-effort do Agno (que já
  confirmamos falhar sem erro) e de quanto o `SYSTEM_PROMPT` sozinho consegue induzir
  JSON válido — nada no repositório detectaria essa perda de proteção.
- `output_schema=AgentAnswer` garante a *forma* da resposta (os campos e tipos
  corretos), não a sua *veracidade* — um `AgentAnswer` com `status="answered"` e um
  número errado, mas com `sql_used` e `sources` preenchidos de forma consistente,
  ainda passa na validação Pydantic. A validação estrutural é uma camada a mais, não
  uma substituta para avaliação de corretude do conteúdo.
- Manter `docs/data_dictionary.md` como fonte de verdade do prompt cria acoplamento:
  qualquer mudança nas limitações documentadas (ex. carregar uma versão mais recente
  do dataset Olist) exige revisar se `SYSTEM_PROMPT` e `tests/golden_questions.jsonl`
  ainda refletem a realidade dos dados.
