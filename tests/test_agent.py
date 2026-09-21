import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator

import pytest
from agno.agent import Agent
from agno.models.base import Model
from agno.models.groq import Groq
from agno.models.response import ModelResponse

from data_agent.agent import build_agent
from data_agent.config import get_settings
from data_agent.prompts import SYSTEM_PROMPT
from data_agent.schemas import AgentAnswer
from data_agent.tools.sql_tools import query_sales


@pytest.fixture(autouse=True)
def _mock_groq_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # `Settings.groq_api_key` tem default "" (vazio); sem fixar um valor aqui,
    # `test_build_agent_uses_groq_model_with_settings_api_key` ficaria
    # acoplado ao que estiver (ou não) num `.env` real na máquina de quem roda os
    # testes, em vez de testar o wiring settings -> agent.model.api_key isolado.
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_build_agent_returns_an_agno_agent() -> None:
    assert isinstance(build_agent(), Agent)


def test_build_agent_wires_the_sql_controlled_tools() -> None:
    # `get_schema` não é mais uma tool do agente principal desde 2026-09-22
    # (otimização de latência): o schema é estático e agora está embutido em
    # `prompts.SYSTEM_PROMPT` como texto — ver docstring de `build_agent` e
    # docs/adrs/0006-troca-de-provedor-llm-para-groq.md, seção "Otimização de
    # latência". A função `get_schema` continua existindo em
    # `tools/sql_tools.py`, só não é mais registrada aqui.
    agent = build_agent()

    assert agent.tools == [query_sales]


def test_build_agent_forces_structured_output_via_agent_answer() -> None:
    agent = build_agent()

    assert agent.output_schema is AgentAnswer


def test_build_agent_uses_the_versioned_system_prompt() -> None:
    agent = build_agent()

    assert agent.instructions == SYSTEM_PROMPT


def test_build_agent_uses_groq_model_with_settings_api_key() -> None:
    agent = build_agent()

    assert isinstance(agent.model, Groq)
    assert agent.model.api_key == "test_key"


def test_build_agent_main_model_does_not_advertise_structured_outputs() -> None:
    # Defaults herdados de `agno.models.base.Model`, não sobrescritos por
    # `agno.models.groq.Groq` — mantidos `False` no modelo principal de propósito
    # (ver docstring de `build_agent`): é o `parser_model` (abaixo) que assume a
    # estruturação, porque a API da Groq rejeita `response_format` combinado com
    # `tools` na mesma chamada.
    agent = build_agent()

    assert agent.model.supports_native_structured_outputs is False
    assert agent.model.supports_json_schema_outputs is False


def test_build_agent_uses_a_tool_less_parser_model_for_structured_output() -> None:
    # `docs/adrs/0006-troca-de-provedor-llm-para-groq.md`: sem `parser_model`,
    # `agent.run()` falha com 400 da API da Groq em toda chamada real, porque o
    # modelo principal sempre tem tools. O `parser_model` roda uma chamada extra,
    # sem tools, só para estruturar a resposta final em `AgentAnswer` — por isso
    # precisa de `supports_json_schema_outputs=True` (o modelo principal
    # deliberadamente não tem isso, ver teste acima).
    agent = build_agent()

    assert isinstance(agent.parser_model, Groq)
    assert agent.parser_model is not agent.model
    assert agent.parser_model.api_key == "test_key"
    assert agent.parser_model.supports_json_schema_outputs is True


def test_build_agent_parser_model_uses_a_smaller_model_than_the_main_one() -> None:
    # docs/adrs/0006-troca-de-provedor-llm-para-groq.md, seção "Otimização de
    # latência" (2026-09-22): o parser_model só estrutura em JSON uma resposta que
    # o modelo principal já produziu — uma tarefa mecânica, não de raciocínio —
    # então não precisa do mesmo modelo grande (`openai/gpt-oss-120b`, default do
    # Agno, não sobrescrito no modelo principal) do agente principal.
    agent = build_agent()

    assert agent.model.id == "openai/gpt-oss-120b"
    assert agent.parser_model.id == "openai/gpt-oss-20b"


def test_build_agent_sets_a_bounded_max_tokens_on_both_models() -> None:
    # Nenhum dos dois modelos tinha `max_tokens` configurado antes (default do
    # Agno/Groq é `None` — sem teto algum) — ver docs/adrs/0006-..., mesma seção.
    agent = build_agent()

    assert agent.model.max_tokens is not None
    assert agent.parser_model.max_tokens is not None
    assert agent.parser_model.max_tokens < agent.model.max_tokens


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
        tools=[query_sales],
        output_schema=AgentAnswer,
        instructions=SYSTEM_PROMPT,
    )

    with caplog.at_level(logging.WARNING, logger="agno"):
        result = agent.run("qualquer pergunta")

    assert not isinstance(result.content, AgentAnswer)
    assert result.content == invalid_json
    assert any("output_schema" in record.getMessage() for record in caplog.records)


@dataclass
class _ToolUseFailedModel(Model):
    """Fake ``Model`` que devolve o erro cru ``tool_use_failed`` da Groq como
    ``content`` (não como exceção levantada).

    Reproduz, sem gastar tokens reais da Groq, o incidente confirmado via trace
    do Langfuse de 2026-09-21 (pergunta "em 2017, quantas vendas houve?", span
    de 2026-09-21T11:59:18Z, ``data_agent/api.py::_is_tool_use_failed_payload``):
    o modelo principal (não o ``parser_model``) tenta emitir uma ``tool_call``
    para ``agent_answer`` — o nome, em snake_case, da classe ``AgentAnswer`` de
    ``output_schema`` — mesmo sem essa tool estar registrada na chamada, e a
    Groq rejeita a chamada inteira. O ``failed_generation`` do erro real já
    contém a resposta correta (45101, com ``sql_used``/``sources`` preenchidos),
    só mal-empacotada como ``tool_call`` em vez de texto.
    """

    id: str = "fake-tool-use-failed-model"
    content: str = json.dumps(
        {
            "error": {
                "message": (
                    "Tool call validation failed: tool call validation failed: "
                    "attempted to call tool 'agent_answer' which was not in "
                    "request.tools"
                ),
                "type": "invalid_request_error",
                "code": "tool_use_failed",
                "failed_generation": json.dumps(
                    {
                        "name": "agent_answer",
                        "arguments": {
                            "status": "answered",
                            "answer": "45101 vendas foram registradas em 2017.",
                            "confidence": 0.99,
                            "sql_used": [
                                "SELECT COUNT(*) FROM orders WHERE "
                                "EXTRACT(YEAR FROM order_purchase_timestamp) = 2017"
                            ],
                            "sources": [{"table": "orders", "rows_returned": 1}],
                        },
                    }
                ),
            }
        }
    )

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


def test_agent_run_with_tool_use_failed_error_logs_and_returns_raw_error_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Confirma por teste (não só lendo o trace do Langfuse) que o erro
    ``tool_use_failed`` da Groq (ver ``_ToolUseFailedModel``) chega ao chamador
    de ``agent.run()`` pelo mesmo caminho silencioso que
    ``test_agent_run_with_invalid_json_logs_warning_and_returns_raw_string``
    já prova para JSON malformado: sem exceção, só um warning e o corpo de erro
    cru como ``content`` (``str``, não ``AgentAnswer``). É esse comportamento
    que ``data_agent/api.py::_is_tool_use_failed_payload`` (e o retry em
    ``ask``) dependem para detectar e mitigar o caso — ver
    ``tests/test_api.py::test_ask_tool_use_failed_error_retries_once_and_returns_success``
    e o teste seguinte para a mitigação em si.
    """
    model = _ToolUseFailedModel()
    agent = Agent(
        model=model,
        tools=[query_sales],
        output_schema=AgentAnswer,
        instructions=SYSTEM_PROMPT,
    )

    with caplog.at_level(logging.WARNING, logger="agno"):
        result = agent.run("em 2017, quantas vendas houve?")

    assert not isinstance(result.content, AgentAnswer)
    assert result.content == model.content
    parsed_error = json.loads(result.content)["error"]
    assert parsed_error["code"] == "tool_use_failed"
    assert "agent_answer" in parsed_error["message"]


@dataclass
class _ToolFailureModel(Model):
    """Fake ``Model`` cuja 1ª resposta pede uma tool REAL que vai falhar de verdade.

    Ao contrário de ``tests/test_api.py`` (onde ``_FakeAgent`` substitui o
    ``Agent`` inteiro), aqui só o ``Model`` é falso — o ``Agent`` e a tool
    (``query_sales`` de ``data_agent.tools.sql_tools``) são reais. Isso permite
    disparar, de propósito e deterministicamente, uma
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
        tools=[query_sales],
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
