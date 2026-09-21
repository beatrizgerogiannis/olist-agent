"""Definição do Agent do Agno (ver docs/adrs/0004-estrategia-anti-alucinacao.md)."""

from __future__ import annotations

from agno.agent import Agent
from agno.models.groq import Groq

from data_agent.config import get_settings
from data_agent.prompts import SYSTEM_PROMPT
from data_agent.schemas import AgentAnswer
from data_agent.tools.sql_tools import query_sales

# Tetos de `max_tokens` por chamada — nenhum dos dois modelos tinha um limite
# configurado antes (o default do Agno é `None`, sem teto algum). Valores
# escolhidos a partir de uso real observado via traces do Langfuse (ver
# docs/adrs/0006-troca-de-provedor-llm-para-groq.md, seção "Latência"): turnos do
# modelo principal (incluindo raciocínio dos modelos `gpt-oss`, que têm a feature
# `reasoning`) ficaram entre 68 e 406 tokens de saída; a chamada do `parser_model`
# (só estrutura um `AgentAnswer` já pronto) ficou em 214. Os dois valores abaixo dão
# margem generosa (~5x o pico observado) sem deixar nenhum dos dois modelos gerar
# sem teto — o risco que motivou revisar isso (item 3 do pedido de otimização de
# latência) era justamente uma geração presa/excessivamente longa inflar a latência
# de uma chamada sem nenhum limite superior.
_MAIN_MODEL_MAX_TOKENS = 2048
_PARSER_MODEL_MAX_TOKENS = 1024

# Modelo do `parser_model`: menor que o modelo principal de propósito — ver
# docstring de `build_agent` e docs/adrs/0006-troca-de-provedor-llm-para-groq.md,
# seção "Modelo do parser_model (2026-09-22)".
_PARSER_MODEL_ID = "openai/gpt-oss-20b"


def build_agent() -> Agent:
    """Monta o Agent do Agno com grounding obrigatório e saída estruturada.

    O modelo principal é o ``Groq`` do Agno (ver
    docs/adrs/0006-troca-de-provedor-llm-para-groq.md), cujo default de
    ``id`` (``"openai/gpt-oss-120b"``) não é sobrescrito aqui de propósito — é o
    único modelo do catálogo gratuito da Groq confirmado (ver ADR-0006) a suportar
    tool-calling e ``structured_outputs`` ao mesmo tempo, e sobrescrever o default do
    Agno aqui duplicaria essa escolha em dois lugares.

    ``tools=[query_sales]`` — antes também incluía ``get_schema``, removido em
    2026-09-22 (otimização de latência): o schema das 8 tabelas é estático (não
    muda em runtime) e agora está embutido em ``prompts.SYSTEM_PROMPT`` como texto,
    então não há mais motivo para o modelo gastar um turno inteiro de ida-e-volta
    à Groq só para descobrir o schema antes de poder montar a primeira query real —
    isso eliminava uma chamada completa ao modelo (e o RTT de rede correspondente)
    em toda pergunta respondível, medido nos traces do Langfuse em ~15-20s por
    pergunta (ver ADR-0006). A função ``get_schema`` continua existindo em
    ``tools/sql_tools.py`` (não foi apagada) para depuração manual/uso futuro, só
    não é mais exposta como tool ativa aqui.

    ``parser_model`` (um segundo ``Groq``, sem tools, com
    ``supports_json_schema_outputs=True``) existe porque a API da Groq rejeita
    combinar `response_format` (mesmo o "JSON mode" básico, não só JSON Schema
    estrito) com `tools` na mesma chamada — ``400 json mode cannot be combined
    with tool/function calling`` — e este agente sempre expõe tools ao modelo
    principal. Isso não é uma limitação do `Groq.supports_native_structured_outputs`
    (que continua `False`, o default herdado de `agno.models.base.Model`): mesmo
    com essa flag desligada, `agno.agent._response.get_response_format` ainda monta
    um `response_format` básico (`{"type": "json_object"}`) sempre que
    `output_schema` está setado — não existe um modo "sem response_format nenhum"
    no modelo principal enquanto ele tiver tools, a não ser delegando a
    estruturação a um `parser_model` separado. Com `parser_model` setado, o Agno
    (`agno/agent/_run.py`, `response_format = get_response_format(...) if
    agent.parser_model is None else None`) para de mandar `response_format` na
    chamada com tools e faz uma chamada extra, só texto → `AgentAnswer`, depois que
    o loop de tools termina — foi assim, empiricamente (a combinação `model` sem
    `parser_model` falha com o 400 acima em todo `agent.run()` real, não só em
    teoria), que essa escolha foi validada durante o ADR-0006, não só lendo o
    código-fonte do Agno. Desde 2026-09-22, ``parser_model`` usa ``openai/gpt-oss-20b``
    (não o mesmo ``openai/gpt-oss-120b`` do modelo principal) — sua tarefa é só
    estruturar uma resposta de texto já pronta em JSON, uma tarefa mecânica que não
    precisa do modelo maior; ver ADR-0006 para a checagem de `supported_features`
    que confirmou esse modelo (o único menor que ``-120b`` com
    ``structured_outputs`` no catálogo gratuito atual da Groq).

    O parse do ``parser_model`` ainda pode falhar (JSON malformado, resposta que não
    bate com o schema); quando isso acontece, o Agno não levanta exceção nem tenta
    de novo — só loga um warning e deixa a string crua passar (``Agent.retries`` é
    ``0`` por padrão e não é configurado aqui; comportamento confirmado em
    ``tests/test_agent.py::test_agent_run_with_invalid_json_logs_warning_and_returns_raw_string``).
    Combinado com as regras de recusa em ``prompts.SYSTEM_PROMPT`` e a tool de SQL
    controlado, isso é um dos três mecanismos anti-alucinação descritos em
    docs/adrs/0004-estrategia-anti-alucinacao.md.
    """
    settings = get_settings()
    api_key = settings.groq_api_key or None
    timeout = int(settings.groq_timeout_seconds)
    return Agent(
        model=Groq(api_key=api_key, timeout=timeout, max_tokens=_MAIN_MODEL_MAX_TOKENS),
        tools=[query_sales],
        output_schema=AgentAnswer,
        instructions=SYSTEM_PROMPT,
        parser_model=Groq(
            id=_PARSER_MODEL_ID,
            api_key=api_key,
            timeout=timeout,
            supports_json_schema_outputs=True,
            max_tokens=_PARSER_MODEL_MAX_TOKENS,
        ),
    )
