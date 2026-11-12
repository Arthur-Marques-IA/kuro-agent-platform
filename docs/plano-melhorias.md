# Plano de melhorias

Plano de execução para agentes. Cada pacote é independente o bastante para virar uma branch e um
PR. Leia "Regras para quem executa" antes de pegar um pacote.

> Escrito em 2026-11-11, a partir de uma revisão do código (branch `fix/correcao-doc-texto`), do
> console rodando localmente e de sessões reais de produção lidas pelo MCP. Os caminhos de arquivo
> são relativos à raiz do repositório.
>
> **Estado em 2026-11-12: os 28 pacotes foram implementados e integrados.** O que cada um mudou
> para quem integra ou opera está em `docs/notas-de-versao.md` (seção `## Não lançada`), e o
> roteiro para atualizar uma instalação em operação em `docs/migracao.md`. Este arquivo fica como
> registro do plano; o que ficou em aberto está em "Pendências depois da execução", no fim.

## Regras para quem executa

- Um pacote por branch e por PR. Não misture pacotes.
- Antes de editar o frontend, leia `frontend/AGENTS.md`: o Next desta versão difere do habitual.
- Mudou algo que quem integra ou opera percebe (campo, status, default, variável de ambiente)?
  Registre em `docs/notas-de-versao.md`, seção `## Não lançada`, no mesmo commit.
- Backend: `uv run pytest -q` passa. Frontend: `npm run lint`, `npm test` e `npm run build` passam.
- Mudança visível no console: verifique no navegador com o frontend rodando do código-fonte
  (`npm --prefix frontend run dev`), não pela imagem Docker, que pode estar defasada.
- Não quebre o contrato atual do `/chat` e do `/analyze`: todo campo novo é opcional.
- "Pronto" de cada pacote é a lista de critérios de aceite dele, inteira.

## Ordem e dependências

| Fase | Pacotes | Observação |
|---|---|---|
| 0 | S1, S2, S3, U0 | Correções pequenas e independentes. Podem rodar em paralelo. |
| 1 | C1 | Contrato de entrada estruturada. Bloqueia C2, C3, C4, T1. |
| 2 | C2, C3, C4, T1 | Em paralelo depois de C1 (C3 e C4 dependem de C2). |
| 2 | U1 a U8 | Independentes de C1; em paralelo entre si, respeitando os arquivos de cada um. |
| 3 | Q1, Q2, Q3, A1 a A4, S4 a S6, D1 | Qualidade, arquitetura e operação. Sem ordem obrigatória. |

Conflito de arquivos a evitar: C3 e U2 mexem em `session-detail-view.tsx` e `runs-table.tsx`
(faça C3 primeiro); U3 e U4 mexem em `agent-form.tsx` (faça em sequência).

---

## Trilha C: contexto e interação em campos separados

**Problema.** A integração do Regente concatena tudo no campo `message`: configuração do sistema,
dados do aluno, dívida, anotações internas, linha temporal, histórico com marcações `[ALUNO]` e
`[ATENDENTE]` e a fala nova. O console e a transcrição tentam desfazer isso com marcadores de
texto fixos, duplicados em `src/agent_service/observability/transcript.py` e
`frontend/src/lib/transcript.ts`. O resultado: a bolha "do usuário" traz várias falas de autores
diferentes, a resposta anterior do agente reaparece na bolha seguinte, o selo "mudou" compara
texto bruto, e a decisão do agente (tool) não aparece na conversa.

**Decisão.** Separar por campos na entrada, sem regex e sem marcadores. O servidor monta o texto
que vai ao modelo e guarda as partes estruturadas; as telas leem as partes.

### C1. Entrada estruturada no `/chat` e no `/chat/stream`

Arquivos: `src/agent_service/api/routes.py`, `src/agent_service/observability/tracing.py`,
`src/agent_service/observability/run_store.py`, nova migração em
`src/agent_service/migrations/versions/`, `src/agent_service/api/integration_routes.py`.

Campos novos em `ChatRequest`, todos opcionais:

```json
{
  "message": "boa tarde, me envia novamente o comprovante",
  "events": [
    {"role": "agent_sent", "content": "Maria, recebi seus dados...", "at": "2026-11-11T12:36:10Z"},
    {"role": "contact", "content": "Para finalizar o diploma. Obrigada.", "at": "2026-11-11T12:37:01Z"},
    {"role": "contact", "content": "boa tarde, me envia novamente o comprovante", "at": "2026-11-11T12:37:15Z"}
  ],
  "context": [
    {"key": "configuracao", "title": "Configuração desta conversa", "content": "EFETIVAÇÃO: LIGADA..."},
    {"key": "dados_aluno", "title": "Dados do aluno", "content": "Nome: ...\nCurso: ..."},
    {"key": "divida", "title": "Contexto da dívida", "content": "SEM parcela vencida..."},
    {"key": "anotacoes", "title": "Anotações internas", "content": "..."},
    {"key": "temporal", "title": "Contexto temporal", "content": "Janela 24h ABERTA", "volatile": true}
  ]
}
```

- `events`: o que aconteceu desde a última resposta do agente, em ordem. `role` é um de
  `contact`, `human_agent`, `agent_sent` (o que a instituição de fato enviou em nome do agente) e
  `system`. Cada item aceita `content`, `at`, `kind` (`text` ou `document`) e `attachments`.
- `context`: blocos nomeados. `key` é um identificador estável (o mesmo a cada turno), `title` é o
  rótulo para humanos, `content` é texto. `volatile: true` marca bloco que muda todo turno e não
  conta como "contexto mudou".
- `message` passa a ser opcional quando `events` traz ao menos um item `contact`. Com os dois, a
  `message` é a fala nova e `events` é o que veio antes dela. Sem nenhum dos dois: 422.
- O servidor monta o texto do modelo com um gabarito fixo e determinístico (blocos de contexto com
  o título, depois os eventos rotulados pelo papel, depois a fala). O gabarito fica num módulo só
  (`src/agent_service/agents/structured_input.py`), com teste de snapshot.
- Limites: no máximo 30 blocos e 200 eventos; a soma dos textos respeita `MAX_INPUT_CHARS`.
- Persistência: colunas novas em `runs`: `input_events` (JSON), `input_context` (JSON com o hash
  de cada bloco) e `input_message` (a fala pura). A coluna `message` continua recebendo o texto
  montado, para os filtros e buscas existentes.
- `dependencies` não muda de papel: continua sendo chave e valor tipado para tools e validação.
  `context` é texto para o modelo ler. Documente a diferença.

Aceite:
- Chamada só com `message` se comporta exatamente como hoje (teste de regressão).
- Chamada com `events` e `context` grava as três colunas e o texto montado bate com o snapshot.
- `agents integrate` e os OpenAPI de integração descrevem os campos novos.
- `docs/integracao.md` ganha a seção "Entrada estruturada", com o antes e o depois de uma chamada
  do tipo do Regente, e `docs/notas-de-versao.md` registra a mudança.

### C2. Linha do tempo da sessão, servida pronta

Arquivos: `src/agent_service/api/observability_routes.py`,
`src/agent_service/observability/transcript.py`, `src/agent_service/cli/sessions.py`,
`src/agent_service/mcp_server.py`.

`GET /observability/sessions/{session_id}/timeline` devolve, por turno:

```json
{
  "run_id": "...", "at": "...", "agent_type": "r6", "agent_version": 10, "status": "success",
  "structured": true,
  "events": [{"role": "contact", "content": "...", "at": "..."}],
  "message": "fala pura",
  "context": [{"key": "divida", "title": "Contexto da dívida", "content": "...", "changed": true}],
  "steps": [{"tool": "regente_r6_decidir", "args": {"acao": "transferir", "departamento": "Secretaria"},
             "ok": true, "result_preview": "...", "side_effect": false}],
  "sources": [],
  "output": "resposta do agente",
  "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0, "latency_ms": 0}
}
```

- `changed` é calculado por bloco, comparando o hash com o do mesmo `key` no turno anterior.
  Blocos `volatile` nunca contam.
- `steps` sai dos spans de tool já gravados em `run_spans`.
- Run antigo (sem as colunas novas) vem com `structured: false`, `events` vazio e a `message`
  original inteira em `raw_input`. Sem tentativa de recorte.
- `kuro sessions show` e a tool MCP `session_show` passam a usar este endpoint.
- Remova `FALA_MARKERS`, `HISTORY_MARKER`, `TEMPORAL_PREFIX`, `split_input` e `clean_context` de
  `transcript.py` e os testes deles.

Aceite: testes cobrindo turno estruturado, turno antigo, bloco que mudou, bloco volátil e tool
que falhou. Nenhum marcador de texto resta no backend (`grep -rn "FALA DO CONTATO" src` vazio).

### C3. Console: nova visualização da conversa

Arquivos: `frontend/src/components/observability/session-detail-view.tsx`,
`frontend/src/components/observability/runs-table.tsx`,
`frontend/src/components/observability/sessions-table.tsx`,
`frontend/src/components/observability/run-trace-view.tsx`, `frontend/src/lib/transcript.ts`,
nova rota BFF em `frontend/src/app/api/sessions/[id]/timeline/`.

- A tela de sessão consome a timeline do C2. Apague `splitInput` e os marcadores de
  `frontend/src/lib/transcript.ts`.
- Uma bolha por evento, com autor e horário: contato à direita, agente à esquerda, atendente
  humano em uma terceira cor com rótulo, sistema como linha discreta centralizada.
- `agent_sent` igual à resposta anterior do agente não é desenhado de novo; se saiu diferente do
  que o agente escreveu, aparece com o selo "enviado diferente".
- Entre a fala e a resposta, uma linha por tool: nome legível, argumentos principais e resultado,
  por exemplo `decidiu: transferir → Secretaria`. Tool com efeito colateral e tool que falhou têm
  selo próprio. Clique expande argumentos e resultado completos.
- Contexto em painel lateral fixo (em telas largas) ou gaveta (em telas estreitas): um cartão por
  bloco, com o `title`; o painel acompanha o turno em foco e destaca os blocos com
  `changed: true`. Cada bloco tem "ver diferença" contra o turno anterior.
- Evento `kind: document` vem recolhido, com o nome do arquivo, e renderiza markdown ao abrir.
- Turno `structured: false` aparece como cartão "entrada não estruturada", recolhido, com o texto
  original e o aviso de que a integração ainda manda tudo em `message`.
- Cabeçalho da sessão: nome do contato quando houver bloco ou dependência que o traga, agente,
  canal, início, duração, número de turnos, custo total. O id vira detalhe copiável.
- Ordem da página: conversa e contexto primeiro; memória do agente, gráficos e tabela de
  execuções em abas ou recolhidos.
- Tabelas de execuções e de sessões: a coluna de mensagem usa `input_message` (a fala pura) e a
  de sessões ganha prévia da última fala e nome do contato.
- Tela de execução: título com a fala pura; entrada renderizada como texto com quebras de linha e
  blocos, não como JSON escapado; migalha coerente com a origem (Logs, não Agentes).

Aceite: com uma sessão estruturada de três turnos (contato, atendente humano, tool de decisão,
contexto que muda no segundo turno), cada item acima é visível sem abrir o trace. Captura de tela
no PR em 1280 px e em 375 px.

### C4. Playground com a mesma entrada estruturada

Arquivos: `frontend/src/components/chat/`, `frontend/src/components/chat/chat-inspector.tsx`.

- O painel "Contexto" do Playground ganha uma aba "Blocos de contexto" (lista de `key`, `title`,
  `content`) ao lado de `dependencies`, e envia `context` na chamada.
- Um modo "simular atendente" permite inserir um evento `human_agent` antes da próxima fala.
- "Código" (snippet de integração) mostra a chamada com os campos estruturados quando usados.

Aceite: uma conversa criada no Playground com blocos aparece na tela de sessão do C3 igual a uma
vinda de integração.

### T1. TUI: fala pura, sessão e expansão

Arquivo: `src/agent_service/tui/app.py`, `tests/test_tui.py`, `docs/tui.md`.

- Coluna "mensagem" da aba Ao vivo e caixa "mensagem" do trace usam `input_message`; a caixa tem
  altura máxima e rolagem própria, para não empurrar os spans.
- Tecla `c` no trace abre o contexto em blocos; tecla `s` abre a sessão do run (timeline do C2,
  um turno por bloco, com as linhas de tool).
- `enter` num span alterna entre a prévia e o conteúdo completo de entrada e saída; `y` copia o
  `run_id`.
- Ajuda (`?`) e `docs/tui.md` atualizadas. Regerar as capturas de `docs/telas/` com um run de
  entrada longa, não só com dados curtos de demonstração.

### Fora deste repositório

A integração do Regente precisa passar a mandar `events` e `context`. Entregue junto do C1 um
guia de migração em `docs/integracao.md` com o mapeamento de cada trecho do texto atual para o
campo novo. As 3.771 sessões já gravadas ficam como "entrada não estruturada"; não há retrocarga.

---

## Trilha U: usabilidade do console

Achados da revisão, com o frontend do código atual contra o serviço local.

### U0. Imagem do console defasada e sem versão visível

A imagem `microservice_agent-frontend` local era de 14/10 e o serviço de 16/10, com código de
29/10 no repositório. Com o frontend novo contra o serviço antigo, `/knowledge` quebra com
"Cannot read properties of undefined (reading 'search_mode')" e a tela de erro mostra a mensagem
crua do JavaScript.

- Mostrar no rodapé da barra lateral a versão do console (hash do build) e a do serviço
  (`/health`), e um aviso quando forem incompatíveis.
- `frontend/src/app/(workspace)/error.tsx`: mensagem em português para o operador, com o detalhe
  técnico recolhido e botão de copiar.
- Leitura defensiva dos campos que o serviço pode não devolver (comece por `search_mode` em
  `frontend/src/components/knowledge/`).
- `docs/operacao.md`: passo de atualização que reconstrói as duas imagens juntas.

### U1. Camadas sobrepostas

- O mascote (`frontend/src/components/brand/kuro-mascot.tsx`, `fixed bottom-3`) cobre o rótulo
  "Alterações não salvas" da barra de salvar do agente e o botão de anexo do Playground quando o
  painel de detalhes está aberto. Reserve espaço para ele ou tire-o dessas telas.
- O menu de Preferências continua aberto por baixo da busca global (`Ctrl+K`). Abrir um
  sobreposto fecha o outro; `Esc` fecha o de cima.

### U2. Listas de Logs

Arquivos: `logs-view.tsx`, `logs-filters.tsx`, `runs-table.tsx`, `sessions-table.tsx`.

- Em 375 px as tabelas viram rolagem horizontal com ids cortados (`cli-b64…`). Abaixo de `md`,
  troque por cartões: fala, agente, status, horário.
- Estado vazio do Panorama e das Execuções deve oferecer o botão "Incluir testes" ali mesmo,
  quando só existirem execuções de teste.
- Garanta que período, agente, status e busca ficam na URL (link compartilhável, botão voltar).
- Exportar a lista filtrada em CSV.
- Atualização automática opcional (a TUI tem, o console só tem o botão "Atualizar").

### U3. Lista e cabeçalho de agentes

Arquivos: `frontend/src/components/agents/`, `frontend/src/lib/format.ts`.

- `initials()` gera "R—" para "R6 — Agente…" e "R8 — …": ignore palavras sem letra ou número e
  use o slug como desempate.
- Nomes cortados no cartão ("Auditor de Atendimentos Hum…", "R8 — Simulação de Vendas (ch…") e
  datas cortadas ("07 de out. d…"): duas linhas para o nome, data relativa curta.
- Produção e draft aparecem como dois cartões iguais ("Demo shadow" duas vezes). Agrupe o par,
  com selo "draft" e atalho para comparar e promover.
- Selos sem rótulo ("Desligada", "Agêntica"): escreva "Memória: desligada".
- Filtro por tipo (conversacional, analista, procedural) e ordenação por uso recente.
- Em 375 px o título vira "R6 — Agente d…" e a migalha "A…": deixe o título quebrar linha.

### U4. Formulário do agente

Arquivo: `frontend/src/components/agents/agent-form.tsx` (1.151 linhas).

- Ao abrir `r6` sem editar nada, a barra já mostra "Alterações não salvas" e "será salvo como
  v2". Observado com o serviço local defasado; reproduza com o serviço atual. Se persistir, o
  defeito está em `buildUpdate` normalizando campo que o serviço não devolveu; se não, cubra o
  caso com a leitura defensiva do U0.
- Página única muito longa: índice lateral fixo das seções (Identidade, Tipo, Prompt, Modelo,
  Memória, Contexto, Tools) com indicação de qual tem alteração ou erro.
- Prompt de 44 instruções e 20 mil caracteres num `textarea` pequeno: editor em tela cheia, com
  busca e numeração, e o diff contra a versão atual antes de salvar.
- "Carregando modelos…" sem prazo: mostrar o modelo salvo de imediato e a lista quando chegar.
- Divida o arquivo em um componente por seção enquanto faz isso.

### U5. Chaves de API e modelos

- A tela diz "Nenhuma chave cadastrada ainda" enquanto o serviço responde pela `GOOGLE_API_KEY`
  do ambiente. Mostre a credencial de ambiente como linha somente leitura.
- O menu diz "Chaves de API", a rota é `/models` e a API fala em `model-credentials`. Unifique o
  nome na interface e explique que não são as chaves de acesso ao Kuro.

### U6. Análise e tela de execução

- `/analyze`: "Campos que este agente devolve" mostra o `response_schema` como JSON cru. Use a
  tabela de campos (nome, tipo, obrigatório, descrição) e deixe o JSON atrás de um botão.
- Resultado de analista: campos ao lado dos valores, com os de `enum` destacados.

### U7. Acessibilidade e teclado

- Só há 6 regiões `aria-live`/`role=status` no console: anuncie fim de resposta em streaming,
  salvamento e erro.
- Em 375 px, 14 de 32 alvos clicáveis da tela de sessão têm menos de 32 px. Mínimo de 40 px para
  toque.
- Meça o contraste do texto secundário de 11 e 12 px sobre o fundo escuro (meta: 4,5:1) e ajuste
  o token; confira o tema claro em todas as telas, que não foi avaliado nesta revisão.
- Atalhos: `j`/`k` nas tabelas e `Esc` para voltar, como na TUI; documente em `docs/console.md`.

### U8. Busca global e navegação

- A busca (`Ctrl+K`) cobre agentes, páginas e as conversas do próprio navegador. Inclua colar um
  `run_id` ou `session_id` para ir direto, e tools e collections pelo nome.
- A barra lateral "Conversas" só lista as do Playground deste navegador; diga isso no título ou
  ofereça o atalho "todas as sessões".

---

## Trilha S: segurança

### S1. Egress: faixa `100.64.0.0/10` passa

`_blocked_reason` em `src/agent_service/tools/egress.py` testa `is_private`, falso para CGNAT.
`100.100.100.200` (metadata da Alibaba Cloud) e `100.64.0.1` saem como liberados. Troque a
cadeia de testes por `not ip.is_global`, mantendo as mensagens por motivo. Aceite: testes novos
em `tests/test_tool_egress.py` para as duas faixas e para um IP público.

### S2. Validador de tool Python aceita fuga por atributo privado

Em `src/agent_service/tools/python_tool.py`, `validate_python_config` aceita
`random._os.getcwd()`, `collections._sys.modules[...]` e `from random import _os as o`. Barre
atributo iniciado por `_`, confira cada nome de `from x import y` contra uma lista por módulo e
entregue cópias dos módulos só com os nomes públicos permitidos. Atualize o docstring: continua
não sendo sandbox. Aceite: os três casos viram teste e são recusados.

### S3. Console sem login com chave admin

O BFF injeta a chave admin (`frontend/src/lib/api.ts`) e `docker/Caddyfile` publica o console sem
barreira. Curto prazo: `basic_auth` no bloco do console, com usuário e hash vindos do `.env`, e o
profile `tls` se recusando a subir sem eles. Documente em `docs/operacao.md` e `docs/console.md`.

### S4. Segredos de tools cifrados em repouso

`auth` e `headers` das tools `api` ficam em texto plano no banco e só são mascarados na leitura
(`src/agent_service/api/tools_routes.py`). Cifre com o Fernet de `src/agent_service/models/crypto.py`,
com migração que cifra o que já existe e leitura tolerante durante a transição.

### S5. Subir fechado por padrão

Sem `ADMIN_API_KEY` o serviço sobe aberto. Passe a falhar a subida, salvo `ALLOW_OPEN=1`
explícito (o compose de desenvolvimento define). Nota de versão obrigatória: muda o default.

### S6. Retenção e dados pessoais

Mensagens, `dependencies` e anexos ficam nos traces sem prazo. Adicione `RUN_RETENTION_DAYS`
(job na fila existente que apaga ou anonimiza runs antigos), lista de chaves a mascarar na
gravação (`TRACE_REDACT_KEYS`, ex.: `cpf`) e a seção correspondente em `docs/operacao.md`.

---

## Trilha A: arquitetura

- **A1. Subida sem efeito colateral no import.** `src/agent_service/main.py` roda migração e
  seeds ao importar. Mova para o lifespan, com `pg_advisory_lock` em volta da migração.
- **A2. Menos consultas por requisição.** `get_agent_with_definition` em
  `src/agent_service/agents/registry.py` lê definição, nota de feedback e uma linha por tool a
  cada chamada. Uma consulta só com os `updated_at`. Documente em `docs/operacao.md` que cache,
  semáforo e fila são por processo.
- **A3. Gravação de trace com fila própria.** `_submit` em
  `src/agent_service/observability/run_store.py` usa o executor padrão. Use um executor dedicado
  com fila limitada, métrica de descarte e esvaziamento no encerramento.
- **A4. Limpezas.** `EmbedderError` vira classe base (hoje é uma tupla, daí o `*EmbedderError`
  em `routes.py`); camada fina entre as rotas e os tipos do Agno; `description` real no
  `pyproject.toml`.

## Trilha Q: testes e CI

- **Q1. Postgres no CI.** `tests/conftest.py` usa SQLite. Adicione um job com
  `pgvector/pgvector:pg16` rodando migrações, fila de jobs (`SKIP LOCKED`, `LISTEN/NOTIFY`) e
  busca híbrida, com marcador `postgres` nos testes que precisam dele.
- **Q2. Lint e tipos no backend.** `ruff check` e `ruff format --check` em
  `.github/workflows/ci.yml`; depois, checagem de tipos em modo gradual.
- **Q3. Testes do console.** Smoke com Playwright (criar agente, conversar, abrir sessão e
  trace) e geração de `frontend/src/lib/types.ts` a partir do OpenAPI do serviço.

## Trilha D: deploy

- **D1. Imagem.** `Dockerfile` em dois estágios, sem `build-essential` na imagem final e com
  usuário não-root. Senha do Postgres por variável em `docker-compose.yml`. Limite de requisições
  por chave no `/chat` e no `/analyze`.

---

## Decisões que são do dono do projeto

1. Retrocarga das sessões antigas: o plano não faz (ficam como "entrada não estruturada").
2. `message` continuar obrigatória quando há `events`: o plano a torna opcional.
3. S5 muda um default e pode quebrar instalações existentes: decidir em qual versão entra.
4. Login de verdade no console (usuários e papéis) em vez do `basic_auth` do S3: fica para o item
   de RBAC do ROADMAP.

---

## Pendências depois da execução

O que os pacotes deixaram em aberto ou decidiram diferente do texto acima.

**Decididas diferente do plano**

- **S3.** A recusa sem o login do console acontece quando o container do Caddy sobe
  (`docker/caddy-tls.sh`), não no `docker compose up`: uma variável obrigatória no compose quebraria
  qualquer comando, mesmo sem o profile `tls`.
- **S4.** Sem `CREDENTIALS_ENCRYPTION_KEY`, a tool é gravada em texto plano, com aviso no log e
  `secrets_at_rest: "plaintext"` na leitura, em vez de 503.
- **S5.** O critério é "nenhuma chave configurada" (só a `RUNTIME_API_KEY` já fecha o serviço), e
  o compose liga `ALLOW_OPEN` por padrão, com a porta só no loopback.
- **C1.** Agente procedural recusa `events` e `context` com 422.
- **Q2.** As regras do `ruff` ficaram no conjunto clássico (`E4`, `E7`, `E9`, `F`), com linha de
  120; ordenação de imports e modernização não foram ligadas.

**Em aberto**

- **S1.** Endereços NAT64 (`64:ff9b::/96`) ainda passam pela trava de egress.
- **S4.** Parâmetros `secret` das tools builtin e headers com `source="const"` continuam em texto
  plano no banco; não há rotação com duas chaves.
- **S6.** A retenção cobre só o trace store. Sessões do Agno, memórias e `procedure_runs` não são
  apagados; no modo `anonymize`, `user_id`, `session_id` e a metadata de correlação ficam.
- **C3.** O nome do contato no cabeçalho da sessão lê a linha `Nome:` de um bloco `dados_aluno`,
  `dados_contato` ou `dados_cliente` como último recurso. Num teste do Playground com esse bloco o
  nome não apareceu; a causa não foi investigada.
- **U0.** O aviso de versões incompatíveis só passa a valer quando a `version` do `pyproject.toml`
  subir (o próximo lançamento) e `MIN_SERVICE_VERSION` acompanhar.
- **U7.** Fora da tela de sessão e dos botões compartilhados, ainda há alvos de toque abaixo de
  40 px (`/agents/new`, `/tools`). Nada foi testado com leitor de tela real.
- **Q1.** A suíte inteira contra Postgres não está no CI: um teste de fila falha por tempo de
  espera com o banco na rede.
- **Q3.** O `/openapi.json` servido descreve os handlers do Agno em `GET /agents` e `GET /health`,
  não os do Kuro. Oito rotas não têm `response_model` e saem sem tipo no console.
- **Horários.** Em SQLite alguns horários aparecem sem fuso na tela de sessão; não foi conferido
  se acontece em Postgres.
- **Retrocarga.** As sessões gravadas antes da entrada estruturada ficam como "entrada não
  estruturada" (decisão 1 da lista acima).
