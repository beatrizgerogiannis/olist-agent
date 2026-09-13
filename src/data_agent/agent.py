"""Definição do Agent do Agno (ver docs/adrs/0004-estrategia-anti-alucinacao.md)."""

from __future__ import annotations

from agno.agent import Agent
from agno.models.openai import OpenAIChat

from data_agent.config import get_settings
from data_agent.prompts import SYSTEM_PROMPT
from data_agent.schemas import AgentAnswer
from data_agent.tools.sql_tools import get_schema, query_sales


def build_agent() -> Agent:
    """Monta o Agent do Agno com grounding obrigatório e saída estruturada.

    ``output_schema=AgentAnswer`` faz o Agno pedir o JSON Schema de ``AgentAnswer``
    como ``response_format`` estrito na chamada à API do modelo (``OpenAIChat``
    tem ``supports_native_structured_outputs=True``) — é a própria API da OpenAI
    que recusa gerar algo fora desse schema, não uma validação com retry do lado do
    Agno. Se o parse do JSON devolvido para ``AgentAnswer`` falhar mesmo assim (ex.:
    trocando para um modelo sem structured outputs nativos), o Agno não levanta
    exceção nem tenta de novo — só loga um warning e deixa a string crua passar
    (``Agent.retries`` é ``0`` por padrão e não é configurado aqui; comportamento
    confirmado em
    ``tests/test_agent.py::test_agent_run_with_invalid_json_logs_warning_and_returns_raw_string``).
    Combinado com as regras de recusa em ``prompts.SYSTEM_PROMPT`` e as tools de SQL
    controlado, isso é um dos três mecanismos anti-alucinação descritos no ADR acima.
    """
    settings = get_settings()
    return Agent(
        model=OpenAIChat(
            api_key=settings.openai_api_key or None,
            timeout=settings.openai_timeout_seconds,
        ),
        tools=[get_schema, query_sales],
        output_schema=AgentAnswer,
        instructions=SYSTEM_PROMPT,
    )
