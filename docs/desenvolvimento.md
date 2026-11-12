# Desenvolvimento

Os comandos abaixo funcionam igual no bash e no PowerShell: um por linha, sem `&&` (que o
Windows PowerShell 5.1 não aceita) e sem `VAR=valor comando`.

```bash
# backend fora do Docker (precisa do postgres do compose rodando)
docker compose up -d postgres
uv sync                     # o grupo dev já traz os extras `observability`, `mcp` e `tui`, usados nos testes
# sem chave de API o serviço não sobe: ADMIN_API_KEY/RUNTIME_API_KEY no .env, ou ALLOW_OPEN=1
# no .env para rodar aberto na sua máquina (fora do compose o padrão é fechado; ver operacao.md)
uv run uvicorn agent_service.main:app --app-dir src --reload

# frontend (o endereço do backend vai em frontend/.env.local, que vale em qualquer shell:
#   AGENT_SERVICE_URL=http://127.0.0.1:58000
#   AGENT_SERVICE_API_KEY=<a ADMIN_API_KEY, se a autenticação estiver ligada>)
npm --prefix frontend install
npm --prefix frontend run dev

# schema: as migrações (Alembic) rodam sozinhas na subida (o lifespan de main.py, não o
# import; ver "A subida" em operacao.md); a suíte migra e semeia no conftest. Para aplicar à mão
uv run kuro-migrate
# mudança de schema nova = um arquivo em src/agent_service/migrations/versions/

# testes (o conftest liga ALLOW_OPEN=1: a suíte roda sem chave)
uv run pytest
npm --prefix frontend test   # a conta dos gráficos do console (frontend/src/lib/*.test.ts)
```

### Testes contra Postgres

`uv run pytest` roda em SQLite (um arquivo descartável por sessão), sem banco nenhum no ar.
O que o SQLite não tem fica nos testes marcados com `@pytest.mark.postgres`
(`tests/test_postgres.py`): as migrações de ida e de volta num banco limpo, o `SKIP LOCKED` e o
`LISTEN/NOTIFY` da fila de jobs, a busca híbrida (pgvector + `tsvector`, com embedder falso) e o
cursor e as colunas JSON do trace store. Sem `TEST_DATABASE_URL` eles são pulados; com ela, a
sessão do pytest cria um banco próprio (`kuro_test_<hex>`) no servidor indicado e o apaga no fim
(`KURO_TEST_KEEP_DB=1` o mantém para inspecionar). Por isso o usuário precisa poder criar banco, e
o servidor precisa da extensão `vector` — a imagem `pgvector/pgvector:pg16` do compose serve.
Servidor fora do ar com a variável definida é erro, não pulo.

```bash
# um Postgres descartável, longe do volume do compose (a porta é só um exemplo)
docker run -d --rm --name kuro-pg-teste -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:55999:5432 pgvector/pgvector:pg16

# bash
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55999/postgres uv run pytest -m postgres
# PowerShell
$env:TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:55999/postgres"
uv run pytest -m postgres

docker stop kuro-pg-teste
```

Com a variável definida, `uv run pytest` sem `-m` roda a suíte inteira contra o Postgres. Isso
ainda não está no CI: a suíte foi escrita para o SQLite em arquivo, e há testes que dependem de
tempo (o da fila de `/analyze` em `test_integracao_regente.py` espera 0,2 s a chamada entrar na
fila, e com o banco na rede ela ainda está no I/O), além de rodar bem mais devagar. O CI roda a
suíte em SQLite (`backend`) e `pytest -m postgres` com um serviço `pgvector/pgvector:pg16`
(`postgres`).

Os testes do frontend usam o test runner do próprio Node (22+, que roda TypeScript direto), sem
dependência a mais. Lógica que dá para testar sem tela (a escala e as taxas do gráfico dos Logs,
por exemplo) fica num módulo puro em `frontend/src/lib/`, com um `*.test.ts` ao lado; o componente
só desenha. O CI roda `npm test` junto com o lint e o build.

### Smoke do console (Playwright)

```bash
npm --prefix frontend run e2e          # build do console + backend de teste + Chromium headless
npx --prefix frontend playwright install chromium   # uma vez, para baixar o navegador
```

Um teste só (`frontend/e2e/smoke.spec.ts`), o caminho principal da tela: criar um agente →
conversar no Playground → abrir a sessão nos Logs → abrir o trace da execução. Fica fora do
`npm test` (leva minutos: faz o `next build`) e roda num job próprio do CI (`e2e`), que publica
o relatório e o trace do Playwright como artefato quando falha (abra com
`npx playwright show-trace <trace.zip>`).

O `playwright.config.ts` sobe dois servidores em portas próprias:

- **backend** em `127.0.0.1:58100`: `scripts/servico_de_teste.py`, o app de verdade
  (`agent_service.main`: rotas, autenticação, trace store, sessões, versões) com três trocas feitas
  no script, nenhuma no código do serviço: SQLite num diretório temporário novo (e o `SqliteDb`
  do Agno no lugar do `PostgresDb`, que usa JSONB), um banco vetorial vazio no lugar do pgvector e
  um modelo determinístico no lugar do provedor — toda resposta é `Resposta de teste: <mensagem>`.
  Também não lê o `.env`, não fala com a rede (lista de modelos fixa, telemetria do Agno
  desligada) e liga a autenticação com a chave `e2e-admin-key`, que o console manda pelo BFF;
- **console** em `127.0.0.1:3100`: o build de produção servido como na imagem
  (`frontend/e2e/iniciar-console.mjs`, o `server.js` do standalone). `E2E_SKIP_BUILD=1` reaproveita
  um `.next` já construído.

Fora do CI, um servidor que já esteja de pé nessas portas é reaproveitado (útil para repetir o
teste rápido), por isso os nomes do teste levam um sufixo único. O que o smoke **não** cobre:
o Postgres (sessões do Agno, JSONB, pgvector), RAG e tools — isso fica com o pytest.
Para subir o backend de teste à mão: `uv run python scripts/servico_de_teste.py --port 58100`.

### Tipos do console a partir do OpenAPI

`frontend/src/lib/api-types.gen.ts` é gerado do OpenAPI do serviço e vai no commit:

```bash
npm --prefix frontend run gen:types
```

O `scripts/exportar_openapi.py` monta o app como o smoke (sem Postgres, sem subir servidor) e
exporta só as rotas do serviço: as do AgentOS ficam de fora, inclusive onde o caminho coincide
(`GET /agents`, `GET /health`), porque quem responde é a do serviço. Depois o `openapi-typescript`
gera os tipos. O job `api-types` do CI regenera e falha se o arquivo mudar: alterou um modelo de
resposta no backend, rode o `gen:types` e faça commit do resultado junto.

O `frontend/src/lib/types.ts` continua sendo o ponto de import do console; a migração para os tipos
gerados é aos poucos, um tipo por vez, re-exportado com o mesmo nome:

```ts
export type PromptVersion = Schemas["PromptVersionOut"];
```

Já migrados: `FeedbackRule`, `FeedbackNote`, `FeedbackVersion` e `PromptVersion`. Para migrar
outro, confira antes se o gerado não fica mais frouxo que o escrito à mão — `string` onde havia uma
união de literais, `{ [key: string]: unknown }` onde havia um objeto tipado — e, se ficar, aperte o
modelo Pydantic do backend (`Literal`, um `BaseModel` no lugar de `dict`) antes. Rotas sem
`response_model` geram tipos vazios ou genéricos: hoje `GET /agent-types`,
`GET /agents/{t}/procedures` e `.../procedures/{session_id}`,
`GET /observability/sessions/{id}/transcript`, `POST /collections/{c}/documents` e `.../files`,
`GET /health` e `GET /ready` (o `/chat/stream` é SSE, não tem schema). São candidatas a ganhar um
modelo. `npm run build` é quem diz se a troca quebrou alguma tela.

**CLI, MCP e TUI** são clientes da API e dividem o mesmo `Client`
(`src/agent_service/cli/client.py`). Uma operação nova entra primeiro nele e na
CLI e depois, se fizer sentido, como tool em `mcp_server.py`. O que não for
apresentação (agregar o stream do chat, rodar o eval) fica em funções sem
`typer`, para os três reaproveitarem. Os testes do MCP (`tests/test_mcp_server.py`)
e da TUI (`tests/test_tui.py`) rodam contra uma API falsa, sem serviço no ar.

**O contrato estável não importa o Agno.** `src/agent_service/api/routes.py` recebe o agente como
valor opaco e lê os eventos do run por `agents/runtime.py` (`read_event` devolve o tipo do evento,
o conteúdo e o uso), o ponto entre essas rotas e os tipos do Agno; os anexos passam por
`agents/attachments.py`. Os erros de embedder que o projeto levanta são subclasses de
`EmbedderError` (`documents/embedder.py`): capture a base, e converta para ela, com
`raise ... from`, a falha de um provedor que você capturar.

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
