import time
from pathlib import Path

import httpx
import pytest
from fastapi import status
from run_eval import (
    EvalOutcome,
    GoldenQuestion,
    RetryConfig,
    _classify,
    _is_infra_response,
    _prompt_fingerprint,
    _response_total_tokens,
    ask_with_retries,
    compute_metrics,
    evaluate_question,
    extract_numbers,
    load_questions,
    matches_expected,
    render_report,
)


def _question(
    expected_status: str = "answered",
    expected_value: float | None = 100.0,
    tolerance: float | None = 0.0,
) -> GoldenQuestion:
    return GoldenQuestion(
        id="q00",
        question="Pergunta de teste?",
        expected_status=expected_status,  # type: ignore[arg-type]
        expected_value=expected_value,
        tolerance=tolerance,
        notes="nota",
    )


_ORDERS_DELIVERED_COUNT = 96478.0
_FREIGHT_TOTAL_BRL = 2251909.54
_AVG_REVIEW_SCORE = 4.0864


class TestExtractNumbers:
    def test_finds_plain_integer(self) -> None:
        assert _ORDERS_DELIVERED_COUNT in extract_numbers("Foram entregues 96478 pedidos.")

    def test_finds_br_formatted_currency(self) -> None:
        text = "O total de frete foi R$ 2.251.909,54."
        assert _FREIGHT_TOTAL_BRL in extract_numbers(text)

    def test_finds_us_formatted_number(self) -> None:
        assert _FREIGHT_TOTAL_BRL in extract_numbers("Total freight was 2,251,909.54.")

    def test_finds_number_with_narrow_no_break_space_thousands_separator(self) -> None:
        # Regressão de q01 (Dia 6, revalidação do prompt): a Groq respondeu
        # "Foram entregues **96 478** pedidos...", usando U+202F (narrow
        # no-break space, convenção SI/francesa) como separador de milhar —
        # confirmado via trace real do Langfuse, não hipotético. SQL e dado
        # batiam (96478); o bug era só aqui, não reconhecer espaço como
        # separador de milhar.
        text = "Foram entregues **96 478** pedidos (order_status = 'delivered')."
        assert _ORDERS_DELIVERED_COUNT in extract_numbers(text)
        assert matches_expected(text, 96478, 0)

    def test_finds_number_with_regular_and_nbsp_thousands_separator(self) -> None:
        assert _ORDERS_DELIVERED_COUNT in extract_numbers("Foram entregues 96 478 pedidos.")
        assert _ORDERS_DELIVERED_COUNT in extract_numbers("Foram entregues 96 478 pedidos.")

    def test_space_separated_small_numbers_are_not_merged_into_one(self) -> None:
        # "5 anos" e "10 mil" não devem virar um único número "5010" ou
        # similar — só dígito-espaço-EXATAMENTE-3-dígitos é tratado como
        # milhar agrupado.
        numbers = extract_numbers("5 anos de operação, 10 mil clientes.")
        assert numbers == {5.0, 10.0}

    def test_matches_expected_within_tolerance(self) -> None:
        assert matches_expected("A média foi 4.0864.", _AVG_REVIEW_SCORE, 0.01)
        assert matches_expected("A média foi 4.5.", _AVG_REVIEW_SCORE, 0.01) is False

    def test_matches_expected_exact_integer(self) -> None:
        assert matches_expected("São 4 pedidos em outubro de 2018.", 4, 0)

    def test_no_numbers_never_matches(self) -> None:
        assert matches_expected("Não há dados suficientes.", 42, 0) is False


class TestIsInfraResponse:
    def _response(
        self, status_code: int, json_body: dict[str, str] | None = None
    ) -> httpx.Response:
        return httpx.Response(
            status_code, json=json_body, request=httpx.Request("POST", "http://x")
        )

    def test_timeout_is_infra(self) -> None:
        response = self._response(status.HTTP_504_GATEWAY_TIMEOUT)
        assert _is_infra_response(response) is True

    def test_provider_error_is_infra(self) -> None:
        response = self._response(
            status.HTTP_502_BAD_GATEWAY, {"detail": "Falha ao consultar o modelo de linguagem."}
        )
        assert _is_infra_response(response) is True

    def test_unexpected_failure_is_not_infra(self) -> None:
        response = self._response(
            status.HTTP_502_BAD_GATEWAY,
            {"detail": "Falha inesperada ao executar o agente ou uma de suas tools."},
        )
        assert _is_infra_response(response) is False

    def test_success_is_not_infra(self) -> None:
        response = self._response(status.HTTP_200_OK, {"status": "ok"})
        assert _is_infra_response(response) is False


class TestClassify:
    def test_answerable_question_answered_correctly(self) -> None:
        question = _question(expected_status="answered", expected_value=96478, tolerance=0)
        assert _classify(question, "answered", "São 96478 pedidos entregues.") == "correct"

    def test_answerable_question_answered_with_wrong_number(self) -> None:
        question = _question(expected_status="answered", expected_value=96478, tolerance=0)
        assert _classify(question, "answered", "São 12345 pedidos.") == "other_mismatch"

    def test_answerable_question_unwarranted_refusal(self) -> None:
        question = _question(expected_status="answered")
        assert _classify(question, "insufficient_data", "Não sei.") == "unwarranted_refusal"

    def test_answerable_question_wrongly_out_of_scope(self) -> None:
        question = _question(expected_status="answered")
        assert _classify(question, "out_of_scope", "Isso foge do escopo.") == "other_mismatch"

    def test_trap_question_hallucination(self) -> None:
        question = _question(
            expected_status="insufficient_data", expected_value=None, tolerance=None
        )
        assert _classify(question, "answered", "O total foi 42.") == "hallucination"

    def test_trap_question_correct_refusal(self) -> None:
        question = _question(
            expected_status="insufficient_data", expected_value=None, tolerance=None
        )
        assert _classify(question, "insufficient_data", "Não há dados.") == "correct"

    def test_trap_question_refused_with_wrong_category(self) -> None:
        question = _question(expected_status="out_of_scope", expected_value=None, tolerance=None)
        assert _classify(question, "insufficient_data", "Não há dados.") == "other_mismatch"


class TestComputeMetrics:
    def test_aggregates_across_verdicts(self) -> None:
        answerable = _question(expected_status="answered", expected_value=1, tolerance=0)
        trap = _question(expected_status="insufficient_data", expected_value=None, tolerance=None)
        answerable_outcomes = [
            EvalOutcome(answerable, "correct", "answered", "1", retries=0),
            EvalOutcome(
                answerable, "unwarranted_refusal", "insufficient_data", "não sei", retries=1
            ),
        ]
        trap_outcomes = [
            EvalOutcome(trap, "hallucination", "answered", "42", retries=0),
            EvalOutcome(trap, "correct", "insufficient_data", "não sei", retries=0),
        ]
        infra_outcomes = [
            EvalOutcome(trap, "infra_error", None, None, retries=5, detail="rate limit"),
        ]
        outcomes = answerable_outcomes + trap_outcomes + infra_outcomes

        metrics = compute_metrics(outcomes)

        evaluated_outcomes = answerable_outcomes + trap_outcomes
        expected_correct = sum(1 for o in evaluated_outcomes if o.verdict == "correct")
        expected_hallucinations = sum(1 for o in trap_outcomes if o.verdict == "hallucination")
        expected_unwarranted_refusals = sum(
            1 for o in answerable_outcomes if o.verdict == "unwarranted_refusal"
        )
        expected_retried = sum(1 for o in outcomes if o.retries > 0)

        assert metrics.total == len(outcomes)
        assert metrics.infra_errors == len(infra_outcomes)
        assert metrics.evaluated == len(evaluated_outcomes)
        assert metrics.answerable_total == len(answerable_outcomes)
        assert metrics.traps_total == len(trap_outcomes)
        assert metrics.correct == expected_correct
        assert metrics.hallucinations == expected_hallucinations
        assert metrics.unwarranted_refusals == expected_unwarranted_refusals
        assert metrics.hit_rate == pytest.approx(expected_correct / len(evaluated_outcomes))
        assert metrics.hallucination_rate == pytest.approx(
            expected_hallucinations / len(trap_outcomes)
        )
        assert metrics.unwarranted_refusal_rate == pytest.approx(
            expected_unwarranted_refusals / len(answerable_outcomes)
        )
        assert len(metrics.retried) == expected_retried

    def test_empty_outcomes_do_not_divide_by_zero(self) -> None:
        metrics = compute_metrics([])
        assert metrics.hit_rate == 0.0
        assert metrics.hallucination_rate == 0.0
        assert metrics.unwarranted_refusal_rate == 0.0


class TestLoadQuestions:
    def test_round_trips_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "golden.jsonl"
        path.write_text(
            '{"id": "q01", "question": "Q?", "expected_status": "answered", '
            '"expected_value": 1.0, "tolerance": 0.0, "notes": "n"}\n'
            "\n"
            '{"id": "q02", "question": "Q2?", "expected_status": "out_of_scope", '
            '"expected_value": null, "tolerance": null, "notes": "n2"}\n'
        )

        questions = load_questions(path)

        assert [q.id for q in questions] == ["q01", "q02"]
        assert questions[0].expected_value == 1.0
        assert questions[1].expected_value is None


class _RetryTransport(httpx.BaseTransport):
    """Transport falso que devolve as respostas de ``responses`` em sequência."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._responses[self.call_count]
        self.call_count += 1
        return response


def _json_response(
    status_code: int, body: dict[str, object], headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status_code, json=body, headers=headers, request=httpx.Request("POST", "http://x/ask")
    )


_PROMPT_FINGERPRINT_LENGTH = 12


class TestPromptFingerprint:
    def test_is_a_stable_short_hex_digest(self) -> None:
        fingerprint = _prompt_fingerprint()

        assert len(fingerprint) == _PROMPT_FINGERPRINT_LENGTH
        assert all(c in "0123456789abcdef" for c in fingerprint)
        assert fingerprint == _prompt_fingerprint()


class TestResponseTotalTokens:
    def test_extracts_header_when_present(self) -> None:
        expected_tokens = 1234
        response = _json_response(200, {}, headers={"x-total-tokens": str(expected_tokens)})
        assert _response_total_tokens(response) == expected_tokens

    def test_none_when_header_absent(self) -> None:
        response = _json_response(200, {})
        assert _response_total_tokens(response) is None


class TestAskWithRetries:
    def test_succeeds_after_transient_infra_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        transport = _RetryTransport(
            [
                _json_response(502, {"detail": "Falha ao consultar o modelo de linguagem."}),
                _json_response(200, {"status": "answered", "answer": "42"}),
            ]
        )
        client = httpx.Client(transport=transport)
        config = RetryConfig(max_retries=3, backoff_base=0.01, backoff_factor=2.0, backoff_max=1.0)

        response, retries, reason = ask_with_retries(client, "http://x", "pergunta", config)

        assert response is not None
        assert response.status_code == status.HTTP_200_OK
        assert retries == transport.call_count - 1
        assert reason == ""

    def test_gives_up_after_exhausting_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        config = RetryConfig(max_retries=2, backoff_base=0.01, backoff_factor=2.0, backoff_max=1.0)
        transport = _RetryTransport(
            [_json_response(504, {"detail": "timeout"})] * (config.max_retries + 1)
        )
        client = httpx.Client(transport=transport)

        response, retries, reason = ask_with_retries(client, "http://x", "pergunta", config)

        assert response is None
        assert retries == config.max_retries
        assert reason


class TestEvaluateQuestion:
    def test_classifies_successful_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        question = _question(expected_status="answered", expected_value=42, tolerance=0)
        expected_tokens = 555
        transport = _RetryTransport(
            [
                _json_response(
                    200,
                    {"status": "answered", "answer": "42"},
                    headers={"x-total-tokens": str(expected_tokens)},
                )
            ]
        )
        client = httpx.Client(transport=transport)
        config = RetryConfig(max_retries=1, backoff_base=0.01, backoff_factor=2.0, backoff_max=1.0)

        outcome = evaluate_question(
            client, "http://x", question, throttle_seconds=0, retry_config=config
        )

        assert outcome.verdict == "correct"
        assert outcome.retries == 0
        assert outcome.total_tokens == expected_tokens

    def test_reports_invalid_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        question = _question()
        transport = _RetryTransport(
            [
                _json_response(
                    502, {"detail": "O modelo não retornou uma resposta estruturada válida."}
                )
            ]
        )
        client = httpx.Client(transport=transport)
        config = RetryConfig(max_retries=0, backoff_base=0.01, backoff_factor=2.0, backoff_max=1.0)

        outcome = evaluate_question(
            client, "http://x", question, throttle_seconds=0, retry_config=config
        )

        assert outcome.verdict == "invalid_output"


def test_render_report_includes_headline_metrics() -> None:
    question = _question(expected_status="answered", expected_value=1, tolerance=0)
    outcomes = [EvalOutcome(question, "correct", "answered", "1", retries=0, total_tokens=100)]
    metrics = compute_metrics(outcomes)

    report = render_report(
        metrics, outcomes, base_url="http://localhost:8000", prompt_fingerprint="deadbeef0000"
    )

    assert "Taxa de acerto" in report
    assert "Taxa de alucinação" in report
    assert "Taxa de recusa indevida" in report
    assert "q00" in report
    assert "deadbeef0000" in report
    assert "100" in report
    assert "Nenhum mismatch nesta execução." in report


def test_render_report_mismatch_diagnostics_include_answer_and_sql() -> None:
    # A seção de diagnóstico precisa do texto completo de `answer`/`sql_used`
    # para um mismatch ser investigável sem rodar de novo contra a API — ver
    # o achado de q01 (Dia 6) em docs/adrs/0009-golden-dataset-e-metricas-de-avaliacao.md.
    question = _question(expected_status="answered", expected_value=96478, tolerance=0)
    outcome = EvalOutcome(
        question,
        "other_mismatch",
        "answered",
        "Foram entregues 96 478 pedidos.",
        retries=0,
        sql_used=[
            "SELECT COUNT(*) AS delivered_count FROM orders WHERE order_status = 'delivered'"
        ],
    )
    metrics = compute_metrics([outcome])

    report = render_report(
        metrics, [outcome], base_url="http://localhost:8000", prompt_fingerprint="deadbeef0000"
    )

    assert "## Diagnóstico de mismatches" in report
    assert "Foram entregues 96 478 pedidos." in report
    assert "SELECT COUNT(*) AS delivered_count" in report
    assert "Nenhum mismatch nesta execução." not in report
