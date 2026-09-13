"""Contratos Pydantic centrais do agente (entrada/saída do agente e das tools)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    """Pergunta em linguagem natural feita pelo usuário ao agente."""

    question: str = Field(min_length=1)


class ToolQueryResult(BaseModel):
    """Dados brutos retornados pela tool ``query_sales``."""

    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


class SourceReference(BaseModel):
    """Uma tabela (e quantidade de linhas) que embasou a resposta do agente."""

    table: str
    row_count: int


class AgentAnswer(BaseModel):
    """Saída estruturada final do agente para uma pergunta do usuário."""

    status: Literal["answered", "insufficient_data", "out_of_scope"]
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)
    sql_used: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
