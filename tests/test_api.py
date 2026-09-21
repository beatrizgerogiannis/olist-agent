from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest
from agno.agent import Agent
from agno.exceptions import ModelProviderError
from fastapi import status
from fastapi.testclient import TestClient

from data_agent import api
from data_agent.config import get_settings
from data_agent.schemas import AgentAnswer, SourceReference


@dataclass
class _FakeRunOutput:
    """Substituto mínimo de ``agno.run.base.RunOutput`` — só os campos lidos por ``ask()``."""

    content: Any
    metrics: Any = None


class _FakeAgent:
    """Substituto de ``agno.agent.Agent`` injetado via ``api.get_agent``.

    Isola os testes de ``POST /ask`` de qualquer chamada real ao Agno/Groq:
    cada teste controla exatamente o que ``agent.run()`` devolve ou levanta.
    """

    def __init__(
        self,
        *,
        result: Any = None,
        exception: Exception | None = None,
        metrics: Any = None,
    ) -> None:
        self._result = result
        self._exception = exception
        self._metrics = metrics

    def run(self, question: str) -> _FakeRunOutput:
        if self._exception is not None:
            raise self._exception
        return _FakeRunOutput(content=self._result, metrics=self._metrics)


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app)


def _use_fake_agent(monkeypatch: pytest.MonkeyPatch, agent: _FakeAgent) -> None:
    monkeypatch.setattr(api, "get_agent", lambda: agent)


def test_get_agent_builds_and_caches_a_real_agno_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    get_settings.cache_clear()
    api.get_agent.cache_clear()
    try:
        agent = api.get_agent()

        assert isinstance(agent, Agent)
        assert api.get_agent() is agent
    finally:
        api.get_agent.cache_clear()
        get_settings.cache_clear()


def test_health_returns_ok() -> None:
    client = TestClient(api.app)

    response = client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


def test_ask_answerable_question_returns_answered_with_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = AgentAnswer(
        status="answered",
        answer="O total de vendas foi R$ 13.591.643,70.",
        confidence=0.95,
        sql_used=["SELECT round(sum(price), 2) FROM order_items"],
        sources=[SourceReference(table="order_items", row_count=112650)],
    )
    _use_fake_agent(monkeypatch, _FakeAgent(result=answer))

    response = client.post("/ask", json={"question": "Qual o total de vendas?"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "answered"
    assert body["answer"] == answer.answer
    assert body["sql_used"] == answer.sql_used


def test_ask_success_exposes_total_tokens_header_when_metrics_available(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # X-Total-Tokens dá visibilidade de custo por chamada a consumidores como
    # scripts/run_eval.py, sem mudar o contrato de AgentAnswer (ver ADR-0005) —
    # não é um campo do corpo, é um header.
    answer = AgentAnswer(status="answered", answer="42.", confidence=0.9)
    fake_metrics = SimpleNamespace(total_tokens=1234)
    _use_fake_agent(monkeypatch, _FakeAgent(result=answer, metrics=fake_metrics))

    response = client.post("/ask", json={"question": "Qual o total de vendas?"})

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["x-total-tokens"] == "1234"


def test_ask_success_omits_total_tokens_header_when_metrics_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = AgentAnswer(status="answered", answer="42.", confidence=0.9)
    _use_fake_agent(monkeypatch, _FakeAgent(result=answer, metrics=None))

    response = client.post("/ask", json={"question": "Qual o total de vendas?"})

    assert response.status_code == status.HTTP_200_OK
    assert "x-total-tokens" not in response.headers


def test_ask_trap_question_returns_insufficient_data_with_200_not_as_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = AgentAnswer(
        status="insufficient_data",
        answer=(
            "Não há pedidos registrados no período solicitado; "
            "os dados cobrem set/2016 a out/2018."
        ),
        confidence=0.0,
    )
    _use_fake_agent(monkeypatch, _FakeAgent(result=answer))

    response = client.post("/ask", json={"question": "Qual o total de vendas em dezembro de 2019?"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "insufficient_data"
    assert body["sql_used"] == []


def test_ask_out_of_scope_question_returns_out_of_scope_with_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = AgentAnswer(
        status="out_of_scope",
        answer="Previsão de vendas futuras não é uma pergunta que este agente responde.",
        confidence=0.0,
    )
    _use_fake_agent(monkeypatch, _FakeAgent(result=answer))

    response = client.post(
        "/ask", json={"question": "Qual a previsão de vendas para o próximo trimestre?"}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "out_of_scope"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": 123},
        {"pergunta": "Qual o total de vendas?"},
    ],
)
def test_ask_malformed_input_returns_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    # Nenhum destes casos deve sequer chegar a chamar o agente — um agente
    # falso que sempre levanta prova que a validação barrou a requisição antes.
    _use_fake_agent(monkeypatch, _FakeAgent(exception=AssertionError("agente não deveria rodar")))

    response = client.post("/ask", json=payload)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_ask_model_timeout_returns_504(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    timeout_error = groq.APITimeoutError(request=httpx.Request("POST", "http://x"))
    model_error = ModelProviderError(
        message=str(timeout_error), model_name="Groq", model_id="openai/gpt-oss-120b"
    )
    model_error.__cause__ = timeout_error
    _use_fake_agent(monkeypatch, _FakeAgent(exception=model_error))

    response = client.post("/ask", json={"question": "Qual o total de vendas?"})

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT


def test_ask_model_provider_error_without_timeout_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_error = ModelProviderError(
        message="rate limited", status_code=429, model_name="Groq", model_id="openai/gpt-oss-120b"
    )
    _use_fake_agent(monkeypatch, _FakeAgent(exception=model_error))

    response = client.post("/ask", json={"question": "Qual o total de vendas?"})

    assert response.status_code == status.HTTP_502_BAD_GATEWAY


def test_ask_unexpected_tool_or_agent_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_fake_agent(monkeypatch, _FakeAgent(exception=RuntimeError("falha inesperada na tool")))

    response = client.post("/ask", json={"question": "Qual o total de vendas?"})

    assert response.status_code == status.HTTP_502_BAD_GATEWAY


def test_ask_non_structured_output_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reproduz o fallback descrito em ADR-0004: se o parse de `output_schema`
    # falhar, `agent.run()` devolve uma `str` crua em vez de uma `AgentAnswer`.
    _use_fake_agent(monkeypatch, _FakeAgent(result="isto não é um JSON válido {"))

    response = client.post("/ask", json={"question": "Qual o total de vendas?"})

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()["detail"] == "O modelo não retornou uma resposta estruturada válida."


def test_ask_non_string_non_answer_content_returns_502_as_invalid_output(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `RunOutput.content` é tipado como `Optional[Any]` pelo Agno — cobre o caso
    # em que não é nem `AgentAnswer` nem `str` (ex.: `None`), que não pode ser um
    # corpo de erro de provedor por definição.
    _use_fake_agent(monkeypatch, _FakeAgent(result=None))

    response = client.post("/ask", json={"question": "Qual o total de vendas?"})

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()["detail"] == "O modelo não retornou uma resposta estruturada válida."


def test_ask_parser_model_provider_error_as_content_returns_502_as_provider_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reproduz uma falha descoberta rodando scripts/run_eval.py contra a Groq real
    # sob rate limit (Dia 6): quando o `parser_model` falha com um erro do
    # provedor, o Agno não levanta exceção — deixa o corpo de erro cru do
    # provedor passar como se fosse o texto de resposta do modelo. Isso não pode
    # ser confundido com uma saída "só malformada" (ver teste acima): é a mesma
    # categoria de falha de infraestrutura do teste de rate limit via exceção
    # (`test_ask_model_provider_error_without_timeout_returns_502`), só chegando
    # por um caminho diferente.
    raw_provider_error = (
        '{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b`",'
        '"type":"tokens","code":"rate_limit_exceeded"}}\n'
    )
    fake_metrics = SimpleNamespace(total_tokens=777)
    _use_fake_agent(
        monkeypatch, _FakeAgent(result=raw_provider_error, metrics=fake_metrics)
    )

    response = client.post("/ask", json={"question": "Qual o total de vendas?"})

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()["detail"] == "Falha ao consultar o modelo de linguagem."
    # Mesmo numa falha, se algum token foi de fato gasto antes dela (ex. get_schema
    # + tentativas de query_sales antes do parser_model falhar), isso fica visível.
    assert response.headers["x-total-tokens"] == "777"
