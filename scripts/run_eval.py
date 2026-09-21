"""Roda tests/golden_questions.jsonl contra o agente via HTTP e mede qualidade.

Calcula três métricas sobre as perguntas efetivamente avaliadas (excluindo
falhas de infraestrutura, ver abaixo):

- **Taxa de acerto**: fração de perguntas respondidas corretamente (status
  esperado batido e, quando ``expected_status == "answered"``, o número
  esperado aparece na resposta em texto livre — ver ``matches_expected``).
- **Taxa de alucinação**: dentre as perguntas-armadilha (``expected_status``
  ``insufficient_data``/``out_of_scope``), a fração em que o agente respondeu
  com ``status="answered"`` (um número) em vez de recusar.
- **Taxa de recusa indevida**: dentre as perguntas respondíveis
  (``expected_status == "answered"``), a fração em que o agente devolveu
  ``status="insufficient_data"`` apesar de o dado existir.

Uso:
    uv run python scripts/run_eval.py --base-url http://localhost:8000
    uv run python scripts/run_eval.py --base-url http://localhost:8000 --ids q11,q22

O tier gratuito da Groq (ver docs/adrs/0006-troca-de-provedor-llm-para-groq.md)
tem rate limit apertado (8000 tokens/minuto) e pode levar 30-90s por chamada
sob carga. Por isso este script:

1. Espaça as chamadas (``--throttle-seconds``) para não estourar o rate limit.
2. Distingue explicitamente uma falha de infraestrutura (timeout, erro
   429/5xx da Groq, refletidos por ``data_agent.api`` como HTTP 504/502 — ver
   ``_is_infra_response``) de uma falha real de raciocínio do agente, e faz
   retry com backoff exponencial só para a primeira. Perguntas que esgotam
   todas as tentativas por infraestrutura são excluídas das três métricas
   principais e listadas separadamente no relatório.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Literal

import httpx
import structlog

# Único ponto em que este script depende de `data_agent` (diferente de
# `scripts/load_data.py`, que é standalone) — precisamos do texto exato do
# `SYSTEM_PROMPT` em uso para registrar, por execução, qual versão foi
# avaliada (ver `_prompt_fingerprint`), não uma cópia/paráfrase dele.
from data_agent.prompts import SYSTEM_PROMPT

logger = structlog.get_logger(__name__)

_HEADER_TOTAL_TOKENS = "x-total-tokens"

DEFAULT_QUESTIONS_PATH = Path("tests/golden_questions.jsonl")
DEFAULT_REPORT_PATH = Path("docs/eval_report.md")
DEFAULT_BASE_URL = "http://localhost:8000"

DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_BASE_SECONDS = 5.0
DEFAULT_BACKOFF_FACTOR = 2.0
DEFAULT_BACKOFF_MAX_SECONDS = 60.0
DEFAULT_THROTTLE_SECONDS = 2.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 120.0

# Detalhes de erro que `data_agent/api.py::ask` devolve para cada tipo de falha
# (ver seus testes em `tests/test_api.py`). Usados para diferenciar infra de
# bug real sem precisar de nenhum campo extra na API.
_PROVIDER_ERROR_DETAIL = "Falha ao consultar o modelo de linguagem."
_INVALID_OUTPUT_DETAIL = "O modelo não retornou uma resposta estruturada válida."

ExpectedStatus = Literal["answered", "insufficient_data", "out_of_scope"]
Verdict = Literal[
    "correct",
    "hallucination",
    "unwarranted_refusal",
    "other_mismatch",
    "invalid_output",
    "unexpected_error",
    "infra_error",
]


@dataclass(frozen=True)
class GoldenQuestion:
    """Um item de ``tests/golden_questions.jsonl``."""

    id: str
    question: str
    expected_status: ExpectedStatus
    expected_value: float | None
    tolerance: float | None
    notes: str


@dataclass
class EvalOutcome:
    """Resultado da avaliação de uma ``GoldenQuestion`` contra o agente."""

    question: GoldenQuestion
    verdict: Verdict
    actual_status: str | None
    actual_answer: str | None
    retries: int
    detail: str = ""
    total_tokens: int | None = None
    sql_used: list[str] = field(default_factory=list)


def _prompt_fingerprint() -> str:
    """Hash curto do ``SYSTEM_PROMPT`` em uso — identifica, por execução, qual
    versão do prompt foi avaliada (ex. antes/depois de um ajuste), sem precisar
    copiar o texto inteiro no log/relatório.
    """
    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


def _response_total_tokens(response: httpx.Response) -> int | None:
    raw = response.headers.get(_HEADER_TOTAL_TOKENS)
    return int(raw) if raw is not None else None


def load_questions(path: Path) -> list[GoldenQuestion]:
    questions: list[GoldenQuestion] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        raw = json.loads(stripped)
        questions.append(
            GoldenQuestion(
                id=raw["id"],
                question=raw["question"],
                expected_status=raw["expected_status"],
                expected_value=raw["expected_value"],
                tolerance=raw["tolerance"],
                notes=raw["notes"],
            )
        )
    return questions


# Separadores de milhar aceitos: `.`/`,` (BR/US) e espaço — comum, NBSP
# (` `) e o "narrow no-break space" (` `, convenção SI/francesa que a
# Groq já foi vista usando, ex. `"96 478"` — achado real investigando o
# mismatch de q01 na avaliação do Dia 6, ver ADR-0009. Decimal continua só
# `.`/`,` — espaço nunca separa parte inteira de decimal.
_THOUSANDS_SEPARATORS = ".,   "

# Casa tanto números com separador de milhar (`13.591.643,70`, `13,591,643.70`,
# `96 478`) quanto números simples (`4`, `12.4968`) — não distingue
# formato BR/US/espaçado aqui, só encontra candidatos; `_normalize_candidates`
# tenta as leituras possíveis.
_NUMBER_RE = re.compile(
    rf"\d{{1,3}}(?:[{_THOUSANDS_SEPARATORS}]\d{{3}})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
)


def _normalize_candidates(raw: str) -> set[float]:
    """Interpreta ``raw`` como número BR (``.`` milhar, ``,`` decimal) e US.

    A resposta do agente é texto livre em português, então números grandes
    tendem a vir no formato BR (``13.591.643,70``), mas o modelo já foi visto
    variando isso — inclusive usando espaço (comum ou NBSP/narrow-NBSP) como
    separador de milhar em vez de ``.``/``,``. Espaço nunca é separador
    decimal, então é removido antes de tentar as duas leituras BR/US; tentar
    todas essas variações e aceitar qualquer uma que bata com o valor esperado
    evita falsos negativos por causa só de formatação — o que este script quer
    detectar é se o número certo aparece na resposta, não validar o estilo de
    formatação do agente.
    """
    despaced = raw.replace(" ", "").replace(" ", "").replace(" ", "")
    candidates: set[float] = set()
    for normalized in (despaced.replace(".", "").replace(",", "."), despaced.replace(",", "")):
        try:
            candidates.add(float(normalized))
        except ValueError:
            continue
    return candidates


def extract_numbers(text: str) -> set[float]:
    numbers: set[float] = set()
    for match in _NUMBER_RE.findall(text):
        numbers |= _normalize_candidates(match)
    return numbers


def matches_expected(answer_text: str, expected_value: float, tolerance: float) -> bool:
    tol = max(tolerance, 1e-9)
    return any(abs(candidate - expected_value) <= tol for candidate in extract_numbers(answer_text))


def _is_infra_response(response: httpx.Response) -> bool:
    """``True`` se ``response`` representa uma falha de infraestrutura (rate
    limit/timeout/erro do provedor Groq), não uma falha de raciocínio do agente.

    Ver docstring do módulo e ``data_agent/api.py::ask`` — timeout vira 504;
    qualquer outro ``ModelProviderError`` (inclui rate limit 429 e erros 5xx da
    Groq) vira 502 com ``detail=_PROVIDER_ERROR_DETAIL``. Um 502 por bug
    inesperado ou saída não estruturada usa outros textos de ``detail`` e não
    conta como infra.
    """
    if response.status_code == HTTPStatus.GATEWAY_TIMEOUT:
        return True
    if response.status_code == HTTPStatus.BAD_GATEWAY:
        try:
            detail = response.json().get("detail", "")
        except ValueError:
            detail = ""
        return detail == _PROVIDER_ERROR_DETAIL
    return False


@dataclass(frozen=True)
class RetryConfig:
    """Parâmetros de retry+backoff exponencial para falhas de infraestrutura."""

    max_retries: int
    backoff_base: float
    backoff_factor: float
    backoff_max: float


def _attempt_request(
    client: httpx.Client, base_url: str, question: str
) -> tuple[httpx.Response | None, str]:
    """Uma tentativa de ``POST /ask``; devolve ``(response, motivo)`` — ``response``
    é ``None`` só em falha de transporte (timeout/conexão), e ``motivo`` só é
    preenchido nesse caso.
    """
    try:
        return client.post(f"{base_url}/ask", json={"question": question}), ""
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return None, f"erro de transporte: {exc!r}"


def ask_with_retries(
    client: httpx.Client, base_url: str, question: str, config: RetryConfig
) -> tuple[httpx.Response | None, int, str]:
    """Faz ``POST /ask``, com retry+backoff exponencial só para falha de infra.

    Devolve ``(response, tentativas_extras_usadas, motivo)``. ``response`` é
    ``None`` somente se todas as tentativas esgotaram por infraestrutura
    (timeout de rede/conexão, ou os casos de ``_is_infra_response``) — nesse
    caso ``motivo`` explica a última falha observada.
    """
    reason = ""
    for attempt in range(config.max_retries + 1):
        response, reason = _attempt_request(client, base_url, question)
        if response is not None:
            if not _is_infra_response(response):
                return response, attempt, ""
            try:
                reason = response.json().get("detail") or f"HTTP {response.status_code}"
            except ValueError:
                reason = f"HTTP {response.status_code}"

        if attempt < config.max_retries:
            delay = min(config.backoff_base * (config.backoff_factor**attempt), config.backoff_max)
            logger.warning(
                "eval_infra_retry",
                question=question,
                attempt=attempt + 1,
                max_retries=config.max_retries,
                delay_seconds=delay,
                reason=reason,
            )
            time.sleep(delay)

    return None, config.max_retries, reason or "falha de infraestrutura persistente"


def _classify(question: GoldenQuestion, actual_status: str, actual_answer: str) -> Verdict:
    if question.expected_status == "answered":
        if actual_status != "answered":
            if actual_status == "insufficient_data":
                return "unwarranted_refusal"
            return "other_mismatch"
        assert question.expected_value is not None
        tolerance = question.tolerance if question.tolerance is not None else 0.0
        if matches_expected(actual_answer, question.expected_value, tolerance):
            return "correct"
        return "other_mismatch"

    # Pergunta-armadilha (expected_status é insufficient_data ou out_of_scope).
    if actual_status == "answered":
        return "hallucination"
    return "correct" if actual_status == question.expected_status else "other_mismatch"


def evaluate_question(
    client: httpx.Client,
    base_url: str,
    question: GoldenQuestion,
    *,
    throttle_seconds: float,
    retry_config: RetryConfig,
) -> EvalOutcome:
    time.sleep(throttle_seconds)
    logger.info("eval_question_started", id=question.id, question=question.question)

    response, retries, infra_reason = ask_with_retries(
        client, base_url, question.question, retry_config
    )

    if response is None:
        logger.error("eval_question_infra_error", id=question.id, reason=infra_reason)
        return EvalOutcome(question, "infra_error", None, None, retries, infra_reason)

    total_tokens = _response_total_tokens(response)

    if response.status_code == HTTPStatus.OK:
        body = response.json()
        sql_used = body.get("sql_used") or []
        verdict = _classify(question, body["status"], body["answer"])
        # `answer`/`sql_used` completos no log (não só o status) para que um
        # `other_mismatch`/`hallucination`/`unwarranted_refusal` seja
        # diagnosticável a partir do log de uma execução já feita, sem
        # precisar rodar de novo contra a API real — achado real do Dia 6
        # (`q01`): sem isso, só dava para saber que o veredito bateu errado,
        # não o porquê.
        logger.info(
            "eval_question_completed",
            id=question.id,
            expected_status=question.expected_status,
            actual_status=body["status"],
            verdict=verdict,
            total_tokens=total_tokens,
            answer=body["answer"],
            sql_used=sql_used,
        )
        return EvalOutcome(
            question,
            verdict,
            body["status"],
            body["answer"],
            retries,
            total_tokens=total_tokens,
            sql_used=sql_used,
        )

    try:
        detail = response.json().get("detail") or f"HTTP {response.status_code}"
    except ValueError:
        detail = f"HTTP {response.status_code}"
    verdict = "invalid_output" if detail == _INVALID_OUTPUT_DETAIL else "unexpected_error"
    logger.error(
        "eval_question_failed",
        id=question.id,
        verdict=verdict,
        detail=detail,
        total_tokens=total_tokens,
    )
    return EvalOutcome(question, verdict, None, None, retries, detail, total_tokens=total_tokens)


@dataclass
class Metrics:
    total: int
    infra_errors: int
    evaluated: int
    traps_total: int
    answerable_total: int
    correct: int
    hallucinations: int
    unwarranted_refusals: int
    other_mismatch: int
    invalid_output: int
    unexpected_error: int
    total_tokens: int
    retried: list[EvalOutcome] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return self.correct / self.evaluated if self.evaluated else 0.0

    @property
    def hallucination_rate(self) -> float:
        return self.hallucinations / self.traps_total if self.traps_total else 0.0

    @property
    def unwarranted_refusal_rate(self) -> float:
        return self.unwarranted_refusals / self.answerable_total if self.answerable_total else 0.0


def compute_metrics(outcomes: list[EvalOutcome]) -> Metrics:
    evaluated = [o for o in outcomes if o.verdict != "infra_error"]
    traps = [o for o in evaluated if o.question.expected_status != "answered"]
    answerable = [o for o in evaluated if o.question.expected_status == "answered"]

    def count(items: list[EvalOutcome], verdict: Verdict) -> int:
        return sum(1 for o in items if o.verdict == verdict)

    return Metrics(
        total=len(outcomes),
        infra_errors=len(outcomes) - len(evaluated),
        evaluated=len(evaluated),
        traps_total=len(traps),
        answerable_total=len(answerable),
        correct=count(evaluated, "correct"),
        hallucinations=count(traps, "hallucination"),
        unwarranted_refusals=count(answerable, "unwarranted_refusal"),
        other_mismatch=count(evaluated, "other_mismatch"),
        invalid_output=count(evaluated, "invalid_output"),
        unexpected_error=count(evaluated, "unexpected_error"),
        total_tokens=sum(o.total_tokens for o in outcomes if o.total_tokens is not None),
        retried=[o for o in outcomes if o.retries > 0],
    )


def render_report(
    metrics: Metrics, outcomes: list[EvalOutcome], *, base_url: str, prompt_fingerprint: str
) -> str:
    lines: list[str] = [
        "# Relatório de avaliação do agente",
        "",
        f"Gerado por `scripts/run_eval.py` contra `{base_url}`, usando "
        f"`tests/golden_questions.jsonl` ({metrics.total} perguntas).",
        "",
        f"`SYSTEM_PROMPT` (fingerprint sha256[:12]): `{prompt_fingerprint}` — ver "
        "`src/data_agent/prompts.py`. Um fingerprint diferente do de uma execução "
        "anterior é a evidência de que o prompt mudou entre as duas.",
        "",
        f"Tokens consumidos nesta execução (soma de `X-Total-Tokens` por chamada, "
        f"quando disponível): **{metrics.total_tokens}**.",
        "",
        "## Métricas principais",
        "",
        "As três métricas abaixo excluem itens marcados como falha de "
        f"infraestrutura ({metrics.infra_errors} de {metrics.total} — ver seção "
        "correspondente): rate limit, timeout ou erro 5xx da Groq não é "
        "atribuível ao raciocínio do agente.",
        "",
        "| Métrica | Valor | Base |",
        "|---|---|---|",
        f"| Taxa de acerto | {metrics.hit_rate:.1%} | {metrics.correct}/{metrics.evaluated} |",
        (
            f"| Taxa de alucinação | {metrics.hallucination_rate:.1%} | "
            f"{metrics.hallucinations}/{metrics.traps_total} (perguntas-armadilha) |"
        ),
        (
            f"| Taxa de recusa indevida | {metrics.unwarranted_refusal_rate:.1%} | "
            f"{metrics.unwarranted_refusals}/{metrics.answerable_total} (perguntas respondíveis) |"
        ),
        "",
        "## Outras categorias (fora das três métricas principais)",
        "",
        "| Categoria | Contagem | O que significa |",
        "|---|---|---|",
        (
            f"| Falha de infraestrutura | {metrics.infra_errors} | Rate limit/timeout/erro "
            "5xx da Groq — excluída das métricas acima, listada abaixo. |"
        ),
        (
            f"| Saída não estruturada | {metrics.invalid_output} | O `parser_model` não "
            "devolveu um JSON válido (ver ADR-0004/ADR-0006); a Groq respondeu, mas fora do "
            "schema — não é rate limit nem hallucination/recusa indevida por definição, mas "
            "conta contra a taxa de acerto. |"
        ),
        (
            f"| Erro inesperado | {metrics.unexpected_error} | Exceção não tratada no agente "
            "ou numa tool — bug real, investigar. |"
        ),
        (
            f"| Recusa/resposta na categoria errada | {metrics.other_mismatch} | Ex.: recusou "
            "com `out_of_scope` quando o gabarito era `insufficient_data` (ou vice-versa), ou "
            "respondeu com um número que não bate com o gabarito. Conta contra a taxa de "
            "acerto, mas não é hallucination nem recusa indevida pela definição estrita "
            "usada aqui. |"
        ),
        "",
        "## Itens que precisaram de retry",
        "",
    ]

    if metrics.retried:
        lines.append("| ID | Pergunta | Tentativas extras |")
        lines.append("|---|---|---|")
        for outcome in metrics.retried:
            lines.append(
                f"| {outcome.question.id} | {outcome.question.question} | {outcome.retries} |"
            )
    else:
        lines.append("Nenhum item precisou de retry nesta execução.")
    lines.append("")

    lines.append("## Detalhe por pergunta")
    lines.append("")
    lines.append(
        "| ID | Status esperado | Status obtido | Veredito | Retries | Tokens | Pergunta |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for outcome in outcomes:
        tokens = outcome.total_tokens if outcome.total_tokens is not None else "-"
        lines.append(
            f"| {outcome.question.id} | {outcome.question.expected_status} | "
            f"{outcome.actual_status or outcome.detail} | {outcome.verdict} | "
            f"{outcome.retries} | {tokens} | {outcome.question.question} |"
        )
    lines.append("")

    # Só para vereditos que não bateram o esperado (correct/infra_error ficam
    # de fora — infra_error nem chegou a produzir uma AgentAnswer real) — o
    # texto completo de `answer`/`sql_used` é o que falta pra diagnosticar um
    # other_mismatch/hallucination/unwarranted_refusal sem rodar de novo
    # contra a API (ver achado de q01 em docs/adrs/0009-...md).
    mismatches = [o for o in outcomes if o.verdict not in ("correct", "infra_error")]
    lines.append("## Diagnóstico de mismatches")
    lines.append("")
    if mismatches:
        for outcome in mismatches:
            lines.append(f"### {outcome.question.id} — {outcome.verdict}")
            lines.append("")
            lines.append(f"- **Pergunta**: {outcome.question.question}")
            lines.append(
                f"- **Esperado**: `{outcome.question.expected_status}`"
                + (
                    f" (valor {outcome.question.expected_value}, tolerância "
                    f"{outcome.question.tolerance})"
                    if outcome.question.expected_value is not None
                    else ""
                )
            )
            lines.append(f"- **Status obtido**: `{outcome.actual_status or '(sem resposta)'}`")
            answer = outcome.actual_answer or outcome.detail or "(vazio)"
            lines.append(f"- **Resposta completa (`answer`)**: {answer}")
            if outcome.sql_used:
                lines.append("- **`sql_used`**:")
                for sql in outcome.sql_used:
                    lines.append(f"  - `{sql}`")
            else:
                lines.append("- **`sql_used`**: (vazio)")
            lines.append("")
    else:
        lines.append("Nenhum mismatch nesta execução.")
        lines.append("")

    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--ids",
        default=None,
        help="Lista de ids separados por vírgula (ex.: q11,q22) para rodar só um subconjunto.",
    )
    parser.add_argument("--throttle-seconds", type=float, default=DEFAULT_THROTTLE_SECONDS)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--backoff-base-seconds", type=float, default=DEFAULT_BACKOFF_BASE_SECONDS)
    parser.add_argument("--backoff-factor", type=float, default=DEFAULT_BACKOFF_FACTOR)
    parser.add_argument("--backoff-max-seconds", type=float, default=DEFAULT_BACKOFF_MAX_SECONDS)
    parser.add_argument("--http-timeout-seconds", type=float, default=DEFAULT_HTTP_TIMEOUT_SECONDS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    prompt_fingerprint = _prompt_fingerprint()
    logger.info(
        "eval_run_started",
        prompt_fingerprint=prompt_fingerprint,
        base_url=args.base_url,
        questions_file=str(args.questions),
        ids_filter=args.ids,
    )

    questions = load_questions(args.questions)
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q.id in wanted]

    retry_config = RetryConfig(
        max_retries=args.max_retries,
        backoff_base=args.backoff_base_seconds,
        backoff_factor=args.backoff_factor,
        backoff_max=args.backoff_max_seconds,
    )

    outcomes: list[EvalOutcome] = []
    with httpx.Client(timeout=args.http_timeout_seconds) as client:
        for question in questions:
            outcomes.append(
                evaluate_question(
                    client,
                    args.base_url,
                    question,
                    throttle_seconds=args.throttle_seconds,
                    retry_config=retry_config,
                )
            )

    metrics = compute_metrics(outcomes)
    report = render_report(
        metrics, outcomes, base_url=args.base_url, prompt_fingerprint=prompt_fingerprint
    )
    args.output.write_text(report, encoding="utf-8")

    print(f"SYSTEM_PROMPT fingerprint: {prompt_fingerprint}")
    print(f"Taxa de acerto: {metrics.hit_rate:.1%} ({metrics.correct}/{metrics.evaluated})")
    print(
        f"Taxa de alucinação: {metrics.hallucination_rate:.1%} "
        f"({metrics.hallucinations}/{metrics.traps_total})"
    )
    print(
        f"Taxa de recusa indevida: {metrics.unwarranted_refusal_rate:.1%} "
        f"({metrics.unwarranted_refusals}/{metrics.answerable_total})"
    )
    print(f"Falhas de infraestrutura (excluídas): {metrics.infra_errors}/{metrics.total}")
    print(f"Tokens consumidos nesta execução: {metrics.total_tokens}")
    print(f"Relatório escrito em {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
