# ADR-0008: Reprodutibilidade de build via `uv.lock` e `uv sync --frozen`

| Campo      | Valor              |
|------------|--------------------|
| **Status** | Aceito             |
| **Data**   | 2026-09-17         |

## Contexto

Até o Dia 5, o projeto não tinha `Dockerfile`/`docker-compose.yml`: dependências eram
sincronizadas com `uv sync` só no ambiente de desenvolvimento, sem nenhum processo de build de
imagem que fixasse exatamente quais versões de pacotes (incluindo transitivos) terminam em
produção. Nada impedia `pyproject.toml` de divergir do que o código realmente usa — e havia um
exemplo concreto disso já no repositório: `pyproject.toml` listava `"openai>=3.13.0"` como
dependência direta, sobra da migração de provedor de LLM para Groq
([ADR-0006](0006-troca-de-provedor-llm-para-groq.md)), sem nenhum import real de
`openai`/`agno.models.openai.OpenAIChat` em `src/` ou `scripts/` (confirmado via
`grep -rn "openai\|OpenAIChat" src/ scripts/ tests/` antes de remover a linha — as únicas
ocorrências restantes eram o *id* do modelo Groq, `"openai/gpt-oss-120b"`, que é um modelo *da
Groq* nomeado com esse prefixo, não um uso da biblioteca `openai`).

Uma dependência órfã sozinha não quebra nada em produção, mas é sintoma de um problema mais
geral: sem uma etapa de build que trave versões exatas a partir de um lockfile versionado, é
fácil o manifesto de dependências carregar peso morto (ou, na direção oposta, faltar algo que só
foi instalado manualmente num ambiente de dev) sem que isso apareça em nenhuma checagem — o
projeto já tem `uv.lock` desde o Dia 1, mas nada até agora obrigava um processo de build a
respeitá-lo estritamente.

## Decisão

1. **Remover a dependência órfã antes de empacotar.** `openai>=3.13.0` saiu de
   `pyproject.toml`; `uv sync` foi rodado localmente e o diff resultante em `uv.lock` confirma
   que `openai` e os pacotes transitivos exclusivos dela (`httpx2`, `httpcore2`,
   `httpx2-jsfetch`, `jiter`, `truststore`) saíram do lock sem afetar a resolução de mais nenhum
   pacote — `make check` (lint + type-check + security-check + 119 testes, 100% de cobertura em
   `src/`) roda limpo depois da remoção.

2. **`Dockerfile` multi-stage, com `uv sync --frozen` como único mecanismo de instalação.**
   `--frozen` faz `uv` **falhar** (em vez de re-resolver silenciosamente) se `uv.lock` estiver
   desatualizado em relação a `pyproject.toml` — drift entre os dois é pego no build, não
   descoberto em produção. O stage `builder`:
   - Primeiro sincroniza só a partir de `pyproject.toml` + `uv.lock`
     (`uv sync --frozen --no-dev --no-install-project`), numa camada isolada de `src/` — o
     Docker só reinstala dependências quando esses dois arquivos mudam, não a cada edição de
     código.
   - Só depois copia `src/` (e `README.md`, exigido por `readme = "README.md"` em
     `[project]` para o build do próprio pacote via `hatchling`) e roda
     `uv sync --frozen --no-dev` de novo, agora instalando o pacote `data-agent` em si.
   - `--no-dev` garante que `ruff`/`mypy`/`bandit`/`pytest` (grupo `dev` em
     `[dependency-groups]`) nunca entram na imagem de produção.

   O stage final copia só o `.venv` já construído e `src/` do `builder` — sem `uv`, sem cache de
   build, sem as camadas intermediárias — roda como usuário não-root (`app`), expõe a porta 8000
   e declara um `HEALTHCHECK` batendo em `GET /health`.

3. **O warehouse não é embutido na imagem.** `data/warehouse.duckdb` (gerado localmente por
   `uv run python scripts/load_data.py`, nunca commitado — ver `.gitignore`) é montado como
   volume pelo `docker-compose.yml`, com `DUCKDB_PATH` sobrescrito para o caminho dentro do
   container. Empacotar o warehouse na imagem acopraria o ciclo de vida da imagem (que muda a
   cada deploy de código) ao dos dados (que muda quando `scripts/load_data.py` roda de novo), e
   inflaria a imagem em ~140MB sem necessidade — o fluxo de dev documentado em `AGENTS.md`
   (`uv sync` → `cp .env.example .env` → `uv run python scripts/load_data.py` →
   `docker compose up`) já assume o warehouse gerado antes de subir o container.

## Consequências

### Positivas

- Build determinístico: a mesma `uv.lock` produz exatamente as mesmas versões de pacotes
  (diretos e transitivos) em qualquer máquina, em qualquer momento — sem o resolvedor do `uv`
  rodando de novo dentro do build e potencialmente escolhendo versões diferentes das usadas em
  dev.
- Falha rápida e explícita (erro de build) em vez de silenciosa: se alguém editar
  `pyproject.toml` e esquecer de rodar `uv sync` para atualizar `uv.lock`, o build da imagem
  Docker quebra imediatamente com `--frozen`, em vez de a imagem subir com uma versão diferente
  da testada localmente.
- Cache de camadas do Docker eficiente: a camada de dependências (mais pesada, ~14s neste
  projeto) só é reconstruída quando `pyproject.toml`/`uv.lock` mudam; iterar em `src/` reusa essa
  camada.
- Imagem final enxuta e sem superfície de dev: sem `uv`, sem ferramentas de lint/type-check/teste,
  sem cache de build — só o runtime necessário para servir a API.
- Menos uma dependência (e os transitivos que só ela trazia) para acompanhar por CVE/atualização,
  sem nenhuma perda de funcionalidade — nada no código a importava.

### Negativas / Trade-offs

- `--frozen` exige disciplina manual: depois de editar `pyproject.toml`, é preciso rodar
  `uv sync` localmente e commitar o `uv.lock` atualizado antes do build da imagem funcionar. Isso
  não é verificado automaticamente hoje — `.github/workflows/pr-quality-checks.yml` roda
  `uv sync --group dev` (sem `--frozen`) nos jobs de CI, então uma divergência entre
  `pyproject.toml` e `uv.lock` passaria pela CI sem erro (o `uv sync` da CI simplesmente
  re-resolveria e atualizaria o lock em memória) e só seria pega ao construir a imagem Docker,
  que não roda como parte do workflow de PR.
- A imagem sozinha não é "self-contained" para rodar o agente de ponta a ponta: sem o volume do
  warehouse montado (ver decisão 3), a API sobe (`GET /health` responde) mas `POST /ask` falha em
  qualquer tool que precise do DuckDB, porque `data/db.py::get_connection` levanta
  `FileNotFoundError` se o arquivo não existir no caminho configurado.
- `COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /uvx ...` fixa a versão do `uv` usada dentro do
  build por tag explícita (não por digest de conteúdo) — trocar essa tag no futuro poderia trazer
  um `uv` com comportamento de resolução diferente do usado em desenvolvimento local, sem que
  nada neste repositório detecte a mudança automaticamente.
