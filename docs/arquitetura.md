# Arquitetura

## Como uma mensagem é processada

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagramas/fluxo-do-chat-escuro.svg">
  <img alt="Sequência de uma requisição ao /chat, da validação das dependencies até o feedback" src="diagramas/fluxo-do-chat-claro.svg">
</picture>

## Componentes

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagramas/arquitetura-escuro.svg">
  <img alt="Componentes do frontend e do backend e onde cada um guarda dados" src="diagramas/arquitetura-claro.svg">
</picture>

```
src/agent_service/
  api/            contrato estável (/chat, /chat/stream, /analyze) e CRUD de agentes, tools, collections, credenciais, observabilidade
  agents/         store (definições + versões + nota de feedback), registry (resolução em runtime), anexos, dependências
  tools/          store, catálogo de builtins, tools de API e Python, resolução
  models/         catálogo de provedores, credenciais cifradas, criação do modelo
  memory/         memória de longo prazo (Agno)
  documents/      collections de documentos (pgvector)
  observability/  trace store local (Postgres), exportador Langfuse opcional e leitura de traces
  cli/            CLI kuro; client.py é o cliente HTTP que a CLI, o MCP e a TUI compartilham
  mcp_server.py   servidor MCP (kuro-mcp): as operações da CLI como tools, por stdio
  tui/            kuro dash (Textual): execuções ao vivo, trace e panorama
  main.py         FastAPI + AgentOS
frontend/         console Next.js (App Router, Tailwind, componentes próprios em components/ui/)
tests/            pytest
```

**Stack:** Python 3.12, [Agno](https://docs.agno.com) (agentes, memória, tools,
knowledge) e [AgentOS](https://docs.agno.com/agent-os) (sessões e ingestão),
FastAPI, PostgreSQL + pgvector, Langfuse (opcional), Next.js + TypeScript +
Tailwind, Docker Compose. Do lado de quem opera: Typer + Rich (CLI), o SDK `mcp`
(servidor MCP) e Textual (TUI). Os três são clientes da API e não entram na imagem
do serviço.
