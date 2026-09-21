# ADR-0010: Hospedagem do demo público (Render)

| Campo      | Valor              |
|------------|--------------------|
| **Status** | Aceito             |
| **Data**   | 2026-09-21         |

## Contexto

Até o Dia 7, a API só era exercitada localmente (`docker compose up`, `scripts/run_eval.py`
contra `http://localhost:8000`). O Dia 8 (opcional) pede um link público que possa ser
divulgado — a única forma de alguém além de mim ver o agente funcionando sem clonar o
repositório e rodar `uv sync`/`docker compose up` na própria máquina.

Isto é uma decisão de arquitetura não-trivial (critério do checklist em
[AGENTS.md](../../AGENTS.md)): escolher onde hospedar molda como `data/warehouse.duckdb`
chega ao container em produção (ver abaixo), como segredos chegam ao processo, e que
guard-rails de custo/abuso são obrigatórios antes de o link ser público.

### Por que não Railway, Fly.io ou Hugging Face Spaces

Este projeto já empacota a API inteira (agente + UI estática, ver Decisão abaixo) como uma
única imagem Docker (`Dockerfile`), então a plataforma só precisava fazer build-a-partir-de-
Dockerfile + expor uma URL HTTPS + permitir variáveis de ambiente como secrets. Três opções
comuns para isso foram descartadas, todas por restrições de plano gratuito no momento desta
decisão (setembro/2026), não por limitação técnica de suportar Docker:

- **Railway**: o crédito gratuito mensal não é suficiente para manter o serviço no ar o mês
  inteiro (o plano cobra por uso mesmo dentro do "free tier", e o crédito se esgota antes do
  fim do mês com um serviço sempre respondendo a health checks). Para um demo de portfólio que
  precisa ficar acessível por tempo indeterminado, isso significa o link cair no meio do mês.
- **Fly.io**: não oferece mais tier gratuito para contas novas — exige cartão de crédito
  cadastrado mesmo para o menor plano, o que não é aceitável para um projeto de portfólio sem
  orçamento dedicado.
- **Hugging Face Spaces**: Docker Spaces (o único tipo de Space compatível com uma imagem Docker
  arbitrária, em vez de um app Gradio/Streamlit) agora exigem plano pago — o tier gratuito de
  Spaces não cobre mais Docker.

O Render foi escolhido porque, no mesmo momento, ainda oferece um Web Service gratuito com
deploy direto de `Dockerfile` (sem exigir cartão), HTTPS automático, variáveis de ambiente como
secrets no painel (nunca commitadas) e um health check configurável — cobrindo exatamente o que
este projeto precisa, sem reescrever o empacotamento existente (`Dockerfile` +
[ADR-0008](0008-reprodutibilidade-com-uv-lock.md)).

### Atualização (mesmo dia): Git LFS trocado por S3 antes de ser usado

A decisão original para levar `data/warehouse.duckdb` (~140MB, acima do limite de 100MB do
GitHub para um blob normal) até o build do Render era Git LFS — a opção óbvia para "arquivo
grande dentro de um repositório Git", e o `README.md` chegou a documentar o passo a passo
(`git lfs install` + `git lfs track` + commit + push). Nenhum commit LFS chegou a ser feito;
antes disso, dois problemas do próprio LFS motivaram a troca para S3:

1. **Cota de bandwidth do LFS gratuito do GitHub (1GB/mês)**, consumida a cada *resolução* de
   ponteiro (checkout/clone que baixa o conteúdo real, não só o ponteiro). O Render provavelmente
   faz um clone fresco a cada deploy — múltiplos deploys durante o debug inicial do Render
   (esperado, é a primeira vez que este projeto builda lá) poderiam facilmente resolver o
   ponteiro LFS várias vezes e estourar essa cota, quebrando o deploy por um motivo desconectado
   de qualquer bug no código.
2. **Nenhuma confirmação de que o build do Render resolve ponteiros Git LFS corretamente** — a
   versão anterior deste ADR já registrava essa incerteza como um risco não verificável a partir
   deste ambiente de desenvolvimento.

S3 resolve os dois: o custo (poucos centavos de `GetObject` por deploy) é previsível e
desacoplado de "quantos deploys eu fiz este mês" (ao contrário de uma cota mensal fixa), e é uma
fonte de dado testável de forma independente do build do Render — `aws s3 cp` ou
`docker compose up --build` localmente validam a credencial/bucket antes de repetir a mesma
configuração no painel do Render, o que não era possível com "confiar que o Render resolve o
ponteiro LFS" sem um primeiro deploy real. A arquitetura de fundo não mudou: o warehouse
continua embutido na imagem, buscado durante o *build* (nunca em runtime/boot do container, o
que somaria atraso ao cold start já lento do free tier do Render).

### Atualização 2 (mesmo dia): fetch do S3 movido de build-time para runtime

A versão anterior desta decisão baixava o warehouse do S3 numa stage `s3-fetcher` durante
`docker build`, usando `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` como `ARG`. Testando essa
abordagem na sessão anterior deste projeto — não como suposição, como verificação real — ficou
confirmado com um Dockerfile mínimo de teste que `docker history --no-trunc` expõe o valor
completo de um `ARG` de credencial em texto puro na camada correspondente, mesmo sem nenhum
`ENV` correspondente (é exatamente o que o linter do BuildKit avisa como
`SecretsUsedInArgOrEnv`, e que a versão anterior deste ADR já registrava como um trade-off
aceito conscientemente). Isso é uma exposição de credencial real, não hipotética: qualquer um
com acesso à imagem buildada (não só ao `Dockerfile`) recupera a `AWS_SECRET_ACCESS_KEY` em
texto puro.

A correção: mover o fetch do S3 do *build* da imagem para o *boot* do container
(`data_agent.warehouse_fetch`, chamado pelo `CMD` do `Dockerfile` antes do `uvicorn`, via
`python -m data_agent.warehouse_fetch && exec uvicorn ...`). Nesse desenho, as credenciais
chegam como variáveis de ambiente de um *processo em execução* — nunca como valor gravado em
nenhuma camada de imagem, porque nenhum `ARG`/`docker build` as toca. O `Dockerfile` não
declara mais nenhum `ARG` de credencial.

Trade-offs explícitos dessa segunda troca:

- **O download agora pode acontecer a cada boot do container**, não só uma vez no build —
  inclusive potencialmente a cada vez que o serviço acorda do "sono" do free tier do Render
  (ver "Cold start" em Negativas/Trade-offs abaixo, com o que se sabe e não se sabe sobre isso).
  Mitigado parcialmente: `ensure_warehouse()` primeiro verifica se o arquivo já existe no
  caminho esperado (`Settings.duckdb_path`) e só baixa se não existir — localmente, o volume
  montado em `docker-compose.yml` garante que o fetch nunca dispara.
- **Falha de boot em vez de falha silenciosa na primeira pergunta.** Se a credencial estiver
  ausente ou o download falhar, `data_agent.warehouse_fetch.main()` sai com código != 0 antes
  do `uvicorn` subir (`&&` no `CMD`, não `;`) — o container inteiro falha ao iniciar, e não
  existe um estado em que `GET /health` responda `200` sobre um warehouse ausente/corrompido.
  Isso é estritamente melhor do que a alternativa considerada (checar a existência do arquivo
  dentro do próprio handler de `/health`): não depende de ninguém lembrar de chamar `/health`
  para descobrir o problema, e não deixa uma janela onde o processo está de pé mas incapaz de
  responder perguntas de verdade.
- **Nova dependência de runtime: `boto3`** (usado só por `data_agent.warehouse_fetch`, nunca
  pela aplicação FastAPI/agente). Escolhida em vez das duas opções originalmente cogitadas
  (`aws-cli` completo copiado para a imagem final, ou uma implementação manual de assinatura
  AWS Signature V4 via HTTP puro): o binário oficial do `aws-cli` (imagem
  `public.ecr.aws/aws-cli/aws-cli`) sozinho tem ~236MB (é uma distribuição Python própria
  embutida, redundante com o Python que a imagem já tem) — pesado demais para uma imagem final
  que agora é sensível a tempo de boot/cold start; já uma assinatura SigV4 manual é código de
  segurança escrito à mão, sem forma de testar contra o S3 real neste ambiente de
  desenvolvimento antes do primeiro deploy, um risco desproporcional para uma chamada de
  `GetObject`. `boto3`/`botocore` (~30MB juntos) ficam no meio: assinatura SigV4 testada e
  mantida pela AWS, não código deste projeto, e uma fração do tamanho do `aws-cli` completo.

## Decisão

1. **`render.yaml`** declara o Web Service como blueprint (`runtime: docker`, build a partir do
   `Dockerfile` existente, `plan: free`, `healthCheckPath: /health`). Toda variável sensível
   (`GROQ_API_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`) usa `sync: false`, forçando o
   Render a pedir o valor no painel na criação do serviço — nada de segredo versionado neste
   arquivo. O passo a passo de deploy está em `README.md`.
2. **A mesma imagem serve a UI e a API — sem segundo serviço.** `static/index.html` (chat
   mínimo em HTML/JS puro, sem framework) é servido por `src/data_agent/api.py` via
   `fastapi.staticfiles.StaticFiles`, montado na mesma app FastAPI depois das rotas `/health` e
   `/ask`. Rodar um segundo serviço estático (ex. um Static Site separado no Render) exigiria
   CORS entre os dois domínios e duplicaria o deploy só para servir um HTML — desnecessário para
   um chat que só faz uma chamada de API.
3. **`data_agent.warehouse_fetch` garante que `data/warehouse.duckdb` exista no boot do
   container**, baixando de um bucket S3 privado quando necessário (ver "Atualização 2"
   acima). Isto não existia até aqui (o warehouse só existia via volume montado em
   `docker-compose.yml`, ver comentário anterior em `README.md`). O plano free do Render não
   tem disco persistente nem forma de montar um volume do host — sem isto, a API sobe e passa
   no health check, mas todo `POST /ask` falharia por falta de dados. O bucket é privado (sem
   acesso público) e a credencial usada é de um usuário/role IAM com permissão mínima (só
   `s3:GetObject`, restrita ao bucket e à chave do objeto — sem `s3:ListBucket` nem qualquer
   permissão de escrita). Localmente, o volume declarado em `docker-compose.yml` faz o arquivo
   já existir quando o container sobe, então `ensure_warehouse()` nem chega a olhar para as
   variáveis `AWS_*`/`S3_*` — o fluxo de dev do dia a dia (`load_data.py` → `docker compose up`)
   não muda, e `docker build`/`docker compose build` não dependem de rede/credenciais AWS
   (diferente da versão anterior desta decisão).
4. **`Dockerfile`/`docker-compose.yml` passaram a escutar em `${PORT:-8000}`** (via `CMD ["sh",
   "-c", "uvicorn ... --port ${PORT:-8000}"]`) em vez de `8000` fixo — o Render injeta `PORT` em
   runtime e a aplicação precisa escutar nesse valor; localmente, sem `PORT` setado, o default
   `8000` preserva o comportamento de sempre.
5. **Guard-rails de custo/abuso antes de qualquer divulgação pública do link:**
   - **Rate limiting por IP** (`slowapi`, `Limiter(key_func=get_remote_address)`) em `POST /ask`
     — a única rota que dispara uma chamada real (e paga) ao provedor de LLM — fixado em
     `5/minute` no código (`data_agent/api.py`, não uma variável de ambiente, mesmo raciocínio
     do `id` do modelo em [ADR-0006](0006-troca-de-provedor-llm-para-groq.md): é suficiente para
     uma demo, e expor isso como configuração sugeriria um caso de uso que este projeto não
     tem). Sem isto, um link público sem autenticação é um jeito trivial de qualquer visitante
     (ou bot) esgotar o rate limit da Groq (ver
     [ADR-0009](0009-golden-dataset-e-metricas-de-avaliacao.md)) ou, pior, gerar uma conta
     inesperada se a chave configurada deixar de ser gratuita.
   - **Lembrete explícito (não automatizável por código) em `README.md`**: configurar um teto de
     gasto ("spend limit"/"usage limit") na chave do provedor de LLM no próprio painel da Groq
     antes de divulgar o link — rate limiting por IP reduz o volume de chamadas, mas não
     impede múltiplos IPs, nem substitui um teto de gasto real na origem do custo.

## Consequências

### Positivas

- Um link HTTPS público, estável, sem custo mensal e sem cartão de crédito cadastrado, cobrindo
  a última etapa do cronograma de portfólio (algo demonstrável sem pedir para quem for avaliar
  clonar o repositório).
- A imagem Docker continua sendo a única unidade de deploy (dev local via `docker compose up` e
  produção via Render usam exatamente o mesmo `Dockerfile`) — nenhuma lógica condicional de
  "ambiente de produção" precisou entrar no código da aplicação.
- Os guard-rails de rate limit + lembrete de teto de gasto são a barreira mínima, mas concreta,
  entre "link no ar" e "link responsavelmente divulgável" — sem eles, publicar o link seria
  assumir um risco de custo aberto.

### Negativas / Trade-offs

- **Requer criar e manter infraestrutura AWS fora deste repositório** (bucket S3 + usuário/role
  IAM + access key) antes do primeiro deploy — um passo manual que preciso fazer eu mesma (ver
  README.md), não algo que `Dockerfile`/`render.yaml` resolvem sozinhos. Rotação/expiração da
  access key IAM é uma responsabilidade operacional que não existia antes; se a credencial no
  painel do Render expirar ou for revogada, o serviço passa a falhar no boot até ser atualizada
  (ver bullet de "falha de boot" abaixo).
- **Cold start pode agora incluir o tempo de download do S3, não só o boot do container** — e
  não há confirmação, a partir deste ambiente de desenvolvimento, se o free tier do Render
  reaproveita o mesmo container (warehouse já baixado sobrevive) ou provisiona um novo a cada
  wake-up (download se repete). Ver "O que sabemos e o que não sabemos" no `README.md` — assume-
  se o pior caso (redownload a cada wake-up) até confirmar o comportamento real; `static/index.html`
  já avisa "até 1 minuto" com essa margem em mente.
- **Falha de boot em vez de resposta HTTP explícita.** Se o fetch falhar, o container não sobe
  (ver "Atualização 2") — correto para nunca servir `/health` como `200` sobre um warehouse
  ausente, mas também significa que quem for depurar um deploy quebrado precisa olhar os logs
  do container no painel do Render, não uma resposta de erro em alguma URL.
- **`5/minute` por IP é uma heurística, não uma solução de autenticação/autorização.** Não
  impede abuso distribuído (múltiplos IPs) nem substitui o teto de gasto configurado
  manualmente na chave do provedor — é uma primeira barreira, pensada para o volume de uma demo
  de portfólio, não para tráfego adversarial sério.
- **Escolha de plataforma amarrada ao estado atual dos planos gratuitos de cada provedor**
  (setembro/2026) — Railway, Fly.io e Hugging Face Spaces podem voltar a oferecer um tier
  gratuito compatível no futuro, e o Render pode mudar o seu; esta decisão não é permanente, só
  a melhor opção disponível no momento em que foi tomada.
