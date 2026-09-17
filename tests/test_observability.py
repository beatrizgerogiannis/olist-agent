import base64

import pytest

from data_agent import observability
from data_agent.config import Settings


def _settings(**overrides: str) -> Settings:
    defaults: dict[str, str] = {
        "langfuse_host": "https://example.langfuse.test",
        "langfuse_public_key": "pk-test",
        "langfuse_secret_key": "sk-test",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_otlp_endpoint_appends_langfuse_otel_traces_path() -> None:
    settings = _settings(langfuse_host="https://example.langfuse.test")

    assert (
        observability._otlp_endpoint(settings)
        == "https://example.langfuse.test/api/public/otel/v1/traces"
    )


def test_otlp_endpoint_strips_trailing_slash_from_host() -> None:
    settings = _settings(langfuse_host="https://example.langfuse.test/")

    assert (
        observability._otlp_endpoint(settings)
        == "https://example.langfuse.test/api/public/otel/v1/traces"
    )


def test_otlp_headers_encodes_public_and_secret_key_as_http_basic_auth() -> None:
    # Langfuse autentica o endpoint OTLP com Basic Auth (public key : secret
    # key), não um Bearer token — ver docstring de `observability._otlp_headers`.
    settings = _settings(langfuse_public_key="pk-abc", langfuse_secret_key="sk-xyz")

    headers = observability._otlp_headers(settings)

    scheme, _, token = headers["Authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(token).decode("utf-8") == "pk-abc:sk-xyz"


class _FakeAgnoInstrumentor:
    """Substituto de ``AgnoInstrumentor`` injetado via monkeypatch nos testes abaixo.

    Evita instrumentar o Agno de verdade: ``AgnoInstrumentor`` é um singleton
    (``BaseInstrumentor.__new__`` sempre devolve a mesma instância) que só loga
    um warning e não faz nada na 2ª chamada a ``.instrument()`` no mesmo
    processo. Como ``data_agent.api`` (importado por ``tests/test_api.py``) já
    chama ``configure_observability()`` de verdade na importação do módulo,
    testar aqui contra o ``AgnoInstrumentor`` real deixaria estes testes
    acoplados à ordem de coleta dos arquivos de teste pelo pytest.
    """

    def __init__(self) -> None:
        self.instrument_calls: list[dict[str, object]] = []

    def instrument(self, **kwargs: object) -> None:
        self.instrument_calls.append(kwargs)


@pytest.fixture
def fake_instrumentor(monkeypatch: pytest.MonkeyPatch) -> _FakeAgnoInstrumentor:
    instance = _FakeAgnoInstrumentor()
    monkeypatch.setattr(observability, "AgnoInstrumentor", lambda: instance)
    return instance


@pytest.fixture(autouse=True)
def _spy_on_global_tracer_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # `trace.set_tracer_provider` só tem efeito real na 1ª chamada por processo
    # (chamadas seguintes só logam um warning e são ignoradas) — pelo mesmo
    # motivo do `fake_instrumentor` acima, substituímos por um espião em vez de
    # depender de sermos a primeira chamada do processo inteiro de testes.
    monkeypatch.setattr(observability.trace, "set_tracer_provider", lambda provider: None)


def test_configure_observability_points_otlp_exporter_at_langfuse(
    fake_instrumentor: _FakeAgnoInstrumentor,
) -> None:
    settings = _settings(
        langfuse_host="https://example.langfuse.test",
        langfuse_public_key="pk-abc",
        langfuse_secret_key="sk-xyz",
    )

    tracer_provider = observability.configure_observability(settings)

    span_processor = tracer_provider._active_span_processor._span_processors[0]
    exporter = span_processor.span_exporter
    assert exporter._endpoint == "https://example.langfuse.test/api/public/otel/v1/traces"
    token = exporter._session.headers["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(token).decode("utf-8") == "pk-abc:sk-xyz"


def test_configure_observability_instruments_agno_with_the_tracer_provider(
    fake_instrumentor: _FakeAgnoInstrumentor,
) -> None:
    tracer_provider = observability.configure_observability(_settings())

    assert len(fake_instrumentor.instrument_calls) == 1
    assert fake_instrumentor.instrument_calls[0]["tracer_provider"] is tracer_provider


def test_configure_observability_uses_get_settings_by_default(
    monkeypatch: pytest.MonkeyPatch, fake_instrumentor: _FakeAgnoInstrumentor
) -> None:
    fake_settings = _settings(langfuse_host="https://from-get-settings.test")
    monkeypatch.setattr(observability, "get_settings", lambda: fake_settings)

    tracer_provider = observability.configure_observability()

    span_processor = tracer_provider._active_span_processor._span_processors[0]
    assert (
        span_processor.span_exporter._endpoint
        == "https://from-get-settings.test/api/public/otel/v1/traces"
    )
