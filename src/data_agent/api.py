"""Aplicação FastAPI que expõe o agente via HTTP (ver
docs/adrs/0005-insufficient-data-como-resposta-valida.md para a decisão de
tratar ``status="insufficient_data"`` como resposta válida em 200, não como
erro).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

import groq
import structlog
from agno.agent import Agent
from agno.exceptions import ModelProviderError
from agno.run.agent import RunOutput
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from data_agent.agent import build_agent
from data_agent.config import get_settings
from data_agent.observability import configure_observability
from data_agent.schemas import AgentAnswer, AskRequest

_LOG_LEVELS = logging.getLevelNamesMapping()


def _configure_structlog(log_level: str) -> None:
    """Configura o structlog para emitir JSON estruturado (nunca print/logging básico).

    Chamado na importação deste módulo, antes de qualquer request: assim, todo
    ``structlog.get_logger`` já usado nas tools (``tools/sql_tools.py``) e neste
    módulo sai no mesmo formato JSON, o que facilita cruzar essas linhas com os
    traces do Langfuse (ver ``configure_observability`` logo abaixo e
    docs/adrs/0007-observabilidade-com-langfuse.md).
    """
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            _LOG_LEVELS.get(log_level.upper(), logging.INFO)
        ),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        cache_logger_on_first_use=True,
    )


_configure_structlog(get_settings().log_level)
configure_observability()

logger = structlog.get_logger(__name__)

app = FastAPI(title="data-agent", version="0.1.0")

# Rate limit por IP em `/ask` (ver docs/adrs/0010-hospedagem-do-demo-publico.md): a única rota
# que dispara uma chamada real (cara, e paga) ao provedor de LLM. Um valor fixo de código, não
# uma variável de ambiente (mesmo raciocínio do id do modelo em ``agent.py`` — ver
# ADR-0006): "poucas requisições por minuto" é suficiente para uma demo de portfólio, e expor
# isso como configuração sugeriria um caso de uso (afinar rate limit em produção) que este
# projeto não tem. ``/health`` fica de fora — é só o healthcheck do Docker/Render.
_ASK_RATE_LIMIT = "5/minute"

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter


def _handle_rate_limit_exceeded(request: Request, exc: Exception) -> Response:
    """Adapta ``slowapi._rate_limit_exceeded_handler`` à assinatura que
    ``Starlette.add_exception_handler`` exige (``exc: Exception``, não
    ``exc: RateLimitExceeded``) — o registro abaixo só chama isto para exceções
    ``RateLimitExceeded``, então o ``isinstance`` nunca falha em uso normal.
    """
    assert isinstance(exc, RateLimitExceeded)
    return _rate_limit_exceeded_handler(request, exc)


app.add_exception_handler(RateLimitExceeded, _handle_rate_limit_exceeded)
app.add_middleware(SlowAPIMiddleware)


@lru_cache
def get_agent() -> Agent:
    """Instância única (por processo) do ``Agent`` do Agno, construída sob demanda.

    ``lru_cache`` evita reconstruir o ``Agent`` (e o client HTTP do modelo) a
    cada request. Em testes, substitua esta função inteira (via
    ``monkeypatch.setattr``) por um agente falso, em vez de tentar limpar o
    cache — isola os testes de ``POST /ask`` de qualquer chamada real ao Agno.
    """
    return build_agent()


class HealthResponse(BaseModel):
    """Resposta de ``GET /health``."""

    status: Literal["ok"]


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


# Mesmo texto usado no branch de ``ModelProviderError`` sem timeout logo abaixo —
# ambos representam a mesma categoria de falha (infraestrutura/provedor), só
# chegando por caminhos diferentes (exceção vs. conteúdo cru, ver
# ``_is_provider_error_payload``). Um consumidor da API (ex.
# ``scripts/run_eval.py``) que precise diferenciar infraestrutura de falha de
# raciocínio do agente depende desse texto ser idêntico nos dois casos.
_PROVIDER_ERROR_DETAIL = "Falha ao consultar o modelo de linguagem."

# Header (não campo de `AgentAnswer`) para expor o custo em tokens de uma chamada,
# sem alterar o contrato documentado em ADR-0005 — o schema de saída do agente é
# decidido pelo `parser_model` (ver ADR-0006) e não faz sentido pedir a ele para
# "saber" quantos tokens ele mesmo consumiu. Adicionado no Dia 6 para dar
# visibilidade de consumo a `scripts/run_eval.py` (o tier gratuito da Groq tem
# orçamento diário apertado — ver docs/adrs/0009-golden-dataset-e-metricas-de-avaliacao.md).
_HEADER_TOTAL_TOKENS = "X-Total-Tokens"


def _total_tokens(run_output: RunOutput) -> int | None:
    """Extrai o total de tokens (todas as chamadas de modelo desta execução,
    incluindo o ``parser_model``) de ``run_output.metrics``, se disponível.

    ``metrics`` é ``None`` só se o Agno não chegou a inicializar `RunMetrics`
    (não observado em uso normal, mas o tipo é `Optional` — ver
    ``agno.run.agent.RunOutput``), daí o acesso defensivo.
    """
    metrics = run_output.metrics
    return metrics.total_tokens if metrics is not None else None


def _is_provider_error_payload(content: object) -> bool:
    """``True`` se ``content`` é o corpo de erro cru de um provedor de LLM (ex.
    Groq), não uma tentativa (só malformada) de ``AgentAnswer``.

    Descoberto rodando ``scripts/run_eval.py`` contra a Groq real sob rate limit
    (Dia 6): o ``parser_model`` (ver docs/adrs/0006-troca-de-provedor-llm-para-groq.md)
    pode falhar com um erro do próprio provedor (ex. ``429`` de rate limit) sem que
    o Agno levante nenhuma exceção — o mesmo comportamento de "loga um warning e
    deixa a string crua passar" que docs/adrs/0004-estrategia-anti-alucinacao.md já
    documentava para JSON malformado também se aplica aqui, só que a "string crua"
    é o corpo de erro do provedor (``{"error": {"message": ..., "code": ...}}``),
    não uma tentativa de resposta do modelo. Sem esta checagem, isso caía no branch
    genérico de "saída não estruturada" — uma categoria de falha de *qualidade do
    modelo*, quando na real é a mesma falha de *infraestrutura* que o branch de
    ``ModelProviderError`` acima já trata como ``502``/``504`` quando é levantada
    como exceção em vez de aparecer como conteúdo.
    """
    if not isinstance(content, str):
        return False
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and isinstance(parsed.get("error"), dict)


@app.post("/ask", response_model=AgentAnswer)
@limiter.limit(_ASK_RATE_LIMIT)
def ask(request: Request, payload: AskRequest, response: Response) -> AgentAnswer:
    """Responde ``payload.question`` via o agente e suas tools de SQL controlado.

    ``request`` (o ``Request`` do Starlette) só existe aqui para o
    ``@limiter.limit`` acima identificar o IP de origem — não é usado no corpo da
    função. ``payload`` já chega validado como ``AskRequest`` (FastAPI devolve 422
    automaticamente para um corpo malformado antes de esta função rodar) e a
    resposta é validada como ``AgentAnswer`` na saída. ``status="insufficient_data"``
    e ``status="out_of_scope"`` são decisões válidas do agente e voltam com 200,
    como qualquer outra ``AgentAnswer`` — só uma falha real de infraestrutura
    (timeout do modelo, erro do provedor — levantado como exceção ou, no caso do
    ``parser_model``, entregue como conteúdo cru, ver ``_is_provider_error_payload``
    — ou uma tool/exceção que escapou do agente) vira um erro HTTP.

    Quando ``run_output`` chega a existir (sucesso ou os dois ramos de "saída não
    estruturada" abaixo), a resposta carrega o header ``X-Total-Tokens`` com o
    custo em tokens da chamada (ver ``_total_tokens``) — não existe nos casos em
    que ``agent.run()`` levanta antes de devolver nada.
    """
    logger.info("agent_call_started", question=payload.question)
    agent = get_agent()
    try:
        run_output = agent.run(payload.question)
    except ModelProviderError as exc:
        is_timeout = isinstance(exc.__cause__, groq.APITimeoutError)
        logger.error(
            "agent_call_model_error",
            question=payload.question,
            error=str(exc),
            timeout=is_timeout,
        )
        if is_timeout:
            raise HTTPException(
                status_code=504, detail="Timeout ao consultar o modelo de linguagem."
            ) from exc
        raise HTTPException(status_code=502, detail=_PROVIDER_ERROR_DETAIL) from exc
    except Exception as exc:
        logger.error("agent_call_failed", question=payload.question, error=str(exc))
        raise HTTPException(
            status_code=502,
            detail="Falha inesperada ao executar o agente ou uma de suas tools.",
        ) from exc

    total_tokens = _total_tokens(run_output)
    error_headers = {_HEADER_TOTAL_TOKENS: str(total_tokens)} if total_tokens is not None else {}

    if not isinstance(run_output.content, AgentAnswer):
        if _is_provider_error_payload(run_output.content):
            logger.error(
                "agent_call_provider_error_as_content",
                question=payload.question,
                content=run_output.content,
                total_tokens=total_tokens,
            )
            raise HTTPException(
                status_code=502, detail=_PROVIDER_ERROR_DETAIL, headers=error_headers
            )
        logger.error(
            "agent_call_invalid_output",
            question=payload.question,
            content=run_output.content,
            total_tokens=total_tokens,
        )
        raise HTTPException(
            status_code=502,
            detail="O modelo não retornou uma resposta estruturada válida.",
            headers=error_headers,
        )

    logger.info(
        "agent_call_completed",
        question=payload.question,
        status=run_output.content.status,
        total_tokens=total_tokens,
    )
    if total_tokens is not None:
        response.headers[_HEADER_TOTAL_TOKENS] = str(total_tokens)
    return run_output.content


# Serve a UI de chat estática (ver static/index.html) na mesma imagem Docker/processo da API —
# sem segundo serviço. Montado por último para que ``/health`` e ``/ask`` (declaradas acima)
# continuem resolvendo primeiro; o mount só responde pelos paths que essas rotas não capturam.
# Caminho absoluto (não relativo ao cwd) porque este módulo é importado tanto a partir da raiz
# do repo (dev local, ``uv run uvicorn ...``) quanto de dentro da imagem Docker — em ambos os
# casos, ``static/`` fica dois níveis acima deste arquivo (``src/data_agent/api.py`` -> raiz).
_STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
