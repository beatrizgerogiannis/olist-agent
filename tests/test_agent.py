import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator

import pytest
from agno.agent import Agent
from agno.models.base import Model
from agno.models.openai import OpenAIChat
from agno.models.response import ModelResponse

from data_agent.agent import build_agent
from data_agent.config import get_settings
from data_agent.prompts import SYSTEM_PROMPT
from data_agent.schemas import AgentAnswer
from data_agent.tools.sql_tools import get_schema, query_sales


@pytest.fixture(autouse=True)
def _mock_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # `Settings.openai_api_key` tem default "" (vazio); sem fixar um valor aqui,
    # `test_build_agent_uses_openai_chat_model_with_settings_api_key` ficaria
    # acoplado ao que estiver (ou não) num `.env` real na máquina de quem roda os
    # testes, em vez de testar o wiring settings -> agent.model.api_key isolado.
    monkeypatch.setenv("OPENAI_API_KEY", "test_key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_build_agent_returns_an_agno_agent() -> None:
    assert isinstance(build_agent(), Agent)


def test_build_agent_wires_the_sql_controlled_tools() -> None:
    agent = build_agent()

    assert agent.tools == [get_schema, query_sales]


def test_build_agent_forces_structured_output_via_agent_answer() -> None:
    agent = build_agent()

    assert agent.output_schema is AgentAnswer


def test_build_agent_uses_the_versioned_system_prompt() -> None:
    agent = build_agent()

    assert agent.instructions == SYSTEM_PROMPT


def test_build_agent_uses_openai_chat_model_with_settings_api_key() -> None:
    agent = build_agent()

    assert isinstance(agent.model, OpenAIChat)
    assert agent.model.api_key == "test_key"


@dataclass
class _InvalidJsonModel(Model):
    """Fake ``Model`` do Agno que sempre devolve conteúdo que não é JSON válido.

    Usado para provar empiricamente (não só ler o código-fonte do Agno) o que
    acontece quando a resposta do modelo não valida contra ``output_schema`` — ver
    ``test_agent_run_with_invalid_json_logs_warning_and_returns_raw_string`` abaixo
    e docs/adrs/0004-estrategia-anti-alucinacao.md.
    """

    id: str = "fake-invalid-json-model"
    content: str = "isto não é um JSON válido {"

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return ModelResponse(role="assistant", content=self.content)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self.invoke(*args, **kwargs)

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


def test_agent_run_with_invalid_json_logs_warning_and_returns_raw_string(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Confirma por teste (não só por leitura do código-fonte do Agno) que o
    enforcement de `AgentAnswer` de hoje não vem de uma validação com retry do
    Agno: quando o modelo devolve algo que não é JSON válido, `agent.run` não
    levanta exceção nenhuma — só loga um warning e devolve o conteúdo cru (uma
    `str`, não uma instância de `AgentAnswer`) para quem chamou o agente. Um
    provider sem structured outputs nativos (ou um bug de provider) cai
    exatamente nesse caminho silencioso.
    """
    invalid_json = "isto não é um JSON válido {"
    agent = Agent(
        model=_InvalidJsonModel(content=invalid_json),
        tools=[get_schema, query_sales],
        output_schema=AgentAnswer,
        instructions=SYSTEM_PROMPT,
    )

    with caplog.at_level(logging.WARNING, logger="agno"):
        result = agent.run("qualquer pergunta")

    assert not isinstance(result.content, AgentAnswer)
    assert result.content == invalid_json
    assert any("output_schema" in record.getMessage() for record in caplog.records)


@dataclass
class _ToolFailureModel(Model):
    """Fake ``Model`` cuja 1ª resposta pede uma tool REAL que vai falhar de verdade.

    Ao contrário de ``tests/test_api.py`` (onde ``_FakeAgent`` substitui o
    ``Agent`` inteiro), aqui só o ``Model`` é falso — o ``Agent`` e as tools
    (``get_schema``/``query_sales`` de ``data_agent.tools.sql_tools``) são os
    reais. Isso permite disparar, de propósito e deterministicamente, uma
    exceção de verdade dentro de uma tool real (aqui, um ``FileNotFoundError``
    de ``data_agent/db.py`` ao apontar ``query_sales`` para um ``db_path``
    inexistente) sem depender de uma chamada de rede real a um LLM.
    """

    id: str = "fake-tool-failure-model"
    calls: int = field(default=0, init=False, repr=False)

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                role="assistant",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "query_sales",
                            "arguments": json.dumps(
                                {
                                    "sql": "SELECT 1",
                                    "db_path": "/tmp/does-not-exist-olist-warehouse.duckdb",
                                }
                            ),
                        },
                    }
                ],
            )
        return ModelResponse(
            role="assistant",
            content=json.dumps(
                {
                    "status": "insufficient_data",
                    "answer": (
                        "A consulta à base falhou; não há dado suficiente para responder."
                    ),
                    "confidence": 0.0,
                    "sql_used": [],
                    "sources": [],
                }
            ),
        )

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self.invoke(*args, **kwargs)

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


def test_agent_run_when_a_real_tool_raises_does_not_propagate_the_exception() -> None:
    """Prova empírica (não só leitura do código-fonte do Agno) da alegação central de
    docs/adrs/0005-insufficient-data-como-resposta-valida.md: quando uma tool REAL
    levanta uma exceção de verdade durante a execução do agente, o próprio Agno
    captura essa exceção dentro da execução da tool — ela não escapa até quem
    chamou ``agent.run()`` (e, portanto, não escaparia até ``data_agent/api.py``
    no caminho comum). Usa o ``Agent`` real com as tools reais de
    ``tools/sql_tools.py``; só o ``Model`` é substituído (por ``_ToolFailureModel``,
    não por um agente/resultado inteiramente falso), para forçar
    deterministicamente uma chamada a ``query_sales`` com um ``db_path``
    inexistente, que levanta ``FileNotFoundError`` de verdade em
    ``data_agent/db.py::get_connection``.
    """
    agent = Agent(
        model=_ToolFailureModel(),
        tools=[get_schema, query_sales],
        output_schema=AgentAnswer,
        instructions=SYSTEM_PROMPT,
    )

    result = agent.run("qualquer pergunta")  # não deve levantar FileNotFoundError

    tool_executions = result.tools or []
    assert len(tool_executions) == 1
    assert tool_executions[0].tool_name == "query_sales"
    assert tool_executions[0].tool_call_error is True
    assert "não encontrado" in (tool_executions[0].result or "")

    assert isinstance(result.content, AgentAnswer)
    assert result.content.status == "insufficient_data"
