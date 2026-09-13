# Arquitetura

## Escopo

Este documento define o que o agente de IA data-aware deve e não deve responder. Ele é a
referência para decidir, em qualquer dia do projeto, se uma pergunta ou uma feature está
dentro do que foi combinado.

### Perguntas que o agente deve responder

O agente consulta a base Olist (via tools de SQL controlado sobre o DuckDB) para responder
perguntas em linguagem natural sobre:

- **Vendas / faturamento**: total de vendas (soma de `price` em `order_items`) por categoria
  de produto, por estado (do cliente ou do vendedor), por vendedor, por período (mês/ano),
  ou combinações dessas dimensões.
- **Logística / entrega**: prazo médio de entrega (diferença entre
  `order_delivered_customer_date` e `order_purchase_timestamp`), comparação entre prazo
  estimado (`order_estimated_delivery_date`) e prazo real, taxa de atraso, por estado ou por
  período.
- **Satisfação do cliente**: nota média de avaliação (`review_score`), distribuição de notas,
  por categoria de produto, por vendedor, por período.
- **Pagamentos**: forma de pagamento mais usada (`payment_type`), número médio de parcelas,
  valor médio de pagamento, por período ou por forma de pagamento.
- **Catálogo / produtos**: quantidade de produtos por categoria, características físicas
  agregadas (peso, dimensões) quando relevante para a pergunta.
- Perguntas que combinam as dimensões acima (ex.: "qual a nota média de avaliação dos pedidos
  entregues com atraso no estado de SP?").

Todas as respostas devem vir de consultas reais aos dados carregados em
`data/warehouse.duckdb` — o agente nunca deve inventar números. Quando a base não tem dado
suficiente para responder com confiança, o agente deve dizer isso explicitamente em vez de
estimar.

### Fora de escopo (explicitamente)

- **Previsão / forecasting**: o agente não projeta vendas futuras, demanda futura ou
  tendências fora do período coberto pelos dados (set/2016 a out/2018). Ver
  [data_dictionary.md](data_dictionary.md) para as limitações do período.
- **Dados fora da base Olist**: preços de concorrentes, dados de mercado externos, notícias,
  cotação de câmbio, ou qualquer informação que não esteja nas 8 tabelas carregadas.
- **Geolocalização de precisão**: o agente não localiza um cliente ou vendedor em um endereço
  exato — a tabela `geolocation` é uma amostra de coordenadas por prefixo de CEP, não um
  geocodificador de endereço completo.
- **Conteúdo textual livre de reviews**: o agente não resume, opina sobre ou cita o texto de
  `review_comment_message` / `review_comment_title` como fonte de verdade qualitativa — a
  análise é sobre os campos estruturados (nota, datas, valores).
- **Ações de escrita**: o agente é somente leitura. Ele não cria, atualiza ou cancela pedidos,
  não altera dados no warehouse.
- **Identificação de pessoas físicas**: `customer_unique_id` e `customer_id` são
  identificadores anonimizados/hasheados; o agente não tenta reidentificar clientes.

## Escopo de implementação (dias seguintes)

Este documento cobre apenas o *escopo funcional das perguntas*. Decisões de arquitetura de
código (estrutura do agente, tools, API, observabilidade) serão detalhadas e implementadas
incrementalmente nos próximos dias do cronograma e registradas em `docs/adrs/`.
