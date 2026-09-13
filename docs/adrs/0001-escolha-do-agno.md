# ADR-0001: Uso do Agno como framework de agente

| Campo      | Valor              |
|------------|--------------------|
| **Status** | Aceito             |
| **Data**   | 2026-09-09         |

## Contexto

O projeto precisa de um agente de IA que responda perguntas em linguagem natural consultando
dados reais (Olist) via tool-calling, retornando saída estruturada e validável (Pydantic) para
ser consumida por uma API FastAPI. As alternativas consideradas foram LangChain, LlamaIndex,
CrewAI e Agno.

- **LangChain**: ecossistema muito amplo, mas historicamente com abstrações pesadas (chains,
  callbacks, múltiplas camadas) para um caso de uso que é essencialmente "um agente, um
  conjunto pequeno de tools de SQL controlado, uma resposta estruturada". O overhead de
  aprendizado e de código não se paga para o escopo deste projeto.
- **LlamaIndex**: focado primariamente em RAG sobre documentos/índices vetoriais. Não é o
  ajuste natural para um agente cujo dado de verdade é uma base relacional (DuckDB), não um
  corpus de texto.
- **CrewAI**: voltado a orquestração de múltiplos agentes/papéis colaborando. Este projeto
  tem um único agente com um conjunto de tools bem definido — a complexidade de multi-agente
  não se justifica.
- **Agno**: framework leve, com suporte nativo a tool-calling e a `response_model` baseado em
  Pydantic para forçar saída estruturada, baixo overhead de execução e de código, e boa
  integração com instrumentação via OpenTelemetry (relevante para observabilidade com
  Langfuse).

## Decisão

Usar **Agno** como framework do agente.

Critérios decisivos:
1. **Tool-calling nativo e direto**: o agente chama tools Python (funções que executam SQL
   controlado contra o DuckDB) sem precisar de camadas intermediárias de "chain" ou "graph".
2. **`response_model` com Pydantic**: a saída do agente pode ser validada como um schema
   estruturado (ex.: valor numérico + unidade + fonte da consulta), o que é central para a
   estratégia anti-alucinação — o agente não "escreve" um número livremente, ele retorna um
   objeto validado que veio de uma tool.
3. **Baixo overhead**: menos abstrações entre a pergunta do usuário e a tool que busca o dado
   real, o que facilita observabilidade (rastrear exatamente qual SQL foi executado) e
   depuração.
4. **Escopo do projeto**: um único agente, um conjunto pequeno e bem definido de tools —
   exatamente o caso de uso para o qual Agno foi desenhado, sem pagar o custo de
   funcionalidades de multi-agente ou RAG que não serão usadas.

## Consequências

### Positivas

- Código do agente mais simples e direto de auditar — importante num projeto cuja premissa é
  "nunca inventar números".
- Saída estruturada via Pydantic integra naturalmente com FastAPI (mesmos schemas podem ser
  reusados como response models da API).
- Menor superfície de dependências transitivas em comparação com LangChain.

### Negativas / Trade-offs

- Ecossistema e comunidade menores que LangChain — menos exemplos prontos e integrações de
  terceiros disponíveis "de fábrica".
- Caso o projeto evolua para múltiplos agentes colaborando ou para RAG sobre documentos, esta
  decisão precisará ser revisitada (Agno não foi escolhido para esses casos).
