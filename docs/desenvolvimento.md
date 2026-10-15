# Desenvolvimento

Os comandos abaixo funcionam igual no bash e no PowerShell: um por linha, sem `&&` (que o
Windows PowerShell 5.1 não aceita) e sem `VAR=valor comando`.

```bash
# backend fora do Docker (precisa do postgres do compose rodando)
docker compose up -d postgres
uv sync                     # o grupo dev já traz os extras `observability`, `mcp` e `tui`, usados nos testes
uv run uvicorn agent_service.main:app --app-dir src --reload

# frontend (o endereço do backend vai em frontend/.env.local, que vale em qualquer shell:
#   AGENT_SERVICE_URL=http://127.0.0.1:58000
#   AGENT_SERVICE_API_KEY=<a ADMIN_API_KEY, se a autenticação estiver ligada>)
npm --prefix frontend install
npm --prefix frontend run dev

# schema: as migrações (Alembic) rodam sozinhas no startup; para aplicar à mão
uv run kuro-migrate
# mudança de schema nova = um arquivo em src/agent_service/migrations/versions/

# testes
uv run pytest
npm --prefix frontend test   # a conta dos gráficos do console (frontend/src/lib/*.test.ts)
```

Os testes do frontend usam o test runner do próprio Node (22+, que roda TypeScript direto), sem
dependência a mais. Lógica que dá para testar sem tela (a escala e as taxas do gráfico dos Logs,
por exemplo) fica num módulo puro em `frontend/src/lib/`, com um `*.test.ts` ao lado; o componente
só desenha. O CI roda `npm test` junto com o lint e o build.

**CLI, MCP e TUI** são clientes da API e dividem o mesmo `Client`
(`src/agent_service/cli/client.py`). Uma operação nova entra primeiro nele e na
CLI e depois, se fizer sentido, como tool em `mcp_server.py`. O que não for
apresentação (agregar o stream do chat, rodar o eval) fica em funções sem
`typer`, para os três reaproveitarem. Os testes do MCP (`tests/test_mcp_server.py`)
e da TUI (`tests/test_tui.py`) rodam contra uma API falsa, sem serviço no ar.

**Diagramas:** as fontes ficam em `docs/diagramas/*.mmd` e o README e os documentos exibem os
SVGs gerados a partir delas (uma versão clara e uma escura), porque o
renderizador de Mermaid do GitHub falha de forma intermitente. Depois de editar
um `.mmd`, regenere os dois SVGs:

```bash
npx -p @mermaid-js/mermaid-cli mmdc -i docs/diagramas/arquitetura.mmd -o docs/diagramas/arquitetura-claro.svg -t default -b transparent -c docs/diagramas/mermaid.json
npx -p @mermaid-js/mermaid-cli mmdc -i docs/diagramas/arquitetura.mmd -o docs/diagramas/arquitetura-escuro.svg -t dark -b transparent -c docs/diagramas/mermaid.json
uv run python docs/diagramas/fixar-tamanho.py    # grava o tamanho real, para o GitHub não esticar a imagem
```

## Documentação

A documentação mora em `docs/`, um assunto por arquivo; o `README.md` só apresenta
o projeto e aponta para eles, e o `AGENTS.md` fica curto, com as receitas da CLI.
Ao mudar algo que quem integra ou opera percebe, registre em
[notas-de-versao.md](notas-de-versao.md) (ver as regras no fim do arquivo).

| Para saber sobre | Leia |
|---|---|
| agentes, tools, RAG, memória, modelos, anexos | [conceitos.md](conceitos.md) |
| o shell, os comandos, o uso por agentes de IA | [cli.md](cli.md) |
| o servidor MCP para agentes de IA | [mcp.md](mcp.md) |
| o painel no terminal (`kuro dash`) | [tui.md](tui.md) |
| o console web | [console.md](console.md) |
| runs, trace store, Langfuse, shadow | [observabilidade.md](observabilidade.md) |
| componentes e fluxo de uma mensagem | [arquitetura.md](arquitetura.md) |
| configuração, HTTPS, deploy, segurança | [operacao.md](operacao.md) |
| integrar outro sistema ao Kuro | [integracao.md](integracao.md) |

Os links relativos entre documentos são conferidos por `tests/test_docs_links.py`:
um link que aponte para um arquivo ou título que não existe quebra o CI.
