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
