"""Testes sobre o texto de ``data_agent.prompts.SYSTEM_PROMPT`` em si (não sobre o
comportamento do LLM, que não é determinístico) — ver regra 8 (formatação numérica) e
regra 3 (filtros categóricos), adicionadas para corrigir os achados de sanidade manual
contra a demo pública documentados em `docs/adrs/0004-estrategia-anti-alucinacao.md`.
"""

from __future__ import annotations

from data_agent.prompts import SYSTEM_PROMPT
from data_agent.tools.guardrails import TABLE_ORDER


def test_system_prompt_instructs_pt_br_thousands_separator_for_integer_counts() -> None:
    # Achado do teste de sanidade manual: perguntas de contagem simples voltaram sem
    # separador de milhar ("45101") ou com espaço ("96 478") em vez do padrão pt-BR
    # ("45.101"). A regra 8 precisa deixar explícito que isso vale mesmo para inteiros
    # sem casas decimais, não só para valores monetários — daí o exemplo concreto de
    # contagem ("45.101") no próprio prompt.
    assert "separador de milhar" in SYSTEM_PROMPT
    assert '"45.101"' in SYSTEM_PROMPT
    assert "mesmo para contagens inteiras" in SYSTEM_PROMPT


def test_system_prompt_requires_confirming_categorical_entity_exists_before_zero_rows() -> None:
    # Achado do teste de sanidade manual: "Distrito Federal Sul" (nome plausível, mas
    # não é uma UF real) voltou como status="answered"/confidence=1.0 com "0 vendas" em
    # vez de insufficient_data — a regra 3 não deixava explícito que zero linhas num
    # filtro categórico exige confirmar a existência do valor antes de responder.
    assert "SELECT DISTINCT" in SYSTEM_PROMPT
    assert "filtros categóricos" in SYSTEM_PROMPT
    assert "Distrito Federal Sul" in SYSTEM_PROMPT


def test_system_prompt_embeds_the_full_warehouse_schema() -> None:
    # docs/adrs/0006-troca-de-provedor-llm-para-groq.md, seção "Otimização de
    # latência" (2026-09-22): `get_schema` deixou de ser uma tool — o schema
    # estático das 8 tabelas foi embutido como texto no prompt para eliminar um
    # turno inteiro de ida-e-volta à Groq só para descobrir algo que nunca muda.
    # Usa `TABLE_ORDER` (a mesma lista que `tools/guardrails.py` usa para a
    # allowlist) em vez de hardcodar os 8 nomes de novo, para não divergir se a
    # allowlist mudar.
    for table_name in TABLE_ORDER:
        assert f"`{table_name}`" in SYSTEM_PROMPT

    # Uma coluna "âncora" por tabela — pega regressão de alguém remover o bloco de
    # uma tabela inteira do schema embutido sem tirá-la de `TABLE_ORDER` junto.
    assert "customer_unique_id" in SYSTEM_PROMPT
    assert "seller_state" in SYSTEM_PROMPT
    assert "product_name_lenght" in SYSTEM_PROMPT
    assert "geolocation_lat" in SYSTEM_PROMPT
    assert "order_delivered_customer_date" in SYSTEM_PROMPT
    assert "freight_value" in SYSTEM_PROMPT
    assert "payment_installments" in SYSTEM_PROMPT
    assert "review_score" in SYSTEM_PROMPT


def test_system_prompt_no_longer_calls_out_to_a_get_schema_tool() -> None:
    # O ponto inteiro da otimização de latência (ver teste acima) é eliminar essa
    # tool do loop — se "get_schema" reaparecer no prompt, a instrução de "não
    # existe (nem chame) uma tool para descobri-lo" foi perdida numa edição futura.
    assert "get_schema" not in SYSTEM_PROMPT
    assert "a tool `query_sales`" in SYSTEM_PROMPT
