"""Aplicação FastAPI que expõe o agente via HTTP (ver
docs/adrs/0005-insufficient-data-como-resposta-valida.md para a decisão de
tratar ``status="insufficient_data"`` como resposta válida em 200, não como
erro).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

import groq
import structlog
from agno.agent import Agent
from agno.exceptions import ModelProviderError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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


@app.post("/ask", response_model=AgentAnswer)
def ask(request: AskRequest) -> AgentAnswer:
    """Responde ``request.question`` via o agente e suas tools de SQL controlado.

    ``request`` já chega validado como ``AskRequest`` (FastAPI devolve 422
    automaticamente para um corpo malformado antes de esta função rodar) e a
    resposta é validada como ``AgentAnswer`` na saída. ``status="insufficient_data"``
    e ``status="out_of_scope"`` são decisões válidas do agente e voltam com 200,
    como qualquer outra ``AgentAnswer`` — só uma falha real de infraestrutura
    (timeout do modelo, erro do provedor, uma tool/exceção que escapou do
    agente) vira um erro HTTP.
    """
    logger.info("agent_call_started", question=request.question)
    agent = get_agent()
    try:
        run_output = agent.run(request.question)
    except ModelProviderError as exc:
        is_timeout = isinstance(exc.__cause__, groq.APITimeoutError)
        logger.error(
            "agent_call_model_error",
            question=request.question,
            error=str(exc),
            timeout=is_timeout,
        )
        if is_timeout:
            raise HTTPException(
                status_code=504, detail="Timeout ao consultar o modelo de linguagem."
            ) from exc
        raise HTTPException(
            status_code=502, detail="Falha ao consultar o modelo de linguagem."
        ) from exc
    except Exception as exc:
        logger.error("agent_call_failed", question=request.question, error=str(exc))
        raise HTTPException(
            status_code=502,
            detail="Falha inesperada ao executar o agente ou uma de suas tools.",
        ) from exc

    if not isinstance(run_output.content, AgentAnswer):
        logger.error(
            "agent_call_invalid_output",
            question=request.question,
            content=run_output.content,
        )
        raise HTTPException(
            status_code=502,
            detail="O modelo não retornou uma resposta estruturada válida.",
        )

    logger.info(
        "agent_call_completed",
        question=request.question,
        status=run_output.content.status,
    )
    return run_output.content
