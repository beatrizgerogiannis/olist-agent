from typing import Any

import pytest
from pydantic import ValidationError

from data_agent.schemas import AgentAnswer, SourceReference


def _valid_kwargs() -> dict[str, Any]:
    return {
        "status": "answered",
        "answer": "O total de vendas foi R$ 100,00.",
        "confidence": 0.9,
    }


@pytest.mark.parametrize("status", ["answered", "insufficient_data", "out_of_scope"])
def test_agent_answer_accepts_every_literal_status_value(status: str) -> None:
    answer = AgentAnswer(**{**_valid_kwargs(), "status": status})

    assert answer.status == status


def test_agent_answer_rejects_status_outside_literal() -> None:
    with pytest.raises(ValidationError):
        AgentAnswer(**{**_valid_kwargs(), "status": "maybe"})


@pytest.mark.parametrize("missing_field", ["status", "answer", "confidence"])
def test_agent_answer_requires_mandatory_fields(missing_field: str) -> None:
    kwargs = _valid_kwargs()
    del kwargs[missing_field]

    with pytest.raises(ValidationError):
        AgentAnswer(**kwargs)


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_agent_answer_rejects_confidence_outside_unit_interval(confidence: float) -> None:
    with pytest.raises(ValidationError):
        AgentAnswer(**{**_valid_kwargs(), "confidence": confidence})


def test_agent_answer_defaults_sql_used_and_sources_to_empty_lists() -> None:
    answer = AgentAnswer(**_valid_kwargs())

    assert answer.sql_used == []
    assert answer.sources == []


def test_agent_answer_accepts_sql_used_and_sources() -> None:
    answer = AgentAnswer(
        **_valid_kwargs(),
        sql_used=["SELECT sum(price) FROM order_items"],
        sources=[SourceReference(table="order_items", row_count=112650)],
    )

    assert answer.sql_used == ["SELECT sum(price) FROM order_items"]
    assert answer.sources == [SourceReference(table="order_items", row_count=112650)]
