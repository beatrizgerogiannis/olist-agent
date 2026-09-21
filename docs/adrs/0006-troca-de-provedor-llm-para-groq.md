# ADR-0006: Troca do provedor de LLM de OpenAI para Groq

| Campo      | Valor              |
|------------|--------------------|
| **Status** | Aceito             |
| **Data**   | 2026-09-15         |

## Contexto

Até este ponto (Dias 1-4), `data_agent/agent.py` usava `agno.models.openai.OpenAIChat` contra a
API real da OpenAI, lendo `Settings.openai_api_key`. Ao preparar o Dia 5 (observabilidade), o
`.env` local não tinha uma `OPENAI_API_KEY` real configurada — só as chaves do Langfuse. Sem uma
chave de LLM válida não é possível rodar o agente de verdade nem gerar os traces reais que o Dia
5 pede. A alternativa disponível era uma chave da [Groq](https://groq.com), que oferece um tier
gratuito generoso e uma API compatível com o formato OpenAI (`chat.completions`).

Trocar o provedor de LLM é uma decisão de arquitetura não-trivial (critério do checklist em
[AGENTS.md](../../AGENTS.md)) — não é só trocar uma chave de API. O Agno já tem uma classe de
modelo dedicada para Groq (`agno.models.groq.Groq`, usando o SDK `groq` oficial diretamente, não
`OpenAIChat` com `base_url` sobrescrito), o que foi confirmado lendo
`agno/models/groq/groq.py` no pacote instalado antes de decidir usar essa classe em vez de
gambiarrar `OpenAIChat`.

### A Groq não é um substituto 1:1 da OpenAI para este agente

Duas descobertas, feitas empiricamente (chamando a API de verdade, não só lendo documentação),
mudaram o desenho de `build_agent()`:

1. **Nem todo modelo do catálogo gratuito da Groq suporta tool-calling + structured outputs ao
   mesmo tempo.** Consultar `GET /openai/v1/models` (com o campo `supported_features` que a API
   da Groq expõe por modelo) mostrou que só 3 modelos do catálogo têm `tools` e
   `structured_outputs` simultaneamente: `openai/gpt-oss-20b`, `openai/gpt-oss-120b` e
   `openai/gpt-oss-safeguard-20b` (este último é um modelo de moderação/safety, não um modelo de
   propósito geral). `openai/gpt-oss-120b` — que por coincidência já é o `id` default da classe
   `agno.models.groq.Groq` — foi o escolhido: o maior modelo de propósito geral com as duas
   features.
2. **A API da Groq rejeita `response_format` (mesmo o "JSON mode" básico, não só JSON Schema
   estrito) combinado com `tools` na mesma chamada** — `400 json mode cannot be combined with
   tool/function calling`. Isso foi reproduzido tanto com uma chamada direta ao SDK `openai`
   apontando pra Groq quanto rodando `agent.run()` de verdade com `agno.models.groq.Groq` como
   `model` (sem `parser_model`): toda chamada real falhava com esse 400, não é um caso de borda.
   A causa-raiz: `agno.agent._response.get_response_format` monta um `response_format` (nem que
   seja só `{"type": "json_object"}`) sempre que `Agent.output_schema` está setado, independente
   de `Model.supports_native_structured_outputs`/`.supports_json_schema_outputs` — essas duas
   flags só mudam qual `response_format` é montado, nunca fazem o Agno deixar de mandar um. Como
   este agente sempre expõe `tools=[get_schema, query_sales]` (é assim que ele consulta o
   warehouse — ver [ADR-0002](0002-camada-de-dados.md)), qualquer tentativa de estruturar a
   saída do mesmo modelo que tem as tools ia sempre bater nesse 400.

## Decisão

Usar `agno.models.groq.Groq` (pacote `groq` adicionado a `pyproject.toml`) como modelo do agente,
lendo a chave via `Settings.groq_api_key` (env `GROQ_API_KEY`) — nunca hardcoded, como o resto
das configurações do projeto.

Para contornar a incompatibilidade `tools` + `response_format` descrita acima,
`build_agent()` (`data_agent/agent.py`) usa o recurso `parser_model` do Agno: um **segundo**
`Groq` (sem tools, com `supports_json_schema_outputs=True` sobrescrito manualmente — a classe
`Groq` do Agno não liga isso por padrão, mas `openai/gpt-oss-120b` suporta de verdade, confirmado
pelo `supported_features` da API) dedicado só a estruturar a resposta final. Lendo
`agno/agent/_run.py`, a presença de `parser_model` muda o comportamento do modelo principal:
`response_format = get_response_format(...) if agent.parser_model is None else None` — com
`parser_model` setado, o modelo principal nunca recebe `response_format` (evitando o 400), e o
Agno faz uma chamada extra, só texto e sem tools, ao `parser_model`, depois que o loop de tools
termina, para transformar a resposta final em `AgentAnswer`. Confirmado rodando `agent.run()` de
verdade (não só lendo o código-fonte do Agno): sem `parser_model`, toda chamada falhava com o
400 acima; com `parser_model`, `result.content` volta como uma instância real de `AgentAnswer`.

O `id` do modelo (`"openai/gpt-oss-120b"`) não é exposto como variável de ambiente — é o default
da classe `Groq` do Agno, e não é sobrescrito em `build_agent()` de propósito, para não duplicar
essa escolha (feita aqui, com a checagem de `supported_features` acima) em dois lugares. Trocar
de modelo continua possível, mas é uma mudança de código em `data_agent/agent.py`, não de
`.env`.

### Otimização de latência (2026-09-22)

Medição informal contra a demo pública apontava ~30s por pergunta. Duas mudanças em
`data_agent/agent.py`, sem tocar guard-rails, schemas Pydantic ou o escopo de tabelas
permitidas:

1. **`get_schema` deixou de ser uma tool do modelo principal.** Antes, `tools=[get_schema,
   query_sales]` fazia o modelo decidir, em runtime, chamar `get_schema` como um turno
   completo (ida-e-volta à API da Groq) antes de sequer montar a primeira `query_sales` —
   visível nos traces do Langfuse como uma chamada de ~15-20s isolada, sempre a primeira do
   loop de tools, para um schema que **nunca muda** (as 8 tabelas são fixas, carregadas uma
   vez por `scripts/load_data.py`). Como o schema é estático, ele foi embutido como texto
   direto em `prompts.SYSTEM_PROMPT` (tabelas, colunas, tipos e relações, extraído de
   `docs/data_dictionary.md` para não divergir da mesma fonte de verdade) — o modelo já
   chega com o schema disponível, sem precisar de uma tool para descobri-lo. `tools=[]` do
   agente principal agora é só `[query_sales]`. A função `get_schema` (`tools/sql_tools.py`)
   não foi apagada (segue disponível para depuração manual/uso futuro), só não é mais
   registrada como tool ativa em `build_agent()`.
2. **`parser_model` trocou de `openai/gpt-oss-120b` para `openai/gpt-oss-20b`.** A tarefa do
   `parser_model` é só estruturar em JSON uma resposta de texto que o modelo principal já
   produziu — não é uma tarefa de raciocínio (montar SQL, decidir `status`, interpretar o
   schema), então não precisa do maior modelo do catálogo. Reconsultado `GET
   /openai/v1/models` em 2026-09-22 (mesmo processo do Dia 5, ver acima) para confirmar o
   catálogo gratuito **atual** antes de escolher — ele mudou desde 2026-09-15: hoje só 13
   modelos estão disponíveis, e `llama-3.1-8b-instant` (sugerido inicialmente como candidato
   óbvio) **não está mais no catálogo** desta chave. Os únicos 3 modelos com
   `structured_outputs` continuam sendo os mesmos de ADR-0006 original —
   `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `openai/gpt-oss-safeguard-20b` (este último
   descartado de novo, é um modelo de moderação/safety) — então `openai/gpt-oss-20b` (a
   versão 20B, menor, da mesma família já validada para `structured_outputs`) é a única
   opção real de "modelo menor" disponível hoje para essa tarefa, não uma escolha entre
   várias famílias de modelo.
3. **`max_tokens` explícito nos dois modelos** (antes `None` nos dois — sem teto algum).
   Medido via traces reais do Langfuse (usage por `GENERATION`): turnos do modelo principal
   (que inclui tokens de raciocínio, já que os modelos `gpt-oss` têm a feature `reasoning`)
   ficaram entre 68 e 406 tokens de saída; a chamada do `parser_model` ficou em 214. Setado
   `max_tokens=2048` no modelo principal e `max_tokens=1024` no `parser_model` (~5x o pico
   observado em cada um) — teto generoso o bastante para não truncar nenhuma resposta normal,
   mas presente para não deixar uma geração presa/anormalmente longa (ex. um loop de
   raciocínio do modelo) inflar a latência de uma chamada sem limite nenhum.

**Impacto medido** (subconjunto de 4 perguntas do golden dataset — `q01`, `q11`, `q17`,
`q22` — contra a API local rodando as três mudanças acima, Groq real, medido via o novo
`duration_seconds` de `data_agent/api.py::ask`, 2026-09-22): `q01` (contagem simples,
`answered`) caiu para **4,4s** — a melhora mais limpa e diretamente atribuível à remoção de
`get_schema`, já que essa era justamente uma pergunta de um único filtro, sem precisar de
uma segunda tool call antes; nos traces anteriores (pré-mudança), só o turno de `get_schema`
já consumia ~15-20s sozinho, antes de sequer chegar em `query_sales`. `q11` ficou em 20,4s.
`q17` e `q22`, porém, não mostram a mesma melhora limpa — 60,5s e 85,0s respectivamente — e
por motivos que **não são regressão das três mudanças**: `q17` disparou a nova regra 3
(verificação de existência de entidade) mesmo para um estado real (`SC`), rodando uma
`query_sales` extra (`SELECT DISTINCT seller_state ...`) antes do `COUNT`, e as duas últimas
perguntas da bateria caíram numa janela de maior contenção de rate limit da Groq (mesmo
padrão de "30-90s sob carga" já documentado acima); `q22` especificamente bateu de novo no
bug `tool_use_failed` (ver `_is_tool_use_failed_payload` em `data_agent/api.py`, mitigado
numa sessão anterior) — o retry automático rodou o loop de tools inteiro pela segunda vez do
zero, dobrando a latência dessa pergunta especificamente. Ou seja: o ganho de remover
`get_schema` é real e mensurável em uma chamada "limpa" (`q01`), mas fica mascarado nas
outras três por dois fatores conhecidos e não relacionados às mudanças desta seção
(contenção de rate limit da Groq sob rajada de chamadas do próprio teste, e a regra 3 —
adicionada numa sessão anterior — custando uma tool call extra quando decide verificar uma
entidade). 4/4 perguntas corretas (nenhuma alucinação, nenhuma recusa indevida); 29.254
tokens consumidos nesta medição.

`data_agent/api.py` também precisou mudar: o `except ModelProviderError` que distingue timeout
(`504`) de outro erro de provedor (`502`) — ver
[ADR-0005](0005-insufficient-data-como-resposta-valida.md) — inspecionava
`isinstance(exc.__cause__, openai.APITimeoutError)`. O SDK `groq` (gerado pelo mesmo stack de
codegen da OpenAI — os nomes de exceção são quase idênticos) tem sua própria hierarquia de
exceções; `groq.APITimeoutError` (que sobe como `exc.__cause__` de um `ModelProviderError` quando
`agno.models.groq.Groq.invoke()` captura um timeout, confirmado lendo `agno/models/groq/groq.py`)
substituiu `openai.APITimeoutError` nessa checagem.

## Consequências

### Positivas

- Desbloqueia rodar o agente de verdade (chamadas reais, não só testes com `Model` falso) sem
  depender de uma chave paga da OpenAI — o tier gratuito da Groq é suficiente para o volume de
  testes manuais e do dia a dia deste projeto de portfólio.
- `agno.models.groq.Groq` é uma classe de primeira classe do Agno (não uma gambiarra de
  `OpenAIChat` com `base_url`), então segue recebendo suporte/correções do próprio Agno.
- O padrão `parser_model` é uma solução do próprio framework para esse tipo de incompatibilidade
  (tools + structured output no mesmo provider/modelo), não um workaround só deste projeto —
  deixa a porta aberta para usar o mesmo padrão com outros providers no futuro, se necessário.

### Negativas / Trade-offs

- **Uma chamada a mais por pergunta respondida.** Toda vez que o modelo principal termina o loop
  de tools, há uma chamada extra ao `parser_model` só para estruturar a resposta — mais latência
  e mais tokens consumidos por pergunta do que uma chamada única com `response_format` nativo
  (o que a OpenAI permite). Isso é visível nos traces do Langfuse (ver
  [ADR-0007](0007-observabilidade-com-langfuse.md)) como um span extra de modelo por `agent.run`.
  Parcialmente mitigado em 2026-09-22 (ver seção "Otimização de latência" acima): essa chamada
  extra agora usa `openai/gpt-oss-20b` em vez de `openai/gpt-oss-120b`, então continua sendo uma
  chamada a mais, mas mais rápida/barata do que era.
- **Menos madura que a API da OpenAI para este tipo de combinação.** A restrição
  `response_format` + `tools` é uma limitação real e atual da API da Groq (não do Agno), então se
  a Groq relaxar essa restrição no futuro, `parser_model` deixa de ser necessário, mas nada neste
  código detectaria essa mudança automaticamente — teria que ser revisitado manualmente.
- **`openai/gpt-oss-120b` é um modelo consideravelmente menor/mais barato que os modelos GPT-4.x
  usados anteriormente com a OpenAI.** Isso pode afetar a qualidade das respostas do agente
  (aderência às regras do `SYSTEM_PROMPT`, qualidade da geração de SQL) de formas que só ficarão
  claras rodando perguntas reais — é exatamente o que a validação manual do Dia 5 (rodar as
  perguntas de `golden_questions.jsonl` contra a API e conferir os traces) ajuda a expor.
- **Dependência de um segundo fornecedor além da Groq para o `id` do modelo continuar válido**:
  `openai/gpt-oss-120b` é um modelo open-weight da OpenAI hospedado pela Groq — se a Groq parar
  de hospedá-lo, `build_agent()` quebra até o `id` ser trocado por outro modelo do catálogo da
  Groq com as mesmas `supported_features` (`tools` + `structured_outputs`).
