"""Configuração de OpenTelemetry + Langfuse (ver
docs/adrs/0007-observabilidade-com-langfuse.md).
"""

from __future__ import annotations

import base64

from openinference.instrumentation.agno import AgnoInstrumentor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from data_agent.config import Settings, get_settings

_SERVICE_NAME = "data-agent"

# O endpoint OTLP do Langfuse é `<LANGFUSE_HOST>/api/public/otel/v1/traces` (ver
# https://langfuse.com/docs/opentelemetry/get-started). Só existe um caminho
# porque `OTLPSpanExporter(endpoint=...)` é passado explicitamente aqui (em vez de
# via variável de ambiente `OTEL_EXPORTER_OTLP_ENDPOINT`) — o exportador só
# acrescenta `/v1/traces` sozinho quando o endpoint vem do env var padrão do
# OpenTelemetry (`_append_trace_path` em
# opentelemetry.exporter.otlp.proto.http.trace_exporter), não quando é passado
# como argumento; sem montar o caminho completo aqui, os traces cairiam num
# endpoint errado silenciosamente (a exportação falha em background, sem
# levantar exceção em `configure_observability`).
_OTLP_TRACES_PATH = "/api/public/otel/v1/traces"


def _otlp_endpoint(settings: Settings) -> str:
    return f"{settings.langfuse_host.rstrip('/')}{_OTLP_TRACES_PATH}"


def _otlp_headers(settings: Settings) -> dict[str, str]:
    """Monta o header exigido pelo Langfuse para autenticar requisições OTLP.

    Langfuse autentica o endpoint `/api/public/otel` com HTTP Basic Auth —
    usuário = public key, senha = secret key — não um Bearer token nem uma
    chave única (ver LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY em .env.example).
    """
    credentials = f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
    token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def configure_observability(settings: Settings | None = None) -> TracerProvider:
    """Configura um ``TracerProvider`` OTLP apontado para o Langfuse e instrumenta o Agno.

    Chamada uma única vez, na importação de ``data_agent.api`` (mesmo padrão de
    ``_configure_structlog`` nesse módulo): depois disso, toda chamada a
    ``Agent.run``/``Agent.arun`` do Agno (incluindo o ``parser_model`` — ver
    docs/adrs/0006-troca-de-provedor-llm-para-groq.md), cada chamada ao modelo, e
    cada execução das tools de SQL controlado (``tools/sql_tools.py``) geram spans
    automaticamente via ``AgnoInstrumentor`` (pacote
    ``openinference-instrumentation-agno``), sem precisar instrumentar manualmente
    nenhum desses pontos.

    Não valida credenciais nem faz nenhuma chamada de rede aqui: criar um
    ``OTLPSpanExporter`` é local (só guarda endpoint/headers); a exportação de
    verdade acontece de forma assíncrona, em background, pelo
    ``BatchSpanProcessor``, e falhas de rede/autenticação são log
    (``opentelemetry.exporter.otlp...``) em vez de exceção — por isso
    ``configure_observability`` nunca levanta por causa de credenciais erradas do
    Langfuse, e por isso a confirmação de que os traces chegam de verdade só pode
    ser feita olhando o dashboard do Langfuse (ver ADR-0007), não por teste
    automatizado.
    """
    settings = settings or get_settings()

    tracer_provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: _SERVICE_NAME}),
    )
    exporter = OTLPSpanExporter(
        endpoint=_otlp_endpoint(settings),
        headers=_otlp_headers(settings),
    )
    tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(tracer_provider)
    AgnoInstrumentor().instrument(tracer_provider=tracer_provider)

    return tracer_provider
