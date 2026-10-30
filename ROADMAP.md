# Roadmap da Kuro Agent Platform

O que já existe, o que vem a seguir e o que fica de fora de propósito. O detalhe de cada entrega
está em [docs/notas-de-versao.md](docs/notas-de-versao.md) e no histórico do git; aqui fica só o
mapa.

> Última revisão: 2026-10-30, conferida contra o código.

**Como ler:** [Feito](#feito) é o que já está no ar, por área. [Próximo](#próximo) é o que vale fazer
agora, em ordem. [Quando houver necessidade](#quando-houver-necessidade) é o que só se paga com um
gatilho concreto, que vem escrito ao lado. [Fora de propósito](#fora-de-propósito) são decisões
tomadas, com o motivo.

---

## Feito

### Agentes

- Agentes definidos em JSON e guardados no Postgres, criados e editados sem deploy nem restart.
- Três tipos: **conversacional**, **analista** (one-shot, com `response_schema` validado e `enum`
  nos campos) e **procedural** (etapas `collect` → `confirm` → `action` conduzidas pelo servidor,
  confirmação montada dos dados sem LLM, ação com trava para nunca rodar duas vezes, funil por etapa).
- Parâmetros do modelo por agente (`temperature`, `top_p`, `max_tokens`, `reasoning`, `prompt_cache`)
  e `timeout_seconds` (504 ao estourar).
- Memória de longo prazo por agente: `none`, `auto`, `agentic` ou `mem0`, e resumo da sessão.
- Anexos multimodais (imagem, áudio, vídeo, PDF, DOCX…) no `/chat` e no `/analyze`.
- Vários provedores (Gemini, OpenAI, Anthropic, Ollama), com a lista de modelos lida ao vivo do
  provedor e as credenciais cifradas no banco.

### Tools

- Três formatos: builtin, API HTTP descrita em JSON e Python.
- Cada parâmetro declara a origem (`model`, `dependency` ou `const`): um CPF vindo da requisição não
  passa pela escolha do modelo.
- Egress controlado: as tools só alcançam endereços públicos, com `TOOL_EGRESS_ALLOWLIST` para hosts
  internos legítimos. Vale também para o `httpx` das tools Python.
- `dry_run`: em teste, a tool recebe o header `X-Kuro-Dry-Run` e decide o que simular.
- Segredos em `headers` saem mascarados na leitura.

### Base de conhecimento (RAG)

- Collections com embedder escolhido na criação (Google, OpenAI ou Ollama), busca vetorial ou
  híbrida, configuração de busca versionada.
- Documentos (texto, PDF, DOCX, PPTX, XLSX, CSV…) e sites (crawl, sitemap ou página, com robots.txt
  e ressincronização incremental), todos cortados pela mesma regra de trechos.
- `sources` na resposta e os trechos lidos no trace.
- Ciclo de correção: feedback por trecho, revisão do documento no mesmo `content_id`, `retest` sem
  modelo e `reindex`.

### Ciclo de mudança validada

- Versão da configuração inteira (prompt, modelo, tools, schema, regras) gravada em cada execução,
  com histórico e rollback.
- Draft → `eval` → promote: o draft é um agente comum, o `eval` compara campo a campo com regras de
  política e reprova a candidata se ela ficar pior que a produção.
- Modo shadow: o sistema legado responde, o Kuro decide em silêncio, e a concordância sai por campo
  e por versão. Os runs com referência viram dataset do `eval`.
- Feedback do admin vira regras com id, com merge, diff, histórico e rollback.
- Agentes como código: `agents export` e `agents apply -f <dir> --dry-run`.

### Observabilidade

- Trace store no próprio Postgres: cada execução com spans de modelo e tools, tokens, latência e
  custo estimado. O Langfuse é opcional.
- Panorama (totais contra o período anterior, por agente e por versão, tools falhando), fila de
  revisão (`--tool-failed`, `--side-effect`, `--feedback`, `--sample`) e correlação por `metadata`.

### Interfaces

- **API** com contrato de erro documentado, `/health` e `/ready`.
- **CLI `kuro`** pensada para humanos e agentes: `--json`, códigos de saída previsíveis, sem prompt
  sem TTY.
- **MCP (`kuro-mcp`)** com as operações da plataforma como tools: local por stdio, remoto por `/mcp`
  ou por `kuro connect` via SSH. Testes em `dry_run` por padrão; remover, restaurar e promover pedem
  confirmação da pessoa.
- **`kuro dash`**: execuções ao vivo, trace e panorama no terminal.
- **Console web**: playground, logs, sessões, editor de agentes e etapas, bases de conhecimento.
- Paridade entre as três portas documentada na [matriz](docs/cli.md#paridade-entre-api-cli-e-console).

### Operação e segurança

- Autenticação por chave de API com escopos `runtime` e `admin`.
- Migrações com Alembic, CI (testes, lint, build do frontend e das imagens).
- Fila de jobs no Postgres (`SKIP LOCKED`, lease, retentativa), usada pelo sync de sites.
- Limite de execuções simultâneas com fila de espera (503 com `Retry-After` quando cheia).
- Instalação enxuta: o núcleo sobe só com Postgres e o serviço; console, Langfuse e HTTPS são
  profiles do Compose.

---

## Próximo

Em ordem. Cada item é pequeno e tira uma armadilha de quem opera ou integra.

| # | O quê | Por quê |
|---|---|---|
| 1 | **Confirmação recusada no MCP devolve o comando da CLI.** Hoje, quando a pessoa recusa, a tool devolve só `{"cancelled": true}`. | Igualar ao caso em que o cliente não suporta confirmação, e deixar claro como seguir. |
| 2 | **Aviso de tool compartilhada.** Alterar pelo draft uma tool que um agente de produção usa vale na hora. Avisar no `tool_apply`/`tool_set` e no console quais agentes de produção a usam, ou pedir confirmação. | O fluxo draft → promote não protege as tools. |
| 3 | **`usage` e custo no `ChatResponse` síncrono.** Hoje só o stream envia. | Quem integra pelo `/chat` não vê o custo da chamada. |
| 4 | **`max_tool_calls` por agente.** | Corta loops de tool que hoje só param no timeout. |
| 5 | **`fallback_models` por agente.** | Com vários provedores cadastrados, uma queda do provedor principal não precisa virar erro. |
| 6 | **Modelo padrão fora do Gemini 2.5** (`default_model_id` em `config.py`). | O Gemini 2.5 está saindo. |
| 7 | **Investigar o modelo gravado nos runs.** Houve runs, depois de uma troca de modelo, mostrando o anterior. A hipótese é a troca ter sido no draft; confirmar pelo `agent_version` dos runs antes de tratar como bug. | Um trace que mente sobre o modelo invalida a investigação. |

### Para fechar o ciclo de mudança validada

- **LLM-juiz no `eval`**, para agentes conversacionais (hoje o `eval` é determinístico e serve aos
  analistas).
- **Replay de execuções sem referência** (`eval --last 50`).
- **Governança do feedback:** `--dry-run` que mostra as regras resultantes, modo proposta (as regras
  caem no draft e passam pelo `eval`) e merge em lote.
- **Decidir se editar um agente de produção pelo MCP pede confirmação.** Hoje segue a CLI e não pede.
- **Uma skill do Claude Code** com o ciclo draft → eval → promote.

### Usabilidade

- `kuro agents new --template {conversacional|analista|procedural}`.
- `--dry-run` em `agents set` e em `agents feedback`.
- `hint` com a próxima ação nas mensagens de erro, como padrão.
- O funil do procedural no `kuro dash`.

---

## Quando houver necessidade

Cada item tem o gatilho que justifica fazê-lo.

| O quê | Quando |
|---|---|
| Tabela `api_keys` (várias chaves, revogação sem restart, `last_used_at`) | Mais de um sistema integrando |
| Multi-tenancy (`tenant_id` nas tabelas antigas, RLS) e RBAC no console | O segundo cliente. As tabelas novas já nascem com `tenant_id` |
| Cache de agentes com invalidação por `LISTEN/NOTIFY` e lock nas migrações | Rodar com mais de um worker ou réplica |
| Webhook de conclusão do procedural (a fila de jobs já existe) | Um integrador que não queira ler `state.done` na resposta |
| Fixar versão por chamada (`agente@12`) e canário por % de sessões | Precisar de duas versões de um agente no ar ao mesmo tempo |
| Orçamento por agente (tokens/dia ou R$/mês, com alerta e corte) | Custo virar assunto. O dado já está no trace store |
| Agentes como código também para tools e collections; `kuro schema agent` | Times mantendo tools e collections em repositório |
| Anexos fora do histórico da sessão (`POST /files` com referência) | Conversas longas com anexos grandes |
| Mem0 OSS sobre o próprio pgvector | Usar `memory_backend=mem0` com dado pessoal (o Mem0 hospedado tira o dado da infra) |
| Rotação de credencial de embedder sem restart | Rotacionar chaves com frequência |
| `kuro up` / `kuro down` / `kuro status` | Operar tudo pela CLI sem conhecer o Compose |
| Exportador OpenTelemetry | Um ambiente que já centralize traces |

---

## Fora de propósito

- **Tools Python em produção sem isolamento real.** O namespace restrito não é sandbox: não há limite
  de memória nem de CPU. Manter `CUSTOM_PYTHON_TOOLS_ENABLED` desligado em produção. As tools de API
  cobrem a maior parte dos casos; isolamento de verdade (container efêmero, sem rede, com limites) é
  um projeto à parte.
- **Redis de volta.** Fila e assíncrono ficam no Postgres (`SKIP LOCKED`).
- **Agente orquestrador genérico.** O procedural resolve os fluxos guiados com muito menos risco. Só
  quando houver um roteamento real entre agentes que ele não resolva.
- **Depender do playground do os.agno.com.** O console já cobre.
- **Limite conhecido:** DNS rebinding contra o controle de egress. Fechar isso é papel de um firewall
  de egress no ambiente, não do serviço.
