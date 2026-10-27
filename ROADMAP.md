# Roadmap do Kuro (agent-service)

> Feito a partir da leitura do código em 2026-09-18 (commit `298a869`) e revisado a cada batelada
> (última revisão: 2026-10-27, depois da batelada de RAG, da fila de jobs e das correções vindas do R8)
> — os itens entregues ficam riscados, com o que foi feito e o que sobrou, em vez de sumirem.
> É o único lugar do roadmap: o README só aponta para cá. Diz o que fazer, em que ordem e o que cortar.
>
> **Repriorizado em 2026-09-24** para a migração dos agentes R4/R6/R8 do RegenteUDSP e para o
> modo enxuto sem Langfuse. A ordem que vale agora é a da §0; as seções seguintes continuam como
> detalhamento técnico de cada item.

## 0. Prioridade atual: Kuro enxuto + migração do Regente

**Fronteira com o Regente.** O Kuro decide e o Regente valida e executa. Ficam no Regente:
claim/debounce por `conversation_id`, catálogo, preço, desconto, alçada, estado da ficha de
matrícula (R8) e do reset de senha (R4), escrita no Flie/Vindi e o fallback quando o Kuro está fora
do ar. O Kuro devolve uma decisão estruturada e rastreável. Todo número que ela contém é revalidado
pelo Regente antes de qualquer efeito. Os três agentes são uma chamada por ciclo com JSON estrito,
então entram como `kind="analysis"` via `/analyze`. Não dependem do procedural nem de memória.

**Sprint 1, "Kuro Lite": produção sem Langfuse — feito**
1. ~~Trace store local~~ **Feito** (`observability/run_store.py`): `DbTraceStore` com as tabelas
   `runs`, `run_spans` (uma span por modelo e por tool call) e `run_scores`, implementando o
   `TraceStore` com os mesmos modelos de resposta. `traced_run_events` grava todo run, com status
   sempre terminal (inclusive cliente desconectado), numa thread fora do caminho da resposta. Uma
   falha na gravação vira log e nunca derruba o run. Já nasce com `tenant_id` (default
   `'default'`) e `config_hash` (vazio até o Sprint 2). Sessões e stats viraram `GROUP BY`. O
   Langfuse passou a ser um exportador opcional, e `TRACE_STORE_BACKEND=langfuse` mantém a
   leitura antiga.
2. ~~Langfuse fora da instalação~~ **Feito**: `langfuse` e `openinference-instrumentation-agno`
   foram para o extra `[observability]` (import lazy; a imagem só o inclui com
   `KURO_EXTRAS="--extra observability"`). O `trace_id` é derivado sem o SDK, com o mesmo valor.
   Ficou para depois: o script de init do banco do Langfuse ainda roda no Postgres do núcleo
   (cria um banco vazio, custo desprezível).
3. ~~Redis fora do core~~ **Feito**: o `messaging/` morto foi removido (§8), junto com a
   dependência `redis` e o `REDIS_URL`. O núcleo sobe com Postgres + agent-service.
4. ~~I/O fora do event loop~~ **Feito em parte**: `/chat`, `/chat/stream` e `/analyze` resolvem o
   agente em `run_in_threadpool`. O snapshot com cache e `LISTEN/NOTIFY` continua em aberto.
5. ~~`kuro runs` sem Langfuse~~ **Feito**: `list/show/stats/tail/sessions/score` e
   `/observability/*` leem do trace store local, e o `health` não avisa mais "sem dados".

**Sprint 2, governança mínima — feito**
1. ~~Alembic~~ **Feito** (`agent_service/migrations`): a revisão de base congela o schema e adota
   um banco criado pelo antigo `create_all` (cria o que falta, acrescenta as colunas que faltam e
   roda uma vez as conversões de dados que viviam no startup). Os `init_store()` e os
   `_add_missing_columns` saíram. As migrações rodam no startup, e `kuro-migrate` roda à mão.
   Testado sobre o banco real do Docker. Com vários workers subindo juntos falta um lock
   (advisory lock do Postgres), porque hoje há um processo só.
2. ~~Versão da config inteira~~ **Feito** (`agents/versions.py`): a revisão cobre instructions,
   modelo resolvido, tools pelo hash da config (sem expor segredo), schema, collection,
   dependências e regras de feedback. É criada quando o registry remonta o agente, então nenhum
   caminho de escrita precisa lembrar dela, e a mesma config reaproveita o mesmo número. Cada run
   grava `agent_version`/`config_hash`, que o `/chat` e o `/analyze` devolvem (o `run_id` é o id
   da decisão). CLI: `kuro agents revisions`.
3. ~~`kuro eval` v0~~ **Feito** (`cli/eval.py`): dataset JSONL, comparação determinística campo a
   campo (com caminhos aninhados e tolerância numérica), regras de política com `when` e
   `{"$dep": ...}`, `--compare` com o agente de produção e a nota `eval` gravada em cada run.
   Só para `kind="analysis"` (o caso do Regente). O LLM-juiz e o replay a partir de `runs` ficam
   para depois.
4. ~~draft/prod~~ **Feito de outro jeito**: em vez de `agente@draft` no runtime, o draft é um
   agente comum (`r8-draft`) e `kuro agents promote r8-draft --to r8` copia a configuração. O
   runtime não muda nada e o Regente chama sempre `r8`. Fixar uma versão por chamada
   (`r8@12`) fica para quando houver necessidade.
5. **Extra:** custo estimado por modelo (`models/pricing.py`, sobrescrito por `MODEL_PRICES`).
   Sem o Langfuse o `cost_usd` vinha vazio para o Gemini.

**Sprint 3, infraestrutura para o Regente integrar sozinho — feito**

Decisão de 2026-09-25: os agentes são criados e mantidos **no repositório do Regente**. O Kuro
entrega a infraestrutura, e o guia para o time de lá é o [docs/integracao.md](docs/integracao.md).
1. ~~Parâmetros do modelo por agente~~ **Feito** (`models/params.py`, migração 0003):
   `model_params` = temperature, top_p, max_tokens, thinking_budget (só Gemini), traduzidos por
   provedor e contados na versão da configuração.
2. ~~`enum` nos campos~~ **Feito**: em `response_schema`, `items` e `dependency_fields`. Vira
   `Literal` no schema do provedor, e um valor fora dele dá 502, nunca uma `acao` inventada.
3. ~~Tempo limite e limite de simultâneas~~ **Feito**: `timeout_seconds` por agente
   (`RUN_TIMEOUT_SECONDS` = 90 por padrão) dá 504 e grava o run como erro. `MAX_CONCURRENT_RUNS`
   por processo; acima dele a chamada espera numa fila em memória (`QUEUE_MAX_SIZE`,
   `QUEUE_MAX_WAIT_SECONDS`) e, com a fila cheia ou a espera esgotada, dá 503 com `Retry-After`.
   Decisão de 2026-10-02: fila só de espera, **sem mudar o contrato HTTP**. Ela não é durável;
   a fila durável continua sendo o claim/daemon do Regente. Se um dia houver execução
   assíncrona, ela vai para o Postgres (`SKIP LOCKED`), não de volta para o Redis.
4. ~~Correlação~~ **Feito** (migração 0004): `metadata` no `/chat`/`/analyze` (não vai para o
   modelo), filtro `--meta`/`?meta=`, e `session_id` opcional no `/analyze` para agrupar por
   conversa.
5. ~~Shadow~~ **Feito**: `POST /observability/references` (escopo runtime) grava a decisão do
   legado; `/observability/agreement` e `kuro runs agreement` dão a concordância por campo e por
   versão; `kuro runs export` gera o dataset do `kuro eval`. A comparação é a mesma do eval
   (`agent_service/evaluation.py`).
6. ~~Readiness~~ **Feito**: `GET /ready` (aberta) confere o banco.
7. ~~Contrato de erro~~ **Feito**: a tabela de status → ação fica em `docs/integracao.md` §4.
8. ~~Agentes como código~~ **Feito**: `?dry_run=true` no `POST`/`PUT /agents`,
   `kuro agents apply -f <dir> [--dry-run]` (só envia o que mudou) e `kuro agents export -o <dir>`.

**Depois, do lado do Regente** (fora deste repositório): `KuroClient`, `LLM_PROVIDER=legacy|kuro|shadow`,
R8 em shadow → prod, depois R4, e por último R6 (shadow → assistido com
`R6_AUTO_EFFECT_ENABLED=false` → autônomo), com a revalidação financeira 100% no Regente.

**Depois do Sprint 3 (2026-09-26), pedidos do uso do console:** sessão dos Logs como chat (com o trace
de cada mensagem mantido), feedback por resposta e da conversa inteira com a transcrição vinda das
execuções, anexos visíveis na conversa (só nome/tipo/tamanho gravados, nunca o conteúdo) e **modelos
lidos ao vivo do provedor** (`/model-providers/{p}/models`), em vez da lista fixa — o Gemini 2.5 está
saindo, e a lista já traz os 3.x. ~~A tabela de preço não conhecia os modelos novos~~ **Feito
(2026-10-01)**: `models/pricing.py` cobre os Gemini 3.x. Pendente: o modelo padrão do serviço ainda é
`gemini-2.5-flash` (`DEFAULT_MODEL_ID`).

**Batelada 0.2.0 (2026-09-28 a 2026-10-02), vinda da integração do R8 em produção — feita:**
erros de configuração claros (503 com `error` em vez de 500), `dry_run` nas tools, `prompt_cache`,
lista de modelos sem esperar o provedor, contexto de teste pré-preenchido no Playground, busca nos
Logs por sessão/usuário/metadata, falha de tool no trace (404 não conta), complexidade do run e tools
com efeito colateral, dashboard dos Logs por perguntas (falhas, custo, versões, tendência) com fila de
revisão (`kuro runs list -c/--tool-failed/--side-effect/--feedback/--sample`), fila de espera por vaga
de execução (§0, Sprint 3 item 3) e fontes do frontend servidas pelo app, sem `next/font/google` no
build. O detalhe para quem integra está em `docs/notas-de-versao.md`.

**Operação por agentes e pelo terminal (2026-10-03) — feito:** três trilhas em paralelo.
1. ~~Servidor MCP~~ **Feito** (`mcp_server.py`, extra `mcp`, ver §5.5): as operações da CLI como
   tools, por stdio, na máquina de quem opera e falando com o serviço local ou na VPS.
2. ~~TUI~~ **Feito** (`tui/`, extra `tui`, ver §4.1): `kuro dash` com as execuções ao vivo, o trace e
   o panorama.
3. ~~Documentação distribuída~~ **Feito**: o README virou vitrine e índice, com um assunto por
   arquivo em `docs/`. `tests/test_docs_links.py` quebra a CI com link quebrado.

A base comum dos três é o `Client` da CLI. O que não é apresentação (o stream do chat, o eval) saiu
do `typer` para os três reaproveitarem.

**Acesso remoto, fila de jobs e RAG (2026-10-03 a 2026-10-27) — feito:**
1. ~~MCP por HTTP~~ **Feito**: o serviço atende o MCP em `/mcp` (com domínio ou pelo profile `ip`),
   e `kuro connect usuario@servidor` liga o MCP pelo SSH, sem porta aberta. `kuro mcp-config` monta a
   configuração pronta para colar. Pelo SSH e pela URL as tools que leem arquivo por caminho são recusadas.
2. ~~Fila durável~~ **Feito** (`jobs/`, migração 0011): fila de jobs no Postgres com
   `FOR UPDATE SKIP LOCKED`, lease, retentativa e `LISTEN/NOTIFY` para acordar o worker; `kuro jobs
   list/show/retry/cancel`. O sync de sites já roda nela. É a base que o webhook `on_complete` do
   procedural esperava (§4.2).
3. ~~RAG de produção~~ **Feito**: busca híbrida e configuração de busca versionada por collection (o
   run grava `collection_version`); sites como fonte (crawl/sitemap/page, robots.txt, egress travado,
   ressincronização incremental, PDF/DOCX/PPTX/XLSX linkados); `sources` na resposta do `/chat`,
   `/analyze` e stream; os trechos lidos no trace (`metadata.references`); feedback do operador por
   trecho; ciclo de correção (revisão no mesmo `content_id`, marcação resolvida, `retest` sem modelo,
   `reindex`) e um corte único de trechos para todo documento.
4. **Extra:** Gemini 3+ sem `temperature`/`top_p`/`thinking_budget`, HTTPS pelo IP sem domínio
   (profile `ip`, sslip.io), console com os Logs divididos em Panorama, Execuções e Sessões.

**Correções da integração do R8 (2026-10-27) — feito:** vindas de uma sessão de operação do Regente.
1. ~~Falha do modelo gravada como "interrompido"~~ **Corrigido**: depois de um `run_error` a rota
   responde 502 e fecha o stream, e isso sobrescrevia o `error` por `interrupted` ("quem chamou
   desconectou"), no trace store e no Langfuse. Um `MALFORMED_FUNCTION_CALL` do Gemini fez investigar a
   conexão do Regente em vez do modelo.
2. ~~502 do `/analyze` contado como sucesso~~ **Corrigido**: sem saída estruturada (vazio, texto,
   nenhum `run_completed`) o run ficava `success`, e o painel mostrava 0 falhas com o chamador no
   fallback. Agora é `error` com a mesma mensagem do 502.
3. ~~Segredo em `headers` de tool em texto puro~~ **Corrigido**: `tool_get` mascarava só `auth`; um
   header com nome de segredo (`X-...-Token`, `Authorization`) passa a sair mascarado.
   **Falta, do lado da operação:** mover os tokens das tools do R8 para `auth.value` e rotacioná-los.

**Pendente dessa mesma sessão:**
- Confirmação recusada no MCP: quando o cliente *recusa* a elicitation (e não só
  quando não a suporta), a tool devolve a mensagem genérica do SDK. Devolver o comando da CLI também
  nesse caso evita contornar a confirmação com script.
- Tools são compartilhadas entre draft e produção: alterar pelo draft uma tool que um agente de
  produção usa vale na hora. Avisar no `tool_apply`/`tool_set` (e no console) quais agentes de
  produção a usam, ou pedir confirmação nesse caso.
- Conferir o modelo gravado nos runs: houve runs depois de uma troca de modelo mostrando o modelo
  anterior. O cache de agentes revalida pelo `updated_at`, então a hipótese é a troca ter sido no
  draft; falta confirmar com `run_show` (`agent_version` dos runs) antes de tratar como bug.

**Adiado de propósito** (há um consumidor só): tabela `api_keys` multi-chave, multi-tenancy
estrutural/RLS, RBAC no console, snapshot com cache e `LISTEN/NOTIFY`, dependências `tool_only`,
migrar a ficha do R8 e o reset do R4 para o procedural (§4.2, já disponível), sandbox Python (manter
`CUSTOM_PYTHON_TOOLS_ENABLED` desligado em produção), exportador OpenTelemetry e GitOps de tools e
collections.

## 1. Onde o produto está

**Já tem, e bem feito:**

- Agentes dinâmicos em Postgres, resolvidos sem restart, com versão de prompt (com diff) e versão da
  configuração inteira gravada em cada execução.
- Três formatos de tool (builtin, API descrita em JSON, Python). Cada parâmetro declara a origem
  (`model`/`dependency`/`const`), o que impede que um CPF passe pelo modelo. Isso é um diferencial real.
- Contrato de integração derivado dos dados (`/agents/{t}/integration`): a documentação não diverge do código.
- Dois tipos de agente: `conversational` e `analysis` (one-shot com `response_schema` validado).
- Anexos multimodais, RAG por collection, Mem0 plugável e a "nota de feedback" (um agent.md do admin).
- Observabilidade no próprio Postgres (trace store local), com o Langfuse como exportador opcional.
- Ciclo de mudança validada: draft → `kuro eval` → promote, e modo shadow com concordância.
- CLI `kuro` pensada para outras IAs (`--json`, códigos de saída, `--no-input`, `AGENTS.md`).

**Falta, em ordem de gravidade:**

| Lacuna | Evidência | Por que importa |
|---|---|---|
| ~~Nenhuma autenticação~~ | **feito (parcial)** — `api/auth.py`: chave de API com escopos `runtime`/`admin`, por middleware (cobre também as rotas do AgentOS, onde a conversa está). Falta a tabela `api_keys` com várias chaves, revogação e `last_used_at`: hoje são duas chaves de ambiente, o tamanho certo para um tenant |
| ~~SSRF nas tools de API~~ | **feito** — `tools/egress.py` resolve o destino e recusa loopback/privado/link-local antes de cada chamada, nos dois caminhos de rede (`kind="api"` e o `httpx` das tools Python), com `TOOL_EGRESS_ALLOWLIST` para o host interno legítimo |
| ~~Sem migrações~~ | **feito** — Alembic em `agent_service/migrations`, com a base adotando o banco que já existia |
| ~~Sem CI~~ | **feito** — `.github/workflows/ci.yml`: pytest, lint e build do frontend, e build das duas imagens, em todo push e PR |
| ~~Versionamento parcial~~ | **feito** — `agents/versions.py`: a configuração inteira (modelo, tools, schema, regras de feedback) vira `agent_version`/`config_hash`, gravados em cada run |
| ~~RAG preso ao Google~~ | **feito** — `documents/embedder.py`: cada collection escolhe o embedder na criação (`google`/`openai`/`ollama`) e a chave sai de `/model-credentials`, como a do modelo. Fixo depois de criada de propósito: a tabela de vetores é de um embedder só |
| ~~Modo só-terminal fica sem logs~~ | **feito** — `observability/run_store.py`: todo run fica no Postgres do serviço, e o Langfuse virou um exportador opcional |

## 2. Posicionamento (para guiar as escolhas)

> **"Infra de agentes validada, operável por outro agente em poucas linhas."**

Três superfícies com papéis bem separados: **API** para integrar (contrato estável), **CLI/MCP** para
operar e corrigir, **UI** opcional para inspecionar. Toda feature nova deveria chegar primeiro na API +
CLI e só depois na UI. A paridade entre as três está fechada e documentada como matriz em [docs/cli.md](docs/cli.md#paridade-entre-api-cli-e-console)
(ver §6 para o que sobrou de fora e por quê).

A palavra que sustenta o produto é **validada**. O ciclo que une as peças — *mudei o agente → provo
que não piorou → publico* — existe desde o Sprint 2 (ver §0 e §5); o que falta nele está na §5.

---

## 3. Horizonte 0: fundação (bloqueia o resto; ~2–3 semanas)

1. ~~**Autenticação por API key com escopos.**~~ **Feito em parte** (`api/auth.py`): duas chaves de
   ambiente (`ADMIN_API_KEY`, `RUNTIME_API_KEY`) com os escopos abaixo, cobrando por middleware para
   alcançar também as rotas do AgentOS (`/sessions`, `/knowledge/...`), que uma dependência por router
   deixaria de fora. A CLI lê `KURO_API_KEY` e o BFF do Next injeta a chave admin server-side. **Falta**
   a tabela `api_keys` (hash, escopos, tenant, `last_used_at`) para várias chaves e revogação sem
   restart — necessário assim que houver mais de um consumidor ou mais de um tenant.
   Contexto original: tabela `api_keys` (hash, escopos, tenant, `last_used_at`).
   Escopos mínimos:
   - `runtime`: `/chat`, `/analyze`, `/chat/stream`, scores e referência do shadow. É o que os outros
     módulos recebem.
   - `admin`: CRUD de agentes, tools, credenciais, collections e leitura de traces. É o que CLI/UI usam.

   A CLI lê `KURO_API_KEY`. O BFF do Next injeta a key no server-side, como já faz com a URL.
   Quando isso existir, `CUSTOM_PYTHON_TOOLS_ENABLED` passa a fazer algum sentido.
2. **Multi-tenancy — adiado de propósito (§0).** `agent_type` é PK global, e o mesmo vale para tools,
   collections e credenciais. Com um consumidor só, a decisão de 2026-09-24 foi não pagar isso agora:
   as tabelas novas (`runs`) já nascem com `tenant_id`, e as antigas migram quando houver o segundo
   cliente — agora com Alembic, o que torna a migração de 5 tabelas um arquivo revisável.
3. ~~**Alembic.**~~ **Feito** no Sprint 2 (`agent_service/migrations`, ver §0).
4. ~~**CI (GitHub Actions).**~~ **Feito** (`.github/workflows/ci.yml`): pytest, `npm run lint`,
   `npm run build` e build das duas imagens. Sem `ruff` por enquanto — não está nas dependências de
   dev nem configurado, e adicioná-lo junto com a CI misturaria "passar a rodar os testes" com
   "formatar o repo inteiro".
5. ~~**Proteção de egress nas tools de API.**~~ **Feito** (`tools/egress.py`): resolve o DNS e recusa
   loopback, privado, link-local, multicast e reservado, com `TOOL_EGRESS_ALLOWLIST` por host. A
   checagem roda com a URL já montada, porque um parâmetro `location="path"` pode compor o host. O
   `httpx` das tools Python passa pelo mesmo controle — sem isso a trava seria enfeite. Falta a parte
   por tenant, que depende do item 2. Limite conhecido: DNS rebinding (checamos e o httpx resolve de
   novo ao conectar); fechar isso é papel de um egress firewall no ambiente.
6. **Tirar I/O síncrono do event loop.** **Feito em parte** no Sprint 1: `run_in_threadpool` nas três
   rotas de execução. Continua valendo o resto: `api/routes.py::chat` é `async`, mas `_resolve` faz
   consultas síncronas no Postgres: `get_definition`, `get_feedback_note` (duas vezes: no stamp e no
   build) e um `get_tool` por tool, ou seja N+2 round-trips bloqueantes por mensagem. Opções, da mais
   barata para a mais completa:
   - envolver em `run_in_threadpool`;
   - uma query só, com join, que devolve os stamps;
   - cache em memória com invalidação por `LISTEN/NOTIFY` do Postgres, que funciona com vários workers.

## 4. Horizonte 1: as suas duas ideias

### 4.1 UI opcional ("liga/desliga")

Custa pouco, porque o frontend já é um serviço separado que só consome a API. **Não faça isso com flag
no código, use `profiles` do Compose.** O peso real nem é a UI: é o Langfuse (ClickHouse + MinIO +
Redis próprio + worker, ~16 GB recomendados). Quem quer só terminal quer os dois desligados.

**Feito.** `frontend` está em `profiles: ["ui"]` e os cinco serviços do Langfuse em
`profiles: ["observability"]`; `LANGFUSE_ENABLED` passou a ter default `false` no `agent-service`,
como a ressalva abaixo exigia. `docker compose up -d` sobe dois serviços (postgres e agent-service —
o Redis do núcleo saiu no Sprint 1); o comando completo sobe oito. Há ainda um profile
`tls`, que põe um Caddy na frente com certificado do Let's Encrypt e renovação automática — sem
ele a chave de API trafegaria em texto claro, o que inviabilizava o serviço ser chamado por
outra plataforma em rede real.

```bash
docker compose up -d                                   # núcleo: postgres, agent-service
docker compose --profile ui --profile observability up -d   # completo
```

Para ficar redondo:

- ~~**Desligar o tracing junto com o profile.**~~ **Feito.** Só remover os containers não bastava: o
  compose passa as chaves ao `agent-service` de qualquer jeito, então `configure_tracing()` continuava
  instrumentando e cada run tentava exportar para um host que não existe (timeout de 20 s). Hoje
  `LANGFUSE_ENABLED` é `false` por padrão **nos três lugares** — `config.py`, `.env.example` e o
  compose — e sobe com `LANGFUSE_ENABLED=true docker compose --profile observability up -d`. Os três
  discordavam entre si, o que fazia o comportamento depender de como o serviço tinha subido.
- **`kuro up [--ui] [--observability]` / `kuro down` / `kuro status`**: um wrapper do compose,
  para quem opera tudo pela CLI. Grave a escolha em `.kuro/config.toml`.
- ~~**Preço do modo enxuto: um trace store local.**~~ **Feito** no Sprint 1
  (`observability/run_store.py`): `kuro runs` funciona sem Langfuse, sessões e stats viraram
  `GROUP BY` sobre o histórico inteiro, e "taxa de 👍 por versão" é uma consulta. O Langfuse virou
  um exportador *adicional*, e nem vem mais na imagem padrão (extra `[observability]`).
- Com a UI desligada, a CLI precisa cobrir tudo. Ver a tabela de paridade na §6.
- ~~**TUI de verdade.**~~ **Feito** (2026-10-03, [docs/tui.md](docs/tui.md)): `kuro dash` em
  [Textual](https://textual.textualize.io), com as execuções chegando ao vivo (filtro por agente,
  status e testes), o trace navegável (spans de modelo e tools, com o caminho até um erro já
  aberto) e o panorama do `/observability/overview`. **Falta:** o funil por etapa (o procedural
  já existe, §4.2; o funil está no console e em `kuro agents procedures`), e operar pelo painel (testar e editar um agente). Decidir sobre essa parte
  depois de usar, porque o shell já cobre.

### 4.2 ~~Agente procedural (etapas)~~ — feito em 2026-10-03

**Feito** como desenhado abaixo, com estes detalhes ([docs/conceitos.md](docs/conceitos.md#agente-procedural-fluxo-em-etapas)):

- `kind="procedural"` com `stages`, de três tipos: `collect` (campos com `enum` e `pattern`),
  `confirm` e `action`. Máquina de estados pura em `agents/procedural.py`, estado em
  `procedure_runs` (migração 0007).
- A confirmação é montada dos dados, sem LLM. Uma `action` só existe depois de uma `confirm`,
  validado no cadastro.
- A ação é reservada com trava otimista e gravada antes da resposta; nunca roda duas vezes. Se
  falhar, a pessoa precisa confirmar de novo. 409 para mensagem concorrente na mesma sessão.
- `ChatResponse.state` e o evento SSE `state`; `GET /agents/{t}/procedures/{sessão}` e o funil
  `GET /agents/{t}/procedures`. Tudo também na CLI (`kuro agents procedures`), no MCP e no console
  (editor de etapas, faixa e etapas no chat, funil).

**Ficou de fora, de propósito:**
- o webhook `on_complete`, porque precisa de uma fila de entrega durável (Postgres `SKIP LOCKED`, com
  retentativa). Essa fila já existe (`jobs/`, 2026-10, §0): falta o job de entrega, a assinatura do
  payload e o contrato. Por enquanto quem integra lê `state.done` e `state.result` na resposta;
- a migração da ficha do R8 e do reset do R4 para procedural;
- o funil no `kuro dash`.

**Desenho original:**

**Verificação no Agno 3.0.9 instalado:**

- `agno/workflow` (Step/Router/Loop/Condition) é um pipeline que roda até o fim. Não foi feito para
  esperar o usuário responder entre uma etapa e outra.
- O HITL do Agno (`requires_confirmation` / `requires_user_input` + `continue_run`) pausa **dentro de
  um run, no nível da tool**. Isso serve para "confirme antes de eu executar a ação", mas não para ser
  o motor de etapas de uma conversa em que cada turno é um `/chat` novo.

**Recomendação:** uma máquina de estados determinística, no nosso código, com o estado numa tabela
própria: `procedure_runs(agent_type, session_id, stage, slots, status, updated_at)`. O `session_state`
do Agno é carregado da sessão a cada run (`agent/_run.py` → `run_context.session_state`), então serve
para *injetar* a etapa e os slots no contexto. Mas a fonte da verdade precisa ser consultável: o funil
por etapa vira um `GROUP BY stage`, e o produto não fica dependente da semântica de sessão do Agno. O LLM faz só duas coisas: extrair dados da mensagem (saída estruturada) e redigir a
resposta. Quem decide avançar, voltar ou concluir é o servidor. É isso que torna o agente
"infra validada", e não um prompt comprido pedindo "siga as etapas".

`kind="procedural"`, com uma coluna nova `stages`:

```json
{
  "agent_type": "abertura-chamado",
  "kind": "procedural",
  "stages": [
    { "id": "identificacao",
      "goal": "Obter CPF e nome do cliente",
      "fields": [ {"name": "cpf", "type": "string", "required": true, "pattern": "^\\d{11}$"},
                  {"name": "nome", "type": "string", "required": true} ],
      "prefill_from_dependencies": true },
    { "id": "problema",
      "goal": "Entender o problema",
      "fields": [ {"name": "categoria", "type": "string", "enum": ["internet","tv","fatura"]},
                  {"name": "descricao", "type": "string", "required": true} ],
      "tools": ["consulta_contrato"] },
    { "id": "confirmacao", "type": "confirm" },
    { "id": "abrir", "type": "action", "tool": "abrir_chamado", "on_success": "done" }
  ],
  "on_complete": { "emit": "webhook", "url": "https://regente.interno/kuro/procedures" }
}
```

Regras de design:

- **Reaproveite o que já existe.** `fields` usa o mesmo formato de `dependency_fields`/`response_schema`
  (e o mesmo `validate_field_specs`). A extração de cada etapa usa `build_response_model`, que já monta
  o schema do agente `analysis`. As etapas `action` chamam tools de API com `source: "dependency"`, e
  os slots coletados entram como dependências.
- **O estado da sessão não pode ficar no `Agent` em cache.** `agents/registry.py` guarda um `Agent`
  por `agent_type`. A etapa atual e os slots são lidos de `procedure_runs` a cada run, e as instruções
  da etapa entram por run (`session_state`/contexto adicional), nunca na instância compartilhada.
- **A confirmação é montada a partir dos slots, sem LLM.** Um template ("Confirma: CPF ***.456.789-00,
  categoria internet…?") impede que o modelo "confirme" um dado que não coletou.
- **Correções e volta atrás:** se o usuário disser "na verdade o CPF é outro", a extração detecta o
  campo já preenchido, o servidor volta para a etapa dona do campo e invalida as etapas seguintes.
- **Pular etapa quando o dado já chegou:** se quem chama mandou `cpf` em `dependencies`, a etapa de
  identificação fica pronta de saída.
- **O contrato de resposta cresce sem quebrar:** `ChatResponse.state = {stage, collected, missing,
  done, result}`. O módulo integrador sabe quando acabou sem precisar interpretar o texto.
- **Ao concluir,** o resultado estruturado vai para um webhook de quem integra, com a entrega
  enfileirada no Postgres (`SKIP LOCKED`, com retentativa) — sem Redis (ver §8).
- **Observabilidade:** cada transição de etapa vira um span/evento. `kuro runs show` mostra a trilha
  `identificacao → problema → confirmacao → abrir`, o que ajuda muito a depurar onde os usuários
  desistem (funil por etapa na UI).
- **UI:** um stepper no inspector do chat. **CLI:** `kuro chat` mostra `[2/4 problema] faltando: descricao`.

## 5. Horizonte 2: "validada" de verdade (o diferencial)

1. **Replay / avaliação de regressão.** **Feito em parte** (Sprints 2 e 3): `kuro eval` roda um
   dataset JSONL com comparação determinística campo a campo, regras de política e `--compare`
   com a produção, e grava a nota `eval` em cada run; `kuro runs export` transforma as execuções
   com referência do shadow em dataset. **Falta:** o LLM-juiz para a qualidade do texto (agentes
   conversacionais) e o replay direto de execuções sem referência (`--last 50`).
2. ~~**Versionar a configuração inteira, não só o prompt.**~~ **Feito** no Sprint 2
   (`agents/versions.py`): `agent_version` + `config_hash` em cada run, criados quando o registry
   remonta o agente. As "etapas" entram quando o procedural existir.
3. **Canais `draft` / `prod` e fixação de versão.** **Feito de outro jeito:** o draft é um agente
   comum e `kuro agents promote` copia a configuração (também pelo console). **Falta**, se houver
   necessidade: fixar a versão por chamada (`suporte@12`) e canário por % de sessões.
4. **A nota de feedback precisa de governança.** **Metade feita.** Ela deixou de ser um markdown
   sobrescrito e virou uma lista de regras com id: o modelo devolve operações (`edit`/`remove`/`add`)
   em vez de reescrever tudo, o servidor funde as regras parecidas demais, cada gravação vira uma
   versão com rollback, e o `POST` responde com o `diff`. O merge também passou a usar o modelo e a
   credencial do próprio agente, não o provedor padrão.
   **Continua faltando** o que separa "editável" de "governado":
   - `--dry-run` que mostra as regras resultantes antes de gravar;
   - modo "proposta", em que as regras caem em `draft` e passam pelo `kuro eval` antes de ir para prod
     — hoje elas valem na próxima mensagem de produção, sem revisão;
   - agrupar vários 👎 comentados num merge só (lote semanal), em vez de um merge por feedback.
5. ~~**Servidor MCP do Kuro.**~~ **Feito** (2026-10-03, [docs/mcp.md](docs/mcp.md)): `kuro-mcp` por
   stdio, com 43 tools que espelham a CLI. O Claude Code e outros agentes operam sem passar pelo
   shell. Toda operação que na CLI pede `--yes` pede confirmação à pessoa por elicitation, sem
   parâmetro que o modelo possa preencher. Os testes rodam em `dry_run` por padrão, e chave de
   modelo não passa pelo MCP.
   **Falta:**
   - ~~transporte HTTP~~ **feito** (2026-10): `/mcp` no serviço e `kuro connect` pelo SSH;
   - devolver o comando da CLI quando a pessoa (ou o cliente) recusa a confirmação, não só quando o
     cliente não suporta elicitation;
   - decidir se editar um agente de produção (`agent_set`/`agent_apply`) também pede confirmação.
     Hoje segue a CLI e não pede;
   - a *skill* do Claude Code com o ciclo draft → eval → promote.
6. **Agentes como código (GitOps).** **Feito para agentes** no Sprint 3: `kuro agents export -o dir`
   grava um JSON por agente, `kuro agents apply -f dir --dry-run` valida no servidor e mostra o que
   mudaria, e `apply -f dir` só envia o que mudou (o fluxo de CI está em `docs/integracao.md` §6).
   **Falta:** o mesmo para tools e collections, e `kuro schema agent` com o JSON Schema para o editor.

## 6. Usabilidade

**Paridade CLI × UI** (necessária para a UI poder ser desligada):

| Capacidade | API | UI | CLI |
|---|---|---|---|
| Criar/editar tools | ✅ | ✅ | ✅ `tools apply -f` / `set` / `delete` / `get --editable` |
| Sessões (listar, ver transcrição, apagar) | ✅ | ✅ | ✅ `kuro sessions list/show/delete` |
| Estatísticas (KPIs, custo por agente) | ✅ | ✅ | ✅ `kuro runs stats` |
| Seguir execuções ao vivo | — | — | ✅ `kuro runs tail --agent x` (consulta repetida, não um stream) |
| Restaurar versão | via PUT | ✅ | ✅ `kuro agents rollback <t> <v>` |
| Upload de arquivo para collection | ✅ | ✅ | ✅ `collections add -f doc.pdf` |
| Documentos indexados (listar, apagar) | ✅ | ✅ | ✅ `collections docs` / `rm-doc` |
| Regras de feedback (ver, editar, histórico, rollback) | ✅ | ✅ aba Aprendizado | ✅ `agents feedback --show/--remove/--versions/--rollback` |
| Rodar um agente analista | ✅ | ✅ página Análise | ✅ `kuro analyze` |

A paridade fechou. O upload de arquivo saiu do `/knowledge/content` do AgentOS
(que só alimenta a collection padrão) para uma rota própria,
`POST /collections/{nome}/files`, que resolve a collection por nome e usa o
embedder dela. Sobraram duas exceções, com motivo declarado na [matriz](docs/cli.md#paridade-entre-api-cli-e-console):
**`runs tail`** e **testar uma chave antes de salvá-la**. A terceira — indexar por **URL**, que
dependia do pipeline do AgentOS e só alimentava a padrão — virou fonte de site em qualquer
collection (`/collections/{nome}/sources`). O fluxo de CI (`eval`, `runs export`,
`agents export`/`apply -f dir`, `--dry-run`) é só da CLI de propósito: trabalha com
arquivos do repositório de quem mantém os agentes. Versões da configuração,
promote e concordância do shadow estão também no console.

`kuro sessions` lê a conversa em si (rotas de sessão do AgentOS); `runs stats`,
`runs tail` e `runs sessions` leem o trace store local — todos funcionam sem Langfuse.

**Outros pontos:**

- `kuro agents new --template {suporte|extrator|procedural}`: parte de um exemplo que funciona em vez
  de um JSON vazio. Os templates também servem de documentação viva.
- `--dry-run`: **feito em `apply`** (Sprint 3). Falta em `set` e em `feedback` (ver as regras
  resultantes antes de gravar).
- Mensagens de erro com a próxima ação sugerida (já acontece em várias rotas, então vale virar padrão),
  por exemplo `"hint": "kuro agents set x dependency_fields=..."`.
- Custo visível no fluxo: `kuro chat` e o `ChatResponse` síncrono devolvem `usage` e custo estimado
  (hoje só o stream envia `usage`).
- Um **orçamento por agente** (tokens/dia ou R$/mês), com alerta e bloqueio: o trace store local e o
  custo estimado por modelo (`models/pricing.py`) já existem, então é uma consulta + um corte.

## 7. Otimizações técnicas na stack

| Onde | Problema | Sugestão |
|---|---|---|
| ~~`agents/base.py`~~ **feito (2026-09-26)**: `memory_backend` = `none` (padrão de agente novo) / `auto` / `agentic` / `mem0`, com limite de memórias no prompt, modelo auxiliar (`AUX_MODEL_ID`), resumo incremental da sessão e raciocínio por nível (`model_params.reasoning`) — `memory/managers.py`. Antes: todo agente `common` ligava `enable_agentic_memory` + `add_memories_to_context`, o que traz chamadas extras ao LLM. ~~Com `mem0`, as duas memórias rodam em paralelo~~ (corrigido: o `mem0` agora substitui a memória do Agno) | Memória explícita em três modos: `none` / `agno` / `mem0`. A maioria dos agentes de atendimento com `user_id` efêmero não precisa de memória longa |
| `memory/mem0_backend.py` | `MemoryClient` é o Mem0 **hospedado**, então dados do cliente (CPF etc.) saem da sua infra. O post-hook salva só a mensagem do usuário, de forma síncrona | Mem0 OSS (`Memory`) sobre o mesmo pgvector, por questão de LGPD; salvar o par usuário/assistente; hook assíncrono em background |
| ~~`agents/base.py` + `/analyze`~~ | **corrigido**: o agente de análise recebia `db=get_db()` e cada chamada criava uma sessão `analyze-<uuid>` no Postgres, com o documento inteiro dentro | `db=None` em `kind="analysis"`. O Agno guarda todo acesso a sessão com `if agent.db is not None`, e a busca na collection não usa `agent.db`, então o RAG continua valendo |
| Anexos | O base64 fica no histórico da sessão e volta a cada turno (o `AGENTS.md` já avisa) | `POST /files` que devolve `file_id`, guardar em storage de objetos e no histórico manter só a referência (o MinIO do compose é do Langfuse e está no profile opcional: usar o Postgres ou um volume evita trazer ele de volta ao núcleo) |
| ~~`documents/collections.py`~~ | **corrigido em parte**: o embedder era fixo em Gemini com a `GOOGLE_API_KEY` do ambiente | Embedder e credencial por collection (`documents/embedder.py`), escolhidos na criação e vindos do cofre. O `lru_cache` agora é chaveado pelo embedder, mas **rotação de credencial ainda exige restart** — a chave já está dentro do cliente |
| AgentOS no boot | Agentes e collections criados depois do boot não aparecem nas rotas do AgentOS | Se o playground do os.agno.com não for essencial, deixe o AgentOS só para ingestão, ou remova (ver §8) |
| Seleção de modelo | Ainda não há fallback (já previsto) | Com vários provedores já cadastrados, dá para ligar `fallback_models` por agente agora |
| Streaming | O `/chat` síncrono agrega o stream em memória | ~~Timeout por run~~ **feito** (`timeout_seconds`, 504) e o cancelamento na desconexão já existe; falta `max_tool_calls` por agente contra loops de tool |
| ~~Tool de API travando o event loop~~ | **corrigido**: o entrypoint era síncrono e o Agno chama entrypoint síncrono direto no caminho `async` (`Function.aexecute`), então uma chamada de 15s parava todas as requisições do worker | Entrypoint `async` com `httpx.AsyncClient` único — o pool também evita refazer o handshake TLS a cada chamada |
| Escala | Cache por processo; `MAX_CONCURRENT_RUNS` também é por processo; migração no startup sem lock | Com o `LISTEN/NOTIFY` do H0 dá para rodar com `--workers N` ou várias réplicas sem cache velho; advisory lock do Postgres no `upgrade_database` antes de subir mais de um processo |

## 8. O que cortar ou não fazer

- ~~**`messaging/redis_streams.py::consume_tasks`**~~ **Saiu** no Sprint 1, junto com o Redis do
  núcleo: era código morto (ack antes de processar, sem dead-letter). Decisão de 2026-09-25: **sem
  fila no Kuro** — quem integra já tem a própria (no Regente, claim + daemon), e o Kuro responde 503
  com `Retry-After` quando está no limite. Quando houver execução assíncrona de verdade (evento de
  conclusão do procedural, `POST /chat/async`), a fila vai para o Postgres (`SELECT … FOR UPDATE
  SKIP LOCKED`), não de volta para o Redis.
- **Tools `python` enquanto não houver isolamento real.** O próprio código admite que o namespace
  restrito não é uma sandbox. As tools de API cobrem a maior parte dos casos. Se forem mantidas,
  precisam rodar em um container efêmero, sem rede e com limite de CPU/memória, e isso é um projeto à
  parte. Prefira esconder do produto até lá.

  Revisadas em 2026-09-22, sem mudar essa conclusão. Foram corrigidos os dois problemas com efeito
  operacional, e o resto virou documentação honesta no docstring do módulo:
  - o `httpx` das tools Python agora passa pelo controle de egress (era o desvio da trava nova);
  - o timeout não interrompe o código (Python não mata thread de fora) e todas as tools dividiam um
    pool de 8 workers, então **oito tools travadas paravam todas as tools Python do serviço**. Agora
    cada chamada roda na própria thread daemon, com teto de 32 simultâneas: a travada queima um slot,
    não a fila inteira.
  - continuam em aberto, e só o isolamento de verdade resolve: sem limite de memória/CPU
    (`[0] * 10**10` derruba o container) e `str.format` alcançando atributos (`"{0.__class__}"`),
    que vaza informação mas só produz texto.
- **Agente "orquestrador" genérico.** O procedural resolve o caso de uso concreto (fluxos guiados) com
  muito menos risco. Faça o orquestrador só quando existir um roteamento real entre agentes que o
  procedural não resolva.
- **Dependência do playground do os.agno.com.** Seu console já cobre isso. Manter o AgentOS só por ele
  traz a ressalva de "aparece só depois do restart".
- ~~**Multi-provedor de embeddings antes do trace store local e da auth.**~~ Este corte caiu: a auth
  saiu antes (H0), e o que motivou o multi-provedor não foi ganho de qualidade e sim uma dependência
  errada — o RAG exigia uma chave do Google mesmo num serviço rodando os agentes em OpenAI, e ignorava
  o cofre de credenciais.

## 9. Sequência sugerida

Os prazos são estimativas grosseiras para dar ordem de grandeza, não compromisso. Repriorizada em
2026-09-24 (ver §0): o que destrava o Regente vem antes, e o resto espera uma necessidade real.

| Fase | Entregas | Resultado |
|---|---|---|
| ~~**S1**~~ feito | Trace store em Postgres, Langfuse como extra opcional, Redis fora do core, `_resolve` em threadpool | Produção enxuta com runs auditáveis |
| ~~**S2**~~ feito | Alembic, `config_hash` + `agent_versions`, `kuro eval` v0 determinístico, draft como agente comum + `promote` | Mudança de agente provada antes de ir para prod |
| ~~**S3**~~ feito | Parâmetros do modelo, `enum`, timeout, limite de simultâneas, metadata, shadow (referência, concordância, export), `/ready`, agentes como código, guia de integração | O Regente integra sozinho |
| ~~**Operação**~~ feito | Servidor MCP (`kuro-mcp`), `kuro dash`, documentação dividida por assunto | Agentes de IA operam o Kuro por tools; humanos, pelo terminal |
| ~~**Procedural**~~ feito | `kind="procedural"`: etapas, confirmação sem LLM, ação com trava, `state` no `/chat`, funil, console | Fluxos guiados conduzidos pelo servidor |
| **Regente**, contínuo | R8 shadow → prod, R4 shadow → prod, R6 shadow → assistido → autônomo | Regente fora do `ServiceLLM::chatJson()` |
| ~~**Remoto, jobs e RAG**~~ feito | MCP por `/mcp` e SSH, fila de jobs no Postgres, RAG híbrido com sites, fontes na resposta e ciclo de correção | Operação remota e base de conhecimento de produção |
| **Próximo** | Confirmação recusada no MCP com o comando da CLI, aviso de tool compartilhada entre draft e prod, `fallback_models`, `max_tool_calls`, `usage`/custo no `ChatResponse`, modelo padrão fora do Gemini 2.5 | Menos armadilha para quem opera e integra |
| **Depois** | `api_keys`/tenant/RLS, webhook de conclusão do procedural (a fila já existe), `tool_only`, cache com `LISTEN/NOTIFY`, lock de migração, sandbox Python, LLM-juiz no eval, `agente@versão`, `kuro up/down`, GitOps de tools/collections, orçamento por agente | Plataforma completa, por necessidade |

A paridade CLI × console está fechada e documentada como matriz em [docs/cli.md](docs/cli.md#paridade-entre-api-cli-e-console) (as exceções, com motivo,
estão na §6). O console foi atualizado com o que os Sprints 2 e 3 trouxeram em 2026-09-25.
