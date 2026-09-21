# syntax=docker/dockerfile:1

# --- Stage "builder": resolve e instala dependências de produção a partir do
# uv.lock, sem tocar em nenhum índice de pacotes fora do que já está travado
# (`uv sync --frozen` falha em vez de re-resolver se uv.lock estiver
# desatualizado em relação ao pyproject.toml — build determinístico, ver
# docs/adrs/0008-reprodutibilidade-com-uv-lock.md).
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Camada de dependências isolada do código-fonte: só reinstala pacotes quando
# pyproject.toml/uv.lock mudam, não a cada alteração em src/.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Agora instala o próprio pacote data-agent (sem reinstalar dependências).
COPY src/ src/
COPY README.md ./
RUN uv sync --frozen --no-dev

# --- Stage final: imagem de execução, sem uv nem cache de build.
FROM python:3.12-slim

RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH"

COPY --from=builder /app/.venv /app/.venv
COPY src/ src/

RUN mkdir -p /app/data && chown app:app /app/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "data_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
