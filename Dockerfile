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
COPY static/ static/

# /app/data existe mas fica vazio no build — data/warehouse.duckdb (~140MB) NÃO é copiado
# para a imagem nem baixado durante o build. Ver docs/adrs/0010-hospedagem-do-demo-publico.md,
# seção "Atualização 2": uma versão anterior deste Dockerfile baixava o warehouse do S3 numa
# stage intermediária durante o build, usando credenciais AWS como `ARG` — confirmado
# empiricamente (via `docker history --no-trunc`) que isso grava as credenciais em texto puro
# no histórico de camadas da imagem final, mesmo sem nenhum `ENV` correspondente. O fetch foi
# movido para o boot do container (`data_agent.warehouse_fetch`, chamado pelo `CMD` abaixo,
# antes do `uvicorn`), onde as credenciais só existem como variável de ambiente do processo em
# execução — nunca em nenhuma camada da imagem. Custo: o cold start do Render (já lento no
# free tier) agora também pode incluir o tempo desse download — ver README.md.
RUN mkdir -p /app/data && chown -R app:app /app/data

USER app

# EXPOSE é só documentação da imagem — o container escuta em $PORT (default 8000 no
# docker-compose local; o Render injeta o valor real dele em runtime, ver render.yaml e
# docs/adrs/0010-hospedagem-do-demo-publico.md).
EXPOSE 8000
ENV PORT=8000

# --start-period=60s (não 10s) para cobrir o pior caso: boot do container + download do
# warehouse do S3 quando o arquivo não vem de um volume já populado (ver
# data_agent/warehouse_fetch.py) — mesmo orçamento de tempo que a UI já promete em
# static/index.html ("até 1 minuto"). Falhas de healthcheck dentro do start-period não contam
# para --retries, então isto não deixa um boot lento ser marcado unhealthy prematuramente.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=3)" || exit 1

# `python -m data_agent.warehouse_fetch` roda uma vez, antes do `uvicorn` — garante que o
# warehouse exista (baixando do S3 se preciso) ou sai com código != 0 e loga o motivo (env var
# ausente, erro do S3). Com `&&` (não `;`), um fetch que falha impede o `uvicorn` de subir:
# não existe um estado em que a API responda 200 em /health sobre um warehouse ausente ou
# corrompido — o container inteiro falha ao iniciar, o que Docker/Render reportam como o
# serviço não subindo, nunca como "healthy" mentiroso. `exec` substitui o processo do shell
# pelo do uvicorn (PID 1 correto, sinais de shutdown do Docker/Render chegam nele direto).
CMD ["sh", "-c", "python -m data_agent.warehouse_fetch && exec uvicorn data_agent.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
