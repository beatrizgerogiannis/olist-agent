# ADR-0009: Dataset golden com perguntas-armadilha como critério de sucesso

| Campo      | Valor              |
|------------|--------------------|
| **Status** | Aceito             |
| **Data**   | 2026-09-17         |

## Contexto

Desde o Dia 3, `tests/golden_questions.jsonl` existia com 10 perguntas (5 respondíveis + 5
armadilhas de período/entidade inexistente/métrica fora de escopo), cada uma com
`expected_status` e, quando aplicável, `expected_value` conferidos manualmente contra
`data/warehouse.duckdb` antes de serem gravadas no arquivo. Como o próprio
[ADR-0004](0004-estrategia-anti-alucinacao.md) já registrava explicitamente, isso era só um
gabarito estático: nada no repositório executava essas perguntas contra o agente de verdade.
"O agente não alucina" era, até aqui, uma alegação sustentada por teste manual ad hoc — rodar
perguntas à mão contra a API e olhar a resposta —, não um número reproduzível, versionado, e
comparável entre dias.

Ao construir a malha de avaliação automatizada prevista para o Dia 6, dois problemas
adicionais precisaram ser resolvidos antes de qualquer número ter significado:

1. **O tier gratuito da Groq tem rate limit apertado** (8000 tokens/minuto, já observado no
   Dia 5 causando respostas de 30-90s sob carga — ver [ADR-0006](0006-troca-de-provedor-llm-para-groq.md)).
   Rodar 26 perguntas em sequência contra a Groq real estoura esse limite sistematicamente; sem
   tratamento, isso apareceria como respostas malformadas ou erros HTTP indistinguíveis de uma
   falha real de raciocínio do agente, contaminando qualquer métrica de qualidade com ruído de
   infraestrutura.
2. **Uma falha de infraestrutura pode se disfarçar de falha de qualidade do modelo.** Rodando a
   primeira versão de `scripts/run_eval.py` contra a API local sob carga real da Groq, várias
   perguntas voltaram como `502` com `detail="O modelo não retornou uma resposta estruturada
   válida."` — a categoria que `data_agent/api.py` usa quando `run_output.content` não é uma
   `AgentAnswer`. Inspecionando os logs estruturados do `structlog` no processo da API
   (`agent_call_invalid_output`), o `content` de cada uma dessas respostas não era um JSON
   malformado nem texto solto do modelo: era o **corpo de erro cru da própria Groq**
   (``{"error":{"message":"Rate limit reached for model \`openai/gpt-oss-120b\`...",
   "type":"tokens","code":"rate_limit_exceeded"}}``). Ou seja: quando o `parser_model` (ver
   ADR-0006) falha com um erro do provedor, o Agno não levanta exceção — o mesmo comportamento
   de "loga um warning e deixa a string crua passar" que ADR-0004 já documentava para JSON
   malformado também se aplica a uma falha de infraestrutura na chamada do `parser_model`, não
   só a uma resposta do modelo que não bate com o schema. Sem tratar esse caso, um rate limit
   apareceria na avaliação como "o modelo produziu saída não estruturada" — uma categoria de
   falha de *qualidade*, quando na real é a mesma falha de *infraestrutura* que o branch de
   `ModelProviderError` já trata como `502`/`504` quando é levantada como exceção em vez de
   aparecer como conteúdo. Isso inflaria artificialmente qualquer contagem de "falha do agente"
   e poderia levar a ajustar o `SYSTEM_PROMPT` para um problema que não existe.

## Decisão

### 1. Expandir o dataset golden de 10 para 26 perguntas

`tests/golden_questions.jsonl` agora tem 16 perguntas respondíveis e 10 armadilhas
(`insufficient_data`/`out_of_scope`), mantendo a mesma disciplina do Dia 3: todo `expected_value`
foi conferido rodando a query SQL correspondente contra `data/warehouse.duckdb` em 2026-09-17,
documentada no campo `notes` de cada linha. A expansão cobre mais dimensões do escopo funcional
(`docs/architecture.md`) — frete, cancelamentos, parcelas de pagamento, distribuição geográfica de
vendedores, prazo médio de entrega — e duas categorias de armadilha que ainda não existiam no
Dia 3: uma armadilha de período na borda exata do fim dos dados coberta (pedidos de novembro de
2018, o mês imediatamente após o corte real da base em 17/out/2018, complementar à armadilha de
dezembro de 2019 já existente) e uma armadilha de ação de escrita (pedir para cancelar um pedido).

Crucialmente, `q11` (**"Quantos pedidos foram feitos em outubro de 2018?"**, resposta correta: 4)
foi incluída de propósito como rede de segurança de regressão para uma classe específica de bug
de guard-rail: `tools/guardrails.py::_check_allowed_tables` já teve um falso positivo real onde
`COUNT(*)`/`COUNT(coluna)` sobre uma tabela inteira virava um nó físico `COLUMN_DATA_SCAN` sem
`Table` em `extra_info`, sendo bloqueado por engano como fonte de dados não reconhecida (ver
docstring de `_check_allowed_tables` e [ADR-0002](0002-camada-de-dados.md)) — corrigido lendo o
plano lógico em vez do físico. `q11` é exatamente esse padrão (`COUNT(*)` com filtro de data) numa
pergunta com resposta pequena e não óbvia (4, não um número redondo), então o dataset golden agora
serve dois papéis: gabarito de qualidade de resposta **e** rede de segurança de regressão para bugs
de guard-rail já corrigidos — não só os testes unitários dedicados em `tests/test_tools.py`.

### 2. `scripts/run_eval.py`: três métricas, com falha de infraestrutura explicitamente excluída

O script roda o dataset inteiro contra `POST /ask` de uma API já no ar (local ou dockerizada, via
`--base-url`) e calcula, sobre os itens efetivamente avaliados:

- **Taxa de acerto**: `status` bate com `expected_status` e, quando `"answered"`, o número
  esperado aparece no texto de `answer` dentro da tolerância declarada.
- **Taxa de alucinação**: entre as perguntas-armadilha, a fração em que o agente devolveu
  `status="answered"` (respondeu com um número) em vez de recusar.
- **Taxa de recusa indevida**: entre as perguntas respondíveis, a fração em que o agente devolveu
  `status="insufficient_data"` apesar de o dado existir.

Um item só entra nesses três denominadores se não for classificado como falha de infraestrutura.
`_is_infra_response` (`scripts/run_eval.py`) reconhece isso pelo `status_code`/`detail` que
`data_agent/api.py::ask` já expõe (504 é sempre timeout; 502 com
`detail="Falha ao consultar o modelo de linguagem."` é sempre erro do provedor, incluindo rate
limit) — nesses casos, `ask_with_retries` tenta de novo com backoff exponencial
(`--backoff-base-seconds`/`--backoff-factor`/`--backoff-max-seconds`, default 5s/2x/60s) antes de
desistir, e todas as chamadas são espaçadas por `--throttle-seconds` (default 2s) para reduzir a
chance de bater no rate limit da Groq em primeiro lugar. Itens que precisaram de qualquer retry
são listados à parte em `docs/eval_report.md`, separado das três métricas principais, como pedido.

### 3. Corrigir `data_agent/api.py` para não disfarçar rate limit de saída não estruturada

Para que a distinção do item 2 seja possível de verdade (não só na teoria), `ask()` agora checa,
antes de cair no branch genérico de "saída não estruturada", se o conteúdo cru devolvido pelo Agno
é o corpo de erro de um provedor (`_is_provider_error_payload`: um JSON com uma chave `"error"` no
nível raiz — a forma que a Groq usa para erros, bem diferente da forma de um `AgentAnswer`). Se for,
a resposta vira `502` com o mesmo `detail="Falha ao consultar o modelo de linguagem."` já usado
pelo branch de `ModelProviderError` sem timeout — a mesma categoria de falha, tratada de forma
idêntica, não importa se ela chegou como exceção (do modelo principal) ou como conteúdo (do
`parser_model`). Coberto por
`tests/test_api.py::test_ask_parser_model_provider_error_as_content_returns_502_as_provider_error`,
reproduzindo o payload de erro real observado nos logs.

### 4. Comparar números em texto livre, não em formato estruturado

`AgentAnswer.answer` é texto livre em português — não há um campo numérico estruturado para
comparar diretamente contra `expected_value`. `extract_numbers` (`scripts/run_eval.py`) usa uma
regex para achar candidatos numéricos no texto e tenta as duas leituras de separador (BR: ponto de
milhar, vírgula decimal; US: vírgula de milhar, ponto decimal), aceitando a resposta como correta
se qualquer uma bater com `expected_value` dentro de `tolerance`. Isso mantém a checagem simples e
não acopla a avaliação a uma convenção de formatação específica do modelo — o custo é que o
critério é sintático (o número certo aparece em algum lugar do texto), não semântico: não valida se
o agente respondeu à pergunta certa com aquele número, só que o número certo apareceu na resposta.

### 5. Resultado da primeira execução e ajuste de prompt daí decorrente

A primeira execução completa de `scripts/run_eval.py` contra as 26 perguntas (Groq real, API
local) deu **92,3% de taxa de acerto (24/26)**, **10% de taxa de alucinação (1/10
armadilhas)** e **0% de taxa de recusa indevida (0/16 respondíveis)**, sem nenhuma falha de
infraestrutura contaminando esses números (0/26) — ver `docs/eval_report.md`. As duas falhas
tiveram causa raiz confirmada nos logs estruturados da API, não só suspeitada:

- **`q22` (hallucination)**: "Quantos pedidos foram feitos em novembro de 2018?" — o agente
  rodou corretamente um `COUNT(*)` filtrado por novembro/2018, recebeu `row_count == 1` com
  valor `0` (a query sempre devolve 1 linha, mesmo sem nenhum pedido no período) e respondeu
  `answered` com "0 pedidos". A regra 3 do `SYSTEM_PROMPT` só instruía recusa explícita para
  `row_count == 0`, sem cobrir o caso de uma agregação sobre um período fora da janela coberta
  que devolve `1` linha com um valor "zero"/`NULL`. `SYSTEM_PROMPT` foi ajustado para tratar
  esse padrão como o mesmo sinal de dado insuficiente.
- **`q21` (other_mismatch)**: "Qual o prazo médio de entrega, em dias, para pedidos
  efetivamente entregues?" — o agente tentou `julianday(...)` (função do SQLite, não existe no
  DuckDB — `SqlGuardrailError` na validação do plano), depois recorreu a subtrair dois
  `TIMESTAMP` diretamente, que no DuckDB devolve um `INTERVAL` formatado (ex. `"12 days
  11:56:49.324"`), não um número de dias — o texto de `answer` não continha o valor numérico
  esperado (12,4968). `SYSTEM_PROMPT` foi ajustado com a orientação de usar
  `date_diff('day', ...)`.

Depois do ajuste, uma nova tentativa de rodar a suíte completa esbarrou num limite da Groq
ainda não documentado no projeto: não o rate limit por minuto (TPM, já conhecido desde o
[ADR-0006](0006-troca-de-provedor-llm-para-groq.md)), mas o **orçamento diário de tokens**
(TPD) do tier gratuito — `"Rate limit reached ... on tokens per day (TPD): Limit 200000, Used
~199000+"`, esgotado pelas execuções completas já feitas neste mesmo dia de trabalho. O
retry+backoff de `ask_with_retries` (`scripts/run_eval.py`) foi desenhado para o caso de rate
limit por minuto (backoff máximo de 60s) e não é suficiente para esperar a liberação de um
orçamento diário — a re-validação end-to-end de `q21`/`q22` com o prompt corrigido ficou
pendente até o orçamento diário da Groq resetar, concluída no dia seguinte (ver decisão 6).

### 6. Revalidação em duas etapas, com custo em tokens auditável por execução

Com o orçamento diário apertado (confirmado na decisão 5) e sem visibilidade de quanto já
tinha sido consumido antes de cada execução, duas capacidades foram adicionadas antes de
revalidar o ajuste de prompt:

- **`X-Total-Tokens`** (header HTTP, não campo de `AgentAnswer` — não faz sentido pedir ao
  modelo para "saber" quantos tokens ele mesmo consumiu): `data_agent/api.py::ask` extrai
  `run_output.metrics.total_tokens` (Agno já agrega isso por execução, incluindo a chamada ao
  `parser_model`) e expõe como header na resposta, em sucesso e nos dois ramos de "saída não
  estruturada" (onde `run_output` existe mesmo a chamada tendo falhado). `scripts/run_eval.py`
  soma esse header por pergunta e reporta o total da execução.
- **Fingerprint do `SYSTEM_PROMPT`** (`_prompt_fingerprint`, sha256[:12] do texto exato do
  prompt): registrado no início de cada execução e no relatório, para que duas execuções sejam
  comparáveis com certeza sobre qual versão do prompt cada uma usou — não uma suposição baseada
  em "rodei depois de editar o arquivo".

A revalidação em si rodou em duas etapas deliberadamente:

1. **Subconjunto mínimo** (`--ids q22,q11,q13,q06,q23`, já suportado por `--ids` sem precisar
   de nenhum campo de tag novo): `q22` (alvo do ajuste) mais quatro vizinhas da mesma classe —
   `q11`/`q13` (mesmo padrão `COUNT(*)` com filtro, mas respondíveis, para garantir que o
   ajuste não tornou o agente excessivamente cauteloso com contagens legítimas) e `q06`/`q23`
   (mesma família de armadilha via `SUM`/`AVG` `NULL`, não `COUNT`, para checar que a regra
   generaliza). Resultado: 5/5 corretas, `q22` virou `insufficient_data`, custo de 34.019
   tokens — parado aí para confirmação antes de gastar mais no orçamento diário.
2. **Suíte completa** (após confirmação): 22/23 corretas (95,7%), **0% de alucinação (0/7)**,
   0% de recusa indevida (0/16), 3 falhas de infraestrutura (`q23`, `q24`, `q26` — o orçamento
   diário esgotou de novo perto do fim da execução, 177.043 tokens consumidos só nesta etapa).
   `q21` e `q22` — as duas falhas que motivaram o ajuste — confirmadas `correct`. Ver a seção
   "Revisão" em `docs/eval_report.md` para a comparação lado a lado com os números pré-ajuste
   (fingerprint antigo `1aaf57a439bc` vs. novo `9d9800652212`).

Um terceiro achado surgiu **só** nesta execução, não presente nas duas anteriores: `q01`
("Quantos pedidos foram efetivamente entregues?") virou `other_mismatch`. Investigado no dia
seguinte, sem gastar nenhum token da Groq — a chamada real já tinha acontecido, só faltava ler
o que já estava registrado:

- O SQL logado (`SELECT COUNT(*) ... WHERE order_status = 'delivered'`), rodado de novo direto
  contra `data/warehouse.duckdb` via `query_sales`, devolveu `96478` — bate exatamente com o
  gabarito. **SQL e dado eliminados como causa.**
- O texto completo de `answer` (não logado por `agent_call_completed`, que só grava `status`)
  foi recuperado da **API pública do Langfuse** (`GET /api/public/traces`, autenticada com as
  credenciais já em `.env` — não é uma chamada à Groq): `"Foram entregues **96 478**
  pedidos..."`, com `status="answered"` e `confidence=0.99`. O modelo usou ` ` (narrow
  no-break space, separador de milhar da convenção SI) em vez de `.`/`,`.
- `scripts/run_eval.py::_NUMBER_RE` só reconhecia `.`/`,` como separador de milhar —
  `"96 478"` virava dois números soltos (`96`, `478`), nenhum batendo com 96478. **Causa
  raiz confirmada por reprodução local**: bug de extração de números no script de avaliação,
  não no agente — a resposta sempre esteve certa. `SYSTEM_PROMPT` também eliminado como causa
  (nada no trace indica hesitação; `status`/`confidence` são de uma resposta confiante e
  correta).

Corrigido em `_NUMBER_RE`/`_normalize_candidates` (aceitam espaço/NBSP/narrow-NBSP como
separador de milhar agora), com teste de regressão reproduzindo o texto real do trace (ver
`docs/eval_report.md`, seção "Achado do `q01`", para o relato completo). De quebra,
`scripts/run_eval.py` passou a logar `answer`/`sql_used` completos por resultado (não só
`status`) e o relatório ganhou uma seção "Diagnóstico de mismatches" com esse detalhe — a
falta desses dois foi exatamente o que tornou esta investigação mais lenta do que precisava
ser.

## Consequências

### Positivas

- Pela primeira vez existe um número reproduzível e versionado para "o agente alucina/recusa
  indevidamente" — não uma alegação: **95,7% de acerto (22/23), 0% de alucinação (0/7), 0% de
  recusa indevida (0/16)**, pós-ajuste de prompt e revalidado de ponta a ponta (ver seção
  "Revisão" em `docs/eval_report.md`, gerado por `uv run python scripts/run_eval.py`) — subiu
  de 92,3%/10%/0% na execução anterior ao ajuste.
- O dataset golden agora serve dois papéis (gabarito de qualidade + rede de segurança de
  regressão para bugs de guard-rail já corrigidos, via `q11`).
- A investigação para separar infra de raciocínio corretamente encontrou e corrigiu um bug real
  em `data_agent/api.py` (rate limit do `parser_model` disfarçado de saída não estruturada) que
  nenhum teste unitário existente havia exercitado — só apareceu rodando o agente de verdade sob
  carga real da Groq, reforçando por que a avaliação de ponta a ponta (não só testes com agente
  falso) é necessária.
- `docs/eval_report.md` também documenta quantos itens precisaram de retry por infraestrutura,
  separado das três métricas — uma taxa ruim não pode mais ser "desculpada" como rate limit sem
  prova.

### Negativas / Trade-offs

- O critério de correção de `"answered"` é sintático, não semântico (ver decisão 4) — um número
  certo por coincidência (ex. duas datas onde uma bate com o valor esperado) passaria como
  correto; não observado nas execuções feitas para este ADR, mas é uma limitação conhecida, não
  tratada.
- `_is_provider_error_payload` reconhece o formato de erro específico da Groq (`{"error": {...}}`
  no nível raiz); se `agent.py` trocar de provedor no futuro, essa checagem pode parar de
  reconhecer o novo formato de erro sem que nada avise — voltaria a cair (silenciosamente) no
  branch genérico de "saída não estruturada".
- O retry+backoff de `ask_with_retries` foi calibrado para rate limit por minuto (TPM), não para
  o orçamento diário (TPD) do tier gratuito da Groq — descoberto na decisão 5 e confirmado de
  novo no dia seguinte (decisão 6): mesmo com o orçamento diário resetado, rodar o subconjunto
  mínimo (34 mil tokens) mais a suíte completa (177 mil tokens) no mesmo dia de trabalho já
  deixou o orçamento (~200 mil/dia) perto do limite de novo, gerando 3 `infra_error` no fim da
  segunda execução. Quando o TPD se esgota, nenhum backoff dentro da ordem de
  segundos/poucos minutos resolve — `scripts/run_eval.py` vai continuar tentando e desistindo
  (`infra_error`) até o orçamento diário liberar o suficiente, o que pode levar bem mais que
  `--backoff-max-seconds`. Rodar a suíte completa mais de uma vez por dia de trabalho não é
  sustentável no tier gratuito — por isso a revalidação foi estruturada em duas etapas (mínima
  primeiro, completa só com confirmação), não porque fosse a forma ideal de rodar a suíte, mas
  porque é a única sustentável no orçamento disponível.
- `data_agent/api.py::ask` continua logando só `status` em `agent_call_completed` (não o texto
  de `answer`) — foi `scripts/run_eval.py` que ganhou esse log (decisão 6), do lado de quem
  consome a API, não do lado da API em si. Uma investigação futura que dependa dos logs da API
  diretamente (não de uma execução de `run_eval.py`) ainda esbarraria na mesma lacuna que
  atrasou o diagnóstico de `q01` — corrigir isso em `data_agent/api.py` ficou fora do escopo
  desta correção.
- Rodar a suíte inteira contra a Groq real é lento por design (throttle + backoff deliberados) e
  consome tokens do tier gratuito a cada execução — não roda em CI a cada PR hoje
  (`.github/workflows/pr-quality-checks.yml` usa credenciais mockadas, que nem tentariam uma
  chamada real). `scripts/run_eval.py` é uma ferramenta para rodar manualmente antes de considerar
  um dia do cronograma concluído, não um gate automatizado.
- A distinção infra/raciocínio depende de `data_agent/api.py` continuar usando exatamente os
  mesmos textos de `detail` que `scripts/run_eval.py` procura (`_PROVIDER_ERROR_DETAIL` em ambos
  os módulos, duplicado propositalmente em vez de importado, já que `scripts/` não depende de
  `src/data_agent` — ver `AGENTS.md`) — uma mudança de texto em um lado sem o outro quebraria essa
  checagem silenciosamente.
