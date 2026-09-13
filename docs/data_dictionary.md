# Dicionário de Dados — Olist Brazilian E-Commerce Dataset

Este documento descreve as 8 tabelas carregadas em `data/warehouse.duckdb` por
`scripts/load_data.py`, seus relacionamentos, granularidade, período coberto e limitações
conhecidas. Ele é a base da estratégia anti-alucinação do agente: qualquer resposta numérica
deve ser rastreável a uma dessas tabelas, e qualquer pergunta que dependa de algo listado como
limitação deve ser recusada ou qualificada explicitamente pelo agente.

## Período coberto

Os dados cobrem pedidos com `order_purchase_timestamp` entre **04/set/2016** e
**17/out/2018**. **Não há dados após outubro de 2018** — qualquer pergunta sobre vendas,
entregas ou avaliações fora dessa janela não pode ser respondida a partir desta base.

O volume de pedidos antes de 2017 é muito pequeno (os primeiros meses são efetivamente um
piloto da plataforma); agregações mensais de 2016 devem ser tratadas com essa ressalva.

## Visão geral das tabelas

| Tabela            | Granularidade                                   | Linhas (dataset completo) |
|-------------------|--------------------------------------------------|---------------------------|
| `customers`       | 1 linha por `customer_id` (1 pedido = 1 customer_id) | 99.441                |
| `sellers`         | 1 linha por vendedor                             | 3.095                     |
| `products`        | 1 linha por produto                              | 32.951                    |
| `geolocation`     | 1 linha por observação de coordenada por CEP (não é chave única) | 1.000.163 |
| `orders`          | 1 linha por pedido                               | 99.441                    |
| `order_items`     | 1 linha por item de pedido (um pedido pode ter vários) | 112.650              |
| `order_payments`  | 1 linha por parcela/transação de pagamento de um pedido | 103.886             |
| `order_reviews`   | 1 linha por avaliação de pedido                  | 99.224                    |

## Tabelas e relacionamentos

### `customers`
Um `customer_id` é gerado **por pedido**, não por pessoa. Para identificar o mesmo cliente
em múltiplos pedidos, usa-se `customer_unique_id`. Isso significa que "número de clientes
distintos" e "número de `customer_id` distintos" **não são a mesma coisa** — a métrica correta
de clientes únicos deve usar `customer_unique_id`.

- PK: `customer_id`
- Colunas: `customer_id`, `customer_unique_id`, `customer_zip_code_prefix`, `customer_city`,
  `customer_state`

### `sellers`
- PK: `seller_id`
- Colunas: `seller_id`, `seller_zip_code_prefix`, `seller_city`, `seller_state`

### `products`
- PK: `product_id`
- `product_category_name` está em português e tem **610 produtos sem categoria preenchida
  (NULL)** no dataset completo — agregações "por categoria" devem decidir explicitamente como
  tratar esse grupo (ex.: rotular como "sem categoria" em vez de omitir).
- As colunas de dimensão física (`product_weight_g`, `product_length_cm`,
  `product_height_cm`, `product_width_cm`) também podem ser NULL para poucos produtos.

### `geolocation`
- **Sem chave primária real.** A granularidade é "uma observação de latitude/longitude para
  um prefixo de CEP", e o mesmo `geolocation_zip_code_prefix` aparece dezenas ou centenas de
  vezes com coordenadas ligeiramente diferentes (mais de 130 mil combinações duplicadas de
  zip+lat+lng no dataset completo). **Geolocalização é aproximada por prefixo de CEP (5
  dígitos), não endereço exato** — não deve ser usada para localizar um cliente ou vendedor
  específico, apenas para agregações regionais aproximadas (ex.: mapa de calor por estado).
  A junção com `customers`/`sellers` é feita por `zip_code_prefix`, e não é 1:1.
- Colunas: `geolocation_zip_code_prefix`, `geolocation_lat`, `geolocation_lng`,
  `geolocation_city`, `geolocation_state`

### `orders`
- PK: `order_id`
- FK: `customer_id` → `customers.customer_id`
- Colunas de data (todas podem ser NULL exceto `order_purchase_timestamp`):
  `order_purchase_timestamp`, `order_approved_at`, `order_delivered_carrier_date`,
  `order_delivered_customer_date`, `order_estimated_delivery_date`.
- **2.965 pedidos (do dataset completo) não têm `order_delivered_customer_date` preenchida**
  — normalmente pedidos cancelados, indisponíveis ou ainda em trânsito no momento do export.
  Cálculos de "prazo médio de entrega" devem filtrar por pedidos efetivamente entregues
  (`order_status = 'delivered'` e `order_delivered_customer_date IS NOT NULL`), documentando
  essa exclusão.
- `order_status` possui os valores: `delivered`, `shipped`, `canceled`, `unavailable`,
  `invoiced`, `processing`, `created`, `approved`. A grande maioria (~97%) é `delivered`.

### `order_items`
- PK composta: (`order_id`, `order_item_id`)
- FK: `order_id` → `orders.order_id`; `product_id` → `products.product_id`; `seller_id` →
  `sellers.seller_id`
- Um pedido (`order_id`) pode ter múltiplos itens, inclusive de vendedores diferentes.
  "Total de vendas" deve ser calculado como soma de `price` (e, se aplicável, `freight_value`
  separadamente) em `order_items`, não a partir de uma coluna de valor total em `orders`
  (que não existe).

### `order_payments`
- PK composta: (`order_id`, `payment_sequential`)
- FK: `order_id` → `orders.order_id`
- Um pedido pode ter mais de uma transação de pagamento (`payment_sequential` > 1, ex.:
  parte no cartão e parte em voucher). A soma de `payment_value` por pedido é o valor
  efetivamente pago, que pode diferir levemente da soma de `price + freight_value` em
  `order_items` por causa de vouchers e arredondamentos.
- `payment_type` possui os valores: `credit_card`, `boleto`, `voucher`, `debit_card`,
  `not_defined` (poucas ocorrências, tratar como "não informado").

### `order_reviews`
- **`review_id` sozinho NÃO é único** no dataset completo (789 valores de `review_id`
  repetidos entre pedidos diferentes) — a chave primária real é a combinação
  (`review_id`, `order_id`).
- FK: `order_id` → `orders.order_id`
- `review_score` é um inteiro de 1 a 5.
- `review_comment_title` e `review_comment_message` são texto livre em português, muitas
  vezes vazio — não são tratados como fonte de dados estruturados pelo agente (ver
  [architecture.md](architecture.md), seção Escopo).

## Diagrama de relacionamento (simplificado)

```
customers 1---N orders 1---N order_items N---1 products
                   |                        \--N---1 sellers
                   |---N order_payments
                   |---N order_reviews

geolocation (join solto por zip_code_prefix com customers/sellers, não é FK declarada)
```

## Limitações conhecidas (resumo para o agente)

1. Não há dados após outubro de 2018 — nenhuma pergunta sobre período posterior pode ser
   respondida.
2. `customer_id` é por pedido; use `customer_unique_id` para contar clientes únicos.
3. `geolocation` é aproximada por prefixo de CEP e tem múltiplas linhas por prefixo — não é
   um geocodificador de endereço exato.
4. ~3% dos pedidos não têm data de entrega ao cliente preenchida; métricas de prazo de
   entrega devem ser calculadas apenas sobre pedidos entregues.
5. ~0,6% dos produtos não têm categoria preenchida.
6. `review_id` não é chave única sozinha; a chave real é (`review_id`, `order_id`).
7. O texto livre de reviews não é usado como fonte de verdade quantitativa pelo agente.
