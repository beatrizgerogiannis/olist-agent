# ADR-0002: DuckDB local com SQL controlado como camada de dados

| Campo      | Valor              |
|------------|--------------------|
| **Status** | Aceito             |
| **Data**   | 2026-09-09         |

## Contexto

O agente precisa responder perguntas numéricas exatas (somas, médias, contagens) sobre a base
Olist sem inventar valores. Havia três abordagens possíveis para dar ao agente acesso a esses
dados:

1. **Banco de dados vetorial** (embeddings de linhas/registros + busca por similaridade).
2. **Jogar o CSV inteiro (ou um resumo dele) no contexto do LLM** e deixar o modelo calcular
   ou extrair valores diretamente do texto.
3. **Banco relacional local (DuckDB) + tools de SQL controlado**, onde o agente chama uma
   função Python que executa uma consulta parametrizada/validada e retorna o resultado exato.

## Decisão

Usar **DuckDB local** (`data/warehouse.duckdb`) como camada de dados, acessado exclusivamente
através de tools de SQL controlado (não SQL livre gerado e executado sem validação).

Motivos para descartar as outras opções:

- **Banco vetorial**: busca por similaridade é a ferramenta certa para "encontrar texto
  parecido com...", não para "somar `price` onde categoria = X". Perguntas deste projeto são
  agregações numéricas exatas sobre dados tabulares — um vetor DB não garante exatidão
  aritmética e adiciona uma camada de indexação/embeddings sem necessidade.
- **CSV inteiro no contexto do LLM**: além de estourar o contexto rapidamente com ~1,5 milhão
  de linhas (a tabela `geolocation` sozinha tem ~1 milhão), pedir a um LLM para somar ou
  calcular médias lendo texto é a forma mais direta de produzir números errados/alucinados —
  exatamente o que este projeto existe para evitar. Não há como auditar ou reproduzir o
  cálculo depois.
- **DuckDB + SQL controlado**: o cálculo é feito pelo motor de banco de dados (determinístico,
  auditável, reexecutável), e o papel do LLM é decidir *qual* tool/consulta chamar e *como*
  formatar a resposta — nunca fazer a conta ele mesmo. DuckDB roda embutido (sem serviço
  externo para operar), lê os CSVs de forma performática e suporta chaves primárias/
  estrangeiras, o que permite validar a integridade referencial dos dados carregados
  (ver `scripts/load_data.py` e `docs/data_dictionary.md`).

## Consequências

### Positivas

- Toda resposta numérica do agente é rastreável a uma consulta SQL específica, o que viabiliza
  observabilidade (logar a query executada) e depuração de respostas incorretas.
- DuckDB não exige infraestrutura de banco separada — roda como arquivo local, simplificando
  Docker e desenvolvimento local.
- Constraints de PK/FK no schema pego cedo erros de integridade dos dados brutos (ex.: um
  `order_item` referenciando um `order_id` inexistente falha a carga em vez de silenciosamente
  gerar métricas erradas depois).
- Performance adequada para o volume do dataset (~1,5 milhão de linhas no total) sem
  necessidade de um data warehouse gerenciado.

### Negativas / Trade-offs

- O conjunto de perguntas que o agente responde bem fica limitado ao que é expressável em SQL
  sobre o schema definido — perguntas abertas sobre o texto livre das reviews (ver
  [architecture.md](../architecture.md), seção Escopo) não são endereçadas por esta camada.
- Exige definir e manter um conjunto de tools de SQL controlado (não SQL arbitrário gerado
  livremente pelo LLM), o que é trabalho adicional de design em comparação com simplesmente
  deixar o modelo gerar qualquer query. Ver
  [0003-sql-controlado-vs-tools-granulares.md](0003-sql-controlado-vs-tools-granulares.md)
  para o trade-off entre uma tool de SQL genérico com guard-rails (escolhida) e tools
  granulares por métrica.
- DuckDB local não escala horizontalmente nem é multiusuário de escrita — adequado para este
  projeto de portfólio (leitura, single-node), mas seria uma limitação real num cenário de
  produção com múltiplos serviços escrevendo concorrentemente.
