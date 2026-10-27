# Notas de versão

O que muda para quem **integra** um sistema ao Kuro ou **opera** o serviço, versão a versão.
Aqui não entram as mudanças internas; essas ficam no histórico do git.

## Como ler

- **Versão no ar:** `GET /health` devolve `version` (ex.: `"0.2.0"`), e `kuro health` mostra o mesmo.
  Compare com as versões abaixo para saber o que você ainda não pegou.
- **Numeração:** `MAIOR.MENOR.CORREÇÃO`. Enquanto a versão começar com `0.`, uma versão MENOR nova
  (`0.1` → `0.2`) **pode** exigir ação de quem integra. Por isso cada versão traz primeiro a seção
  **Ação necessária**. Uma versão de CORREÇÃO (`0.2.0` → `0.2.1`) nunca exige ação.
- **Pulando versões:** leia a seção **Ação necessária** de cada versão entre a sua e a nova, da
  mais antiga para a mais nova.
- **Seções de cada versão:**
  - **Ação necessária**: o que pode quebrar ou mudar de comportamento se você não fizer nada.
  - **Novo**: o que passou a existir. É opcional usar.
  - **Corrigido**: bugs que deixaram de acontecer.
  - **Como atualizar**: os passos de quem opera o serviço.

---

## Não lançada

### Ação necessária

- **Todo documento passa a ser cortado pela regra dos sites.** Texto colado, arquivo enviado (PDF,
  DOCX, PPTX, XLSX, TXT, MD) e a correção de documento deixam o corte do Agno (até 5.000 caracteres,
  sem olhar o conteúdo) e usam o mesmo de páginas de site: trechos de até 1.500 caracteres, cortados
  entre parágrafos, sem separar título de conteúdo nem pergunta de resposta, e cada trecho com
  `section`. CSV continua uma linha por trecho. Os trechos ficam menores: se um agente passar a
  receber pouco contexto, suba o `max_results` (k) da collection. Os documentos já indexados não
  mudam até serem recortados (`POST /collections/{nome}/reindex`, `kuro collections reindex`).

- **Fila no limite de execuções.** Acima de `MAX_CONCURRENT_RUNS`, a chamada **espera**
  na fila (até `QUEUE_MAX_WAIT_SECONDS`, 30 s) em vez de receber 503 na hora, e só então o run começa
  a contar o `timeout_seconds`. O timeout do seu cliente precisa cobrir as duas esperas
  (`QUEUE_MAX_WAIT_SECONDS + timeout_seconds + 5`); um cliente com `timeout_seconds + 5`, como o
  [guia de integração](integracao.md#4-erros-e-fallback) recomendava, pode desistir com o run ainda na fila.
  Para o comportamento antigo (503 na hora), use `QUEUE_MAX_WAIT_SECONDS=0`.

### Novo

- **`kuro` no host do servidor sem exportar a chave.** Sem `KURO_API_KEY`, a CLI, o `kuro dash` e o
  `kuro-mcp` usam a `ADMIN_API_KEY` do `.env` da pasta atual (só dela, sem subir pastas), e só com o
  serviço em loopback (`localhost`/`127.0.0.1`): esse `.env` é o do serviço da máquina, e a chave dele
  não vai para outro endereço. Numa rotação (`nova,antiga`) vai a nova. A `KURO_API_KEY` explícita
  sempre vence. Antes, `uv run --extra tui kuro dash` no host dava 401, porque só dentro do container
  a chave vem configurada (e a imagem não traz o extra `tui`). Um 401 na CLI e no painel agora diz as
  três saídas: `KURO_API_KEY`, rodar na pasta do repositório ou `docker compose exec agent-service kuro`.
- **Ciclo de correção do RAG: revisão de documento e marcação resolvida.** Corrigir um documento não
  troca mais o `content_id`: `PUT /collections/{nome}/documents/{content_id}` (texto) ou
  `PUT .../{content_id}/file` (arquivo) reindexa no mesmo id e grava a revisão seguinte
  (`GET .../revisions[/{n}]`, com o texto quando é texto). Página de site dá 409 (corrija na fonte; o
  sync grava uma revisão a cada mudança). Cada trecho leva `document_revision` na metadata, que
  aparece em `metadata.references`, no `GET .../search` e no trace. A marcação de trecho
  (`chunk_feedback`) ganha `document_revision`, `status` (`open`, `resolved`, `dismissed`),
  `resolved_in_revision`, `resolved_by`, `resolved_at`, `resolution_note`, `current_revision` e
  `stale`. A edição resolve as marcações abertas do documento (ou as de `resolves`);
  `POST /observability/chunk-feedback/{id}/status` dispensa, resolve ou reabre uma; remover o
  documento fecha as abertas. `POST .../{content_id}/retest` busca de novo as perguntas marcadas,
  sem modelo. CLI: `collections edit-doc|revisions|retest`, `runs chunk-dismiss|chunk-resolve|chunk-reopen`,
  `runs chunk-marks --status --content-id`, `runs chunk-flagged --status`; MCP: `collection_edit_doc`,
  `collection_doc_revisions`, `collection_retest`, `chunk_feedback_list`, `chunk_feedback_status`.
  Console: botão **Corrigir** na tela Conhecimento, com as marcações a resolver e o "Testar de novo".
  Ver [corrigir o documento](conceitos.md#corrigir-o-documento-e-fechar-a-marcação).
- **Recortar documentos já indexados.** `POST /collections/{nome}/reindex[?content_id=]` corta de
  novo pela regra atual, a partir dos trechos indexados: mesmo `content_id`, mesmas páginas, uma
  revisão nova ("recortado") em cada documento, embeddings gerados de novo; páginas de site ficam de
  fora. Devolve, por documento, `status` e os trechos antes e depois. CLI `collections reindex`, MCP
  `collection_reindex`.
- **Texto do documento para corrigir.** A revisão de um arquivo passa a guardar o texto extraído; sem
  texto guardado, `GET .../revisions/{n}` (a atual) remonta o texto dos trechos indexados
  (`text_source: "chunks"`). O "Corrigir" do console abre preenchido e também aceita o arquivo
  corrigido (`PUT .../{content_id}/file`).
- **Aba RAG nos Logs.** `/logs/rag` lista as execuções que buscaram na base, com três recortes e a
  contagem de cada um: buscaram na base, busca sem trecho nenhum (a base não cobre a pergunta) e
  trecho marcado errado ainda aberto, filtráveis por collection. API: `rag=searched|empty|flagged`
  em `GET /observability/runs` e `/stats` (que também aceita `knowledge_collection`).

- **Investigar runs que não são conversa.** Três adições aditivas à observabilidade, sem migração:
  - O trace de um turno de agente procedural (`GET /observability/runs/{id}/trace`) traz
    `procedure_state`: o `state` como o turno terminou (etapa, `collected`, `missing`, `invalid`,
    `done`), o mesmo que o `/chat` devolveu. Fica gravado num span `procedimento · estado` (tipo
    `CHAIN`, que não conta como tool); runs anteriores vêm com `procedure_state: null`.
  - `GET /observability/runs/{id}/related` (admin): os runs, de qualquer agente, que têm o mesmo
    valor de uma chave de correlação na `metadata`, do mais antigo ao mais novo — ex.: triagem →
    redação → consulta à base num ciclo do moderador. As chaves vêm de `CORRELATION_METADATA_KEYS`
    (separadas por vírgula, padrão `ciclo_id`; vazio desliga) e aparecem em
    `GET /observability/config` (`correlation_keys`).
  - `POST /observability/runs/{id}/feedback` (admin) `{value, comment?, user_id?, reference?}`:
    grava o score `feedback` e, com `value=0` num agente `analysis`, a saída correta como
    referência do run (`source="feedback"`) — ela sai em `GET /observability/export` /
    `kuro runs export` como caso do `kuro eval`. Não vira regra no prompt: `/agents/{t}/feedback`
    continua 422 em `kind="analysis"`. `reference` com `value=1` dá 422.

  MCP: `run_related` e `run_feedback`. Console: a tela do run mostra a entrada e a saída
  estruturada de um `/analyze` (árvore dos campos), o estado do turno procedural, a linha do tempo
  da correlação e "Avaliar esta execução" (👎 com a saída correta no analista; "ensinar" no
  conversacional e no procedural); na sessão, os runs de analista viram cartões recolhíveis e o
  procedural ganha o "ensinar". Ver [console](console.md#a-tela-da-execução).
- **Fila de jobs no Postgres.** A sincronização de sites passou a rodar como job de uma fila no
  próprio banco (tabela `jobs`, sem broker novo): várias réplicas dividem o trabalho sem repetir
  um job, um processo que cai no meio devolve o job à fila quando o lease vence, a falha é
  repetida com backoff exponencial, e o `refresh` agendado é um job com `run_after` no futuro. A
  API de sites não muda (202 no `POST .../sources` e no `.../sync`, 409 com um sync já na fila ou
  rodando; um sync só agendado é puxado para agora). Novo para o operador: `GET /jobs`,
  `GET /jobs/{id}`, `POST /jobs/{id}/retry|cancel` (admin) e `kuro jobs list|show|retry|cancel`.
  Ver [fila de jobs](operacao.md#fila-de-jobs).
- **Busca híbrida e configuração de busca versionada por collection.** `POST /collections` aceita
  `search_mode` (`vector`, o padrão e o comportamento de antes, ou `hybrid`: vetor + full-text do
  Postgres), `language` (dicionário do full-text, `portuguese` por padrão; fixo depois da criação),
  `max_results` (k, padrão 10) e `vector_weight` (0–1); `PUT /collections/{nome}` muda os três
  últimos (`language` diferente do atual dá 422). `reranker` existe só como gancho: qualquer valor
  dá 422 por enquanto. A collection devolve `search`, `version` e `config_hash`; cada mudança vira
  uma versão em `GET /collections/{nome}/versions[/{versão}]`. O run grava `knowledge_collection`,
  `collection_version` e `collection_config_hash` (em `/observability/runs`, filtráveis por
  `knowledge_collection` e `collection_version`), e cada busca em `metadata.references` traz
  `version`, `config_hash`, `search_mode` e `score_kind` (`similarity`, `hybrid` ou `rerank`).
  `GET /collections/{nome}/search` sem `limit` passa a usar o k da collection (antes, 5) e traz
  `score_kind`. CLI: `kuro collections create --mode/--language/--k/--vector-weight`,
  `collections set search_mode=...`, `collections versions`, `runs list --collection
  --collection-version`; MCP: os mesmos campos em `collection_create`/`collection_set`,
  `collection_versions` e os filtros em `runs_list`. Console: painel "Como a busca funciona" na
  tela de Conhecimento e a versão da base no trace. Collections existentes ficam como estavam
  (vetorial, k=10) e ganham a v1. Ver [como a collection busca](conceitos.md#como-a-collection-busca).
- **Feedback por trecho recuperado pelo RAG.** No trace de um run, cada cartão de "Trechos
  consultados" ganha três botões discretos: relevante, irrelevante e errado/desatualizado, com
  comentário opcional. Isso fica numa tabela própria (`chunk_feedback`, migração `0013`), com a
  pergunta, a collection, o `content_id`, o `chunk_id`, quem marcou e quando. O Kuro só registra
  e aponta: nenhum documento é escondido ou despublicado. A tela Conhecimento mostra os
  documentos mais marcados como errados, com link para os runs.
  API: `POST`/`GET /observability/chunk-feedback`, `DELETE .../{id}`, `GET .../flagged` e
  `GET .../export` (gabarito de RAG: pergunta → trechos relevantes). CLI: `kuro runs
  chunk-feedback|chunk-unmark|chunk-marks|chunk-flagged|chunk-export`. MCP: `run_chunk_feedback`,
  `chunk_feedback_flagged` e `chunk_feedback_export`.
  Ver [conceitos](conceitos.md#feedback-por-trecho).

- **A coleta de sites indexa os arquivos linkados.** PDF, DOCX, PPTX e XLSX linkados nas páginas,
  do mesmo escopo da fonte e dentro de `max_pages` (depois das páginas), entram na collection lidos
  pelos leitores do Agno, sem o agente formatador; cada trecho de PDF leva `page` (e o de PPTX, o
  slide) e `source_url`. Opção `include_files` em `POST /collections/{n}/sources` e no `PATCH`:
  **padrão `true` nas fontes novas**; uma fonte já cadastrada continua sem arquivos até
  `include_files=true`. `GET .../sources/{id}` passa a trazer `kind` (`page` | `file`) em cada página.
  CLI `kuro collections add-site --files/--no-files`, MCP `collection_add_site(include_files=...)`, e
  no console uma caixa na aba Site e o selo "arquivo" no painel Sites. Nova variável
  `WEB_SOURCE_MAX_FILE_MB` (20). Ver [sites como fonte](conceitos.md#sites-como-fonte).
- **Filtros dos Logs mais soltos, e KPIs que levam às execuções.** Panorama, Sessões e Execuções
  aceitam vários agentes (com busca, mostrando o `agent_type`), vários status e um intervalo de
  datas livre além dos atalhos; tudo fica na URL, e os links antigos (`agent=`, `status=`, `period=`)
  continuam valendo. Cada KPI do Panorama abre as Execuções com o recorte que explica o número
  (Falharam = erro + interrompida; Custo e Latência p95 = as mais caras/lentas primeiro). Na API:
  `agent_type` e `status` passam a ser repetíveis em `GET /observability/runs`, `/sessions` e
  `/stats` (`agent_type` também em `/overview`), e `/runs` ganha `sort=recent|cost|latency` (sem os
  runs sem custo/latência; 501 com `TRACE_STORE_BACKEND=langfuse`). Um valor só funciona como antes.
  Ver [console](console.md#logs-panorama-sessões-e-execuções).
- **O Kuro no canto do console.** O corvo do `kuro dash` aparece no canto inferior esquerdo do
  conteúdo (telas a partir de 1280 px) e reage às execuções como na TUI: asas batendo com execução
  nova, alerta com erro, interrupção ou tool falhando, de cabeça para baixo com a API fora do ar e
  dormindo com a observabilidade desligada. O balão diz o que chegou e o clique abre o run ou os
  Logs. Consulta `/observability/runs` a cada 5 s só com a aba visível; dá para minimizar.
- **Página Tools reorganizada no console.** As tools aparecem agrupadas por integração (prefixo do
  nome ou host da API), com as de sistema recolhidas no fim, e filtros em chip: tipo, efeito
  colateral, não classificada, desligada e sem agente. Cada linha mostra a 1ª frase da descrição em
  texto normal, método e host da API e quais agentes a usam; o detalhe abre a descrição inteira, a
  URL, os parâmetros com a origem de cada um e os agentes. Ligar/desligar também fica no menu da
  linha, e as tools que ainda não dizem se têm efeito colateral ganham um aviso. A API não muda.
- **Formulário de `dependencies` no console.** O painel Contexto do Playground, a tela Análise e o
  botão Testar de uma tool montam um formulário a partir dos `dependency_fields` do agente (ou dos
  parâmetros da tool `kind="api"`): um controle por campo, com obrigatórios, descrição, `enum` como
  seleção e validação inline. O modo **JSON** continua ao lado, sincronizado nos dois sentidos, e os
  campos não declarados aparecem em "Outros campos" em vez de se perderem. A API não muda.
- **Trechos consultados no trace.** A span `search_knowledge_base` de um run passa a trazer
  `metadata.references`: a pergunta, a collection e cada trecho recuperado (`rank`, `score`,
  `document`, `content_id`, `chunk_id`, `chunk`, `content` e, de sites, `source_url`/`page_title`).
  `GET /collections/{nome}/search` devolve esses mesmos campos ao lado de `content` e `metadata`.
  O que o modelo recebe não muda. Ver [o que o agente leu](conceitos.md#o-que-o-agente-leu).
- **Fontes na resposta.** `/chat` e `/analyze` passam a devolver `sources`: um item por documento
  ou página da base que as buscas do run devolveram, com `title`, `source_url`, `content_id`,
  `collection`, o melhor `score` e os `chunk_ids`. No `/chat/stream`, chega o evento novo `sources`
  antes do `done` — um cliente que ignora eventos desconhecidos não precisa mudar. `kuro chat`
  e `kuro analyze` listam as fontes no rodapé, e o console mostra um chip por fonte abaixo da
  resposta, com o link da página e o atalho para os trechos no trace. Ver
  [fontes consultadas](integracao.md#fontes-consultadas-sources).
- **Custo da coleta de sites nos Logs.** O que o agente formatador gasta em cada sincronização de
  uma fonte de site (o custo informado pelo provedor ou, sem ele, a estimativa da tabela de
  preços) fica gravado e entra no custo do panorama geral: `GET /observability/overview` soma em
  `totals.cost_usd`, `previous.cost_usd` e `buckets[].cost_usd`, e mostra a parte da coleta em
  `ingestion_cost_usd` (e os tokens em `totals.ingestion_tokens`, fora de `total_tokens`). Com
  `agent_type`, nada muda: a coleta não é de agente nenhum. Conta também a sincronização que falhou
  ou foi cancelada no meio. O `progress` da fonte ganha `format_cost_usd`.
- **Sites como fonte de uma collection.** `POST /collections/{nome}/sources` (`url`, `mode`
  `crawl`/`sitemap`/`page`, `max_pages`, `max_depth`, `include`, `exclude`, `format`, `refresh`)
  coleta o site em background (202), com a trava de egress das tools em cada salto, `robots.txt`
  e um agente formatador conferido contra a página. `GET .../sources[/{id}]` mostra o progresso e
  cada página; `POST .../sources/{id}/sync` ressincroniza (incremental: substitui o que mudou,
  remove o que sumiu); `PATCH` muda agendamento e opções; `DELETE` tira o site e as páginas
  (`?keep_documents=true` deixa as páginas). CLI: `kuro collections add-site|sources|sync|set-site|rm-site`;
  MCP: `collection_add_site`, `collection_sources`, `collection_sync_site`, `collection_rm_site`.
  Novas variáveis: `WEB_SOURCE_MAX_PAGES`, `WEB_SOURCE_CONCURRENCY`, `WEB_SOURCE_REQUEST_DELAY_MS`,
  `WEB_SOURCE_USER_AGENT`, `WEB_SOURCE_SCHEDULER`. Ver [sites como fonte](conceitos.md#sites-como-fonte).
- **Console: aba Site em qualquer coleção.** Substitui a aba URL, que só funcionava na coleção
  padrão (pipeline do AgentOS). O `/knowledge/content` do AgentOS continua existindo para quem o usa.
- **Gemini 3 em diante: sem `temperature`, `top_p` e `thinking_budget`.** O Google passou a
  recusar esses campos nos modelos novos (`400 INVALID_ARGUMENT`). Em agentes `google` com modelo
  Gemini 3+ (ou `-latest`) o Kuro deixa de enviá-los; o cadastro continua aceitando, então nada
  quebra, mas o valor não tem efeito. Para controlar o raciocínio use `reasoning`
  (`off`/`low`/`medium`/`high` → `thinking_level`). Gemini 2.x não muda.
- **Gráfico dos Logs: volume e taxa de falha separados.** Na aba Volume, as barras empilham as
  falhas na base, junto ao eixo (antes ficavam no topo, um fio invisível num dia de muito volume),
  e um segundo gráfico, alinhado ao primeiro, mostra a taxa de falha e a de tool falhando de cada
  período, em %, com a média do período como referência. Período com menos de 5 execuções sai
  com o ponto vazado e não define a escala; o mesmo vale na latência p95. O hover marca o mesmo
  período nos dois gráficos, e a tabela ganhou as colunas das taxas. Ver
  [console](console.md#o-gráfico-do-panorama-logs).
- **Console: Logs separados em Panorama, Sessões e Execuções.** O dashboard (KPIs, série no tempo,
  tools falhando, agentes e versões) fica em `/logs`; as listas para investigar ganham rota própria,
  `/logs/sessions` e `/logs/runs` (esta com a fila de revisão). O período, o agente e os filtros vão
  na URL, e cada agente, versão ou tool falhando do Panorama leva às execuções que conta. Os links
  antigos continuam valendo: `/logs?user_id=` abre as sessões do usuário e `/logs?session_id=` as
  execuções da sessão. Ver [console](console.md#logs-panorama-sessões-e-execuções).
- **`kuro dash` de cara nova**: tema com as cores do console, um cabeçalho com o corvo do Kuro (o
  humor dele mostra o estado do painel: execuções chegando, erro, pausado ou serviço fora do ar),
  a aba Execuções virou **Ao vivo**, com um resumo do que está na tela, e o **Panorama** virou
  cartões com a variação e um minigráfico do período. Nova tecla `a` (e um botão no cabeçalho)
  liga e desliga as animações; `TEXTUAL_ANIMATIONS=none` já abre com elas desligadas. As cores
  certas também pelo SSH: o painel liga o truecolor sozinho (`KURO_TRUECOLOR=0` desliga). Tudo pelo
  teclado: `]`/`[` trocam de aba, `tab` circula entre as tabelas, `/` filtra por agente, `s`, `t`
  e `d` mudam status, testes e período, `j`/`k` movem a seleção, `?` mostra todas as teclas e `g`
  abre o repositório no GitHub. Veja [docs/tui.md](tui.md).
- Fila de espera por vaga de execução em `/chat`, `/analyze` e `/chat/stream`: em ordem de chegada,
  limitada por `QUEUE_MAX_SIZE` (64) e `QUEUE_MAX_WAIT_SECONDS` (30). Fila cheia ou espera esgotada:
  503 com `Retry-After`, como antes. O `timeout_seconds` do run só conta depois que ele ganha a vaga.
  A fila é por processo e em memória: não sobrevive a restart.
- **Servidor MCP** (`kuro-mcp`, extra `mcp`): as operações da CLI como tools MCP, para agentes
  de IA (Claude Code e outros) operarem o Kuro sem passar pelo shell. Roda na máquina de quem
  opera, por stdio, e fala com o serviço pela API, local ou remoto, com as variáveis da CLI
  (`KURO_API_URL`, `KURO_API_KEY`, `KURO_CA_BUNDLE`). Remover, restaurar e promover pedem
  confirmação ao usuário pelo cliente MCP (elicitation); cliente sem esse suporte recebe erro com
  o comando equivalente da CLI. `chat`, `analyze`, `tool_invoke` e `eval` rodam em `dry_run` por
  padrão. Guia em `docs/mcp.md`.
- **`kuro dash`** (extra `tui`): painel em tela cheia no terminal, com as execuções ao vivo
  (Enter abre o trace, com os spans de modelo e tools) e o panorama do dashboard dos Logs. Funciona
  contra o serviço local ou remoto, com as mesmas flags da CLI. Guia em `docs/tui.md`.
- **Agente procedural** (`kind="procedural"`): um fluxo em etapas (`stages`) que o servidor conduz.
  Uma etapa coleta dados (`collect`), confirma (`confirm`) ou executa uma tool (`action`). O modelo
  só extrai valores e redige a resposta; avançar, voltar e concluir são decididos pelo código. A
  confirmação é montada dos dados, sem o modelo, e uma ação só roda depois dela. O que muda para
  quem integra, tudo aditivo:
  - `POST /chat` devolve `state` num agente procedural: `stage`, `collected`, `missing`, `invalid`,
    `done` e, ao concluir, `result`. No `/chat/stream`, o mesmo chega no evento `state`, antes do
    `done`. Nos outros tipos, `state` é `null` e o stream não muda.
  - **409** quando outra mensagem da mesma sessão ainda está sendo processada: espere a resposta
    dela e reenvie. É o que impede uma ação de rodar duas vezes.
  - A tool de uma etapa `action` recebe os dados coletados como argumentos e como `dependencies`,
    mais `dependencies.idempotency_key`: a mesma enquanto os dados não mudam, para o seu sistema
    reconhecer uma nova tentativa do mesmo pedido.
  - `GET /agents/{t}/procedures/{session_id}` (o estado de uma conversa; aceita a chave `runtime`)
    e `GET /agents/{t}/procedures` (o funil: quantas conversas em cada etapa; sem os testes em
    `dry_run`; exige `admin`).
  - Num agente procedural, um 502 ou 504 pode chegar depois de a ação ter rodado: leia o estado da
    sessão antes de cair no fallback.
- `kuro mcp-config`: rodado no servidor (`docker compose exec agent-service kuro mcp-config
  --show-key`), imprime o `claude mcp add ...` e o `.mcp.json` prontos para conectar o
  servidor MCP. Eles instalam o `kuro-mcp` com `uvx` direto do GitHub, sem clone na máquina de quem opera.
  O endereço sai sozinho: `KURO_PUBLIC_URL`, ou `https://` + `KURO_API_DOMAIN`, ou o HTTPS pelo IP
  do profile `ip`, com o certificado da CA para salvar (comandos em bash e PowerShell). `--url` sobrepõe.
- **HTTPS pelo IP, sem domínio** (profile `ip`): `docker compose --profile ip up -d` descobre o IP
  público e atende HTTPS na porta 58443 com a CA interna do Caddy. Não precisa de domínio, das portas
  80/443 nem de mexer no `.env`, e convive com um Traefik ou nginx na mesma máquina. Volumes novos:
  `caddy_ip_data` (a CA), `caddy_ip_config` e `kuro_public` (a URL e o certificado da CA, montado só
  para leitura no agent-service). Ver [operação](operacao.md#https-pelo-ip-sem-domínio-profile-ip).
- **`kuro connect`: o MCP pelo SSH.** Na máquina de quem usa,
  `uvx --from "agent-service[mcp] @ git+https://..." kuro connect root@servidor` entra pelo SSH, acha
  o container, testa o MCP (a tool `health`) e só então o registra no Claude Code. O MCP roda no
  container (`ssh ... docker exec -i <container> kuro-mcp`): sem porta aberta, sem certificado e sem a
  chave de API na máquina de quem usa. Se o SSH pedir senha, oferece instalar uma chave SSH. Ver
  [MCP](mcp.md#pelo-ssh-kuro-connect).
- **MCP pela URL em `/mcp`** (Streamable HTTP), com a chave admin: com domínio e o profile `tls`,
  `claude mcp add --transport http kuro https://<domínio>/mcp --header "Authorization: Bearer ..."`,
  sem instalar nada. O `kuro mcp-config` imprime o comando pronto quando há domínio e, em qualquer
  caso, começa pelo `kuro connect`.
- Pelo SSH e pela URL o MCP roda no servidor, então os parâmetros que recebem caminho de arquivo
  (`document_file`, `cases_file`, `rules_file`, o `file` de `collection_add`) são recusados: o caminho
  seria do container. Mande o conteúdo inline. Com o `kuro-mcp` na sua máquina, nada muda.
- **Console com a marca Kuro**: o corvo do `kuro dash` no lugar do ícone de robô, "kuro" no topo da
  barra lateral e no título da aba, e o favicon com o corvo. No tema escuro, os cinzas viraram os do
  `kuro dash` (ardósia em vez de verde-azulado), e verde e âmbar de status são os mesmos nos dois.
- Tool `kind="api"`: um parâmetro de header que não vem do modelo (`source` `dependency` ou
  `const`) aceita nome de header HTTP com hífen, como `Idempotency-Key` ou `X-Request-Id`. Antes só
  identificadores passavam, e não havia como mandar a `idempotency_key` no header esperado.
- **Rotação de chave sem derrubar ninguém.** `ADMIN_API_KEY` e `RUNTIME_API_KEY` aceitam uma lista
  separada por vírgula (`nova,antiga`), e qualquer uma vale. A primeira é a atual: é a que a CLI no
  container, o `/mcp` e o console mandam. Passo a passo em
  [operação](operacao.md#rotacionar-as-chaves).
- **OpenAPI de quem integra:** `GET /integration/openapi.json` abre com a chave **runtime** e traz só
  as rotas que ela alcança (`/chat`, `/chat/stream`, `/analyze`, scores, referência, estado do
  procedural) e os schemas delas. O `/docs` e o `/openapi.json` completos continuam exigindo admin.
- **`kuro connect` diz por que o SSH não entrou:** chave com passphrase fora do ssh-agent (manda
  carregar, em vez de oferecer outra chave), servidor que só aceita chave (manda pôr a pública no
  `authorized_keys`, sem pedir senha) ou senha (o fluxo de antes, que instala a chave). `--passphrase`
  cria a chave nova com senha de proteção e a carrega no agent. Ver [MCP](mcp.md#pelo-ssh-kuro-connect).
- **`CORS_ORIGINS` explícito.** Vazio mantém a lista de antes (a do AgentOS: `localhost:3000` e os
  domínios do Agno); `none` fecha para todas as origens; uma lista libera exatamente essas. `*` e
  origem com caminho derrubam a subida com o motivo. Ver
  [operação](operacao.md#segurança-e-limitações-atuais).
- **OpenAPI por agente:** `GET /agents/{t}/openapi.json`, com a chave **runtime**: só as rotas que o
  agente usa, `agent_type` fixo, `dependencies` tipadas (com `enum`), o `result` do analista em JSON
  Schema, o header `Idempotency-Key` e `servers` com a URL pública. O contrato de integração ganha
  `agent_openapi_url`. Ver [integração](integracao.md#2-acesso).
- **Contrato de integração mais completo** (`GET /agents/{t}/integration`, `kuro agents integrate`,
  `agent_integration` no MCP e a aba Integração): `base_url` e `base_url_source` (de onde saiu a
  URL), `openapi_url`, `agent_version`, `request_fields` (os campos do corpo, inclusive `metadata`,
  `dry_run` e `attachments`), `response_example` (com `run_id`, `agent_version`, `config_hash` e
  `sources`), `stream_events` e `errors` (a tabela de erros e o que fazer). O `request_example`
  passa a trazer `metadata` e `dry_run`.
- **Aba Integração do console** montada só a partir do contrato: os eventos do stream (`run`,
  `message`, `usage`, `state`, `sources`, `error`, `done`), a resposta de exemplo, a tabela de erros
  e um exemplo em **PHP** (cURL, com o stream lido por `CURLOPT_WRITEFUNCTION`). Os exemplos de
  stream em JavaScript e Python tratam o evento `error`, que antes era ignorado.
- **MCP:** cada parâmetro das tools tem descrição no schema (valores aceitos e exemplo). As
  respostas têm teto de ~30 mil caracteres: passou disso, textos e listas são cortados e a resposta
  traz `truncated: true` (`run_show`, `agent_get`, `agent_revisions`, `feedback_versions`,
  `collections_list`, `session_show`...). `agent_get(editable=true)` nunca é cortado.
- **MCP: as tools que faltavam frente à CLI.** `runs_reference` e `runs_export` fecham o ciclo
  shadow → dataset → `eval(cases=...)` sem sair do MCP (`runs_export` devolve os casos inteiros e diz
  quantos ficaram de fora); `runs_sessions` (execuções por conversa), `credentials_test` (testa a
  chave salva, sem expô-la), `collection_create`/`collection_get`/`collection_set`/
  `collection_embedders`, `agents_export` (o agente como código, sem corte) e `attachments`/`metadata`
  em `chat` e `analyze` (`analyze` também aceita `user_id` e só anexos). Ver [MCP](mcp.md#o-que-dá-para-fazer).
- **MCP: defaults coerentes.** `runs_list` passa a deixar os testes (`dry_run`) de fora por padrão,
  como `runs_overview` e `procedure_funnel` já faziam; filtrar por `session_id` os inclui sempre (o
  run que o `chat` do MCP acabou de criar continua achável). `runs_list` ganha `user_id` e
  `min_chars`. `sessions_list`, `session_show` e `session_delete` sem `user_id` olham as conversas de
  todos (antes, só as do `user_id` `mcp`: as da CLI e das integrações sumiam sem aviso), e
  `sessions_list` pagina (`page`, `meta.total_pages`).
- **`Idempotency-Key` no `/chat` e no `/analyze`.** Repetir com a mesma chave e o mesmo corpo devolve
  a resposta guardada (mesmo `run_id`, header `Idempotent-Replayed: true`) em vez de rodar o agente e
  as tools de novo; **409** se a primeira ainda está em andamento, **422** se o corpo for outro. Uma
  falha libera a chave. As tools recebem a chave em `dependencies.idempotency_key` (o header vence um
  valor mandado em `dependencies`), e ela fica no `metadata` do run. Vale por `IDEMPOTENCY_TTL_HOURS`
  (24 h). O `/chat/stream` recusa o header com 422. Ver
  [integração](integracao.md#repetir-sem-duplicar-idempotency-key).
- Uma tool com parâmetro `source="dependency"` em `idempotency_key` não exige mais que o agente
  declare o campo em `dependency_fields` (como já era com `dry_run`): o servidor o preenche.
- **`kuro connect` com keepalive no SSH** (`ServerAliveInterval=15`, `ServerAliveCountMax=3`,
  `ConnectTimeout=15`): com a rede caída, a tool falha em ~45 s em vez de ficar pendurada.
- **URL sem domínio próprio, documentada:** `KURO_API_DOMAIN=<ip-com-hífens>.sslip.io` com o profile
  `tls` dá HTTPS com certificado válido, inclusive para o MCP por `/mcp`. Precisa das portas 80 e
  443 livres. Ver [operação](operacao.md#sem-domínio-próprio-sslipio).

### Corrigido

- **Achar uma execução pelo id.** O `search` de `/observability/runs`, `/sessions` e `/stats` também
  acha pelo `run_id` ou `trace_id` inteiro (um trecho do id não conta). No console, colar um dos dois
  na busca de Logs abre a execução, mesmo que seja teste.
- **Números da aba Execuções batendo com a lista.** `GET /observability/stats` aceita `include_dry_run`
  (padrão `true`, como antes); no console, os números do agente passam a seguir o "Incluir testes"
  da lista, em vez de contar testes que ela esconde.
- Com a autenticação ligada, o preflight de CORS (`OPTIONS`, que o navegador manda sem a chave)
  recebia 401, e nenhuma página em outro domínio conseguia chamar a API, nem as origens liberadas.
  O preflight passa a ser respondido pelo CORS; a chamada de verdade continua exigindo a chave.
- O contrato de integração pedido pelo MCP ou pela CLI dentro do container saía com
  `http://localhost:8000` no endpoint e no cURL. Sem `base_url`, o servidor agora usa a URL pública
  que conhece (`KURO_PUBLIC_URL`, depois `KURO_API_DOMAIN`, depois o profile `ip`, por último
  `http://127.0.0.1:<porta publicada>`), a mesma do `kuro mcp-config`. Quem manda `base_url`
  explícito continua recebendo o que pediu.
- O link "OpenAPI" da aba Integração e o "Referência OpenAPI" do menu do console abriam o `/docs`,
  que dá 403 sem a chave admin. Agora abrem o OpenAPI de integração pelo console.
- `kuro agents integrate` num agente analista imprimia `streaming (SSE): POST None`.

- O título de uma conversa com `dependencies` mostrava os dados do cliente: sem nome definido, o
  AgentOS usa a primeira mensagem como título, e o Agno junta a ela o bloco
  `<additional context>{...}` (CPF, nome...). O console, a CLI (`kuro sessions list`) e o MCP
  agora mostram só a mensagem — vale também para as conversas antigas. A API `/sessions` continua
  devolvendo o nome como o AgentOS o guarda.
- Clone novo no Windows (`core.autocrlf=true`): o script de init do Postgres saía com CRLF e o
  `docker compose up` falhava com "cannot execute: required file not found". O repositório agora
  fixa LF nos arquivos que rodam dentro dos containers (`.gitattributes`). Quem já clonou: apague
  `docker/` e rode `git checkout -- docker/` para regravar os arquivos.
- `KURO_CA_BUNDLE` apontando para um arquivo que não existe: a CLI, o `kuro-mcp` e o `kuro dash`
  quebravam com um traceback do Python. Agora é um erro de certificado (sai com 3 na CLI) que diz
  qual caminho faltou.
- Porta bloqueada por firewall (a conexão fica sem resposta até o timeout) dizia "o serviço está no
  ar? (docker compose up -d)", que manda olhar o lugar errado. Agora a CLI, o MCP e o painel dizem
  que a porta parece bloqueada por um firewall no caminho (o do painel do provedor, por exemplo) e
  sugerem o `kuro connect`; e não tentam de novo, o que dobrava a espera.

### Como atualizar

- Rode o `up` com `--build` e com todos os profiles que você já usa: o `kuro mcp-config` novo, o
  `kuro-mcp` que o `kuro connect` chama e o `/mcp` moram na imagem (o pacote `mcp` passou a ser
  dependência do serviço; o extra `[mcp]` continua aceito). Para ligar o HTTPS pelo IP, acrescente `--profile ip`, por exemplo
  `docker compose --profile ip up -d --build`. Ele só sobe com `ADMIN_API_KEY` e `RUNTIME_API_KEY`
  preenchidas, e a porta 58443 precisa estar liberada no firewall do provedor.
- A migração `0007` roda sozinha no startup: acrescenta `agent_definitions.stages` e cria a
  tabela `procedure_runs`. Nada muda nos agentes que já existem, nem a versão da configuração deles.
- A migração `0008` também roda sozinha: cria `knowledge_sources` e `knowledge_source_pages`
  (os sites das collections).
- A migração `0009` também roda sozinha: cria `knowledge_sync_costs` (o gasto do formatador de
  sites por sincronização). Sincronizações anteriores a ela não entram no custo dos Logs.
- A migração `0010` também roda sozinha: cria `idempotency_keys` (as chaves do header
  `Idempotency-Key`, com a resposta guardada). As vencidas somem sozinhas. Variável nova, opcional:
  `IDEMPOTENCY_TTL_HOURS` (24).
- A migração `0011` também roda sozinha: cria a tabela `jobs` (a fila de jobs no Postgres). No
  startup, cada fonte de site agendada ganha o seu próximo sync na fila, e a que ficou presa em
  `running` vira `failed`. Nada a configurar; as variáveis `JOBS_*` são opcionais (ver
  [fila de jobs](operacao.md#fila-de-jobs)). Com mais de uma réplica, todas podem ficar com
  `JOBS_ENABLED=true` e `WEB_SOURCE_SCHEDULER=true`: a fila não roda o mesmo job duas vezes.
- A migração `0012` também roda sozinha: acrescenta a configuração de busca em
  `document_collections`, cria `collection_versions` (com a v1 de cada collection existente) e
  acrescenta `knowledge_collection`, `collection_version` e `collection_config_hash` em `runs`.
  Nenhuma collection muda de comportamento; runs anteriores ficam com esses campos nulos.
- A migração `0013` também roda sozinha: cria `chunk_feedback` (as marcações por trecho do RAG).
- A migração `0014` também roda sozinha: cria `document_revisions` e acrescenta o estado e a revisão
  em `chunk_feedback`. As marcações existentes ficam `open`, com a revisão desconhecida; os documentos
  já indexados ganham a revisão 1 na primeira vez que alguém a consulta, sem reindexar nada.
- `AGENT_SERVICE_PUBLIC_URL` do console deixou de ter `http://localhost:58000` fixo no compose:
  vazia, a aba Integração mostra a URL que o agent-service conhece. Para forçar outra, defina-a no
  `.env`.
- Quem conectou o MCP pelo SSH antes desta versão: rode o `kuro connect` de novo para registrar o
  comando com keepalive.
- MCP: `agent_versions` sem `version` passou a devolver a lista resumida (versão, data, `chars` e
  `preview`) em vez do texto inteiro de cada versão; peça `version=N` para o texto.

---

## 0.2.0

Primeira rodada de melhorias vinda da integração do R8 em produção: erros de configuração com
motivo claro, modo teste para tools, upload de documentos no RAG, informações para escolher
modelo e cache, falha de tool visível no trace, dashboard dos Logs refeito e fila de revisão.

### Ação necessária

1. **Erro de configuração agora é 503 com `error`, e não 500.** Quando falta a chave do provedor
   do modelo ou a `CREDENTIALS_ENCRYPTION_KEY`, a resposta é:

   ```json
   HTTP 503
   {"detail": "google: nenhuma chave cadastrada em /model-credentials nem GOOGLE_API_KEY no ambiente",
    "error": "model_provider_not_configured"}
   ```

   `error` é `model_provider_not_configured` ou `encryption_not_configured`. O 503 com header
   `Retry-After` continua significando falta de vaga ou banco fora. **O que fazer:** se o seu
   cliente repete todo 503, não repita quando o corpo tiver `error`. Nesse caso, caia no fallback e
   alerte quem opera o Kuro, porque repetir não resolve
   ([integração, seção 4](integracao.md#4-erros-e-fallback)).

2. **Agente google sem chave falha na hora.** Antes, o run começava e podia terminar como
   `interrupted` ("cliente desconectou"), o que parecia problema de rede. Agora a chamada responde 503 antes de
   executar. Se o seu monitoramento contava `interrupted` como sinal de credencial, passe a olhar o
   503 com `error`.

3. **As chamadas de teste do console avisam as tools.** O Playground, a tela de Análise e o teste de
   tool mandam `dry_run` **ligado por padrão**. Com isso, toda chamada HTTP de tool leva o header
   `X-Kuro-Dry-Run: true`. **O que fazer:** nas APIs que suas tools chamam e que têm efeito
   colateral (cobrança, acordo, mensagem), trate o header e, quando ele vier, só registre a
   intenção. Uma API que ignora o header continua executando de verdade.

4. **`dry_run` virou nome reservado em `dependencies`.** Um parâmetro de tool com
   `"source": "dependency", "dependency": "dry_run"` recebe o flag da requisição (`true`/`false`),
   e um valor `dry_run` enviado nas `dependencies` é ignorado. Pelo mesmo motivo, um header
   `X-Kuro-Dry-Run` fixo na config da tool é descartado. **O que fazer:** se você já usava um campo
   com esse nome para outra coisa, renomeie.

5. **Texto da mensagem de run interrompido mudou.** Agora é "Execução interrompida antes de
   terminar: quem chamou desconectou ou cancelou (ex.: timeout do cliente menor que o
   timeout_seconds do agente)." Só afeta quem compara esse texto; o status continua
   `interrupted`.

6. **`POST /tools/{nome}/invoke` responde `ok: false` quando a API da tool devolve erro.**
   Antes, um HTTP 4xx/5xx ou uma falha de rede saíam como `ok: true`, com o erro só no texto de
   `result`. Agora a resposta traz `failure` (o tipo) e `http_status`, e `kuro tools invoke` sai
   com código 1. O 404 é a exceção: continua `ok: true`, com `failure: "not_found"`. **O que
   fazer:** se um script seu tratava `ok: true` com "HTTP 500" no texto como sucesso, ele passa a
   ver a falha. É o comportamento correto.

### Novo

- **`dry_run`** no `/chat`, `/analyze` e `/tools/{nome}/invoke`. O run fica com
  `metadata.dry_run = "true"` (`kuro runs list --meta dry_run=true`). Na CLI, use `--dry-run` em
  `chat`, `analyze`, `eval` e `tools invoke` ([integração, seção 6](integracao.md#modo-teste-dry_run)).
- **`model_params.prompt_cache`** (`5m` ou `1h`, só anthropic) guarda as instructions e as tools no
  cache do Claude. No google e no openai o campo é recusado com 422, porque os dois já fazem esse
  cache sozinhos.
- **Tokens de cache por execução:** a span do modelo traz `metadata.cache_read_tokens` e
  `cache_write_tokens` (`kuro runs show <run_id>`), em qualquer provedor que informe.
- **`channel` na lista de modelos** (`GET /model-providers/{p}/models`): `stable`, `preview` ou
  `alias` (`-latest`). É deduzido do nome, porque nenhum provedor informa. O console avisa ao
  escolher um modelo preview ou alias.
- **`GET /health`** passou a devolver `version` e `model_credentials` (`enabled`, ou o motivo de o
  cadastro de chaves estar desligado).
- **`docker-compose.override.example.yml`**: exemplo de como ligar o Kuro à rede Docker de outro
  sistema, com o `TOOL_EGRESS_ALLOWLIST` que as tools precisam
  ([integração, seção 6](integracao.md#rede-docker)).
- **Custo estimado dos modelos Gemini 3.x.** A tabela de `models/pricing.py` agora conhece
  `gemini-3.1-flash-lite`, `3.5-flash`, `3.5-flash-lite`, `3.6/3.7/3.8-flash`, `3.1-pro-preview` e
  `omni-1.1-flash`; antes o `cost_usd` desses runs vinha `null`. Os 3.6–3.8 têm preço promocional até
  2026-12-31 e dobram em 2027-01-01: ajuste com `MODEL_PRICES` na virada. Runs antigos não são recalculados.

- **Falha de tool aparece no trace.** Uma chamada de tool que falha (HTTP 4xx/5xx, rede, destino
  bloqueado, exceção) vira uma span com `level: "ERROR"` e `metadata.failure`:
  `invalid_arguments`, `auth`, `unavailable`, `config` ou `exception`. O **`status` do run não
  muda**: se o agente respondeu, o run continua `success`. O texto que o modelo lê também é o mesmo
  de antes. Um **404 não conta como falha** (pode ser resposta normal, como "não há sessões
  anteriores"): a span sai como aviso (`WARNING`, `failure: "not_found"`), fica fora de
  `tool_failures` e continua visível no trace e no quadro de tools do dashboard.
- **Resumo de tools no run** (`GET /observability/runs` e `kuro runs show`): `tool_calls`,
  `tool_failures` e `complexity` (1 = nenhuma tool de negócio, 2 = uma ou duas distintas, 3 = três
  ou mais; memória e busca na base não contam). `null` no backend Langfuse.
- **`side_effect` nas tools** (`true`, `false` ou vazio): diz se a tool grava, cobra, transfere ou
  envia algo. É opcional; o console avisa enquanto estiver vazio. Não entra na versão da
  configuração do agente, então classificar uma tool não gera versão nova.
- **`GET /observability/overview`**: o panorama do dashboard dos Logs num pedido só. Traz totais
  com o período anterior, a série no tempo sem buracos (por hora, dia ou semana, conforme o
  intervalo, no fuso `tz`, padrão `America/Sao_Paulo`), uma linha por agente com as versões da
  configuração que rodaram, e as tools que estão falhando. Os testes `dry_run` ficam de fora, a
  menos que `include_dry_run=true`. Só no trace store local; com o Langfuse responde 501.
- **Dashboard dos Logs refeito**: indicadores com variação contra o período anterior (falhas
  separadas de tool falhando, latência p95 em vez da média), gráfico por resultado, custo ou
  latência com visão em tabela, quadro de tools falhando e tabela por agente com a comparação
  entre versões. A busca e o filtro de status passaram para junto das listas.
- **Fila de revisão** (`GET /observability/runs` e `kuro runs list`): filtros `complexity`
  (repetível), `tool_failed`, `side_effect` (pela classificação atual das tools, então vale também
  para runs antigos), `min_message_chars` (esconde "ok" e "oi"), `feedback` (`up`, `down`, `none`),
  `include_dry_run=false` e `sample=N` (amostra aleatória, sem paginação). No console, ficam na aba
  Execuções dos Logs e de cada agente, com a coluna de complexidade e falhas de tool.
- A falha de rede de uma tool passou a dizer o tipo do erro ("Falha ao chamar a API:
  ReadTimeout"). Antes vinha vazia.

### Corrigido

- Upload de **DOCX, PDF, CSV, PPTX, XLSX e XLS** no RAG falhava com "package is not installed". As
  bibliotecas de leitura agora fazem parte da imagem.
- `POST /model-credentials` sem `CREDENTIALS_ENCRYPTION_KEY` devolvia 500 sem explicação. Agora
  devolve 503 dizendo como gerar a chave.
- Um agente cujo provedor estava sem chave impedia o serviço de subir. Agora o serviço sobe, e só
  esse agente responde 503.

### Como atualizar

1. Reconstrua a imagem. É obrigatório, porque há dependências novas:

   ```bash
   docker compose up -d --build agent-service
   ```

   A migração `0006` roda sozinha no boot: cria as colunas de tools no run e o `side_effect` das
   tools, e **reclassifica os runs já gravados** a partir dos textos de erro que as tools
   `kind="api"` devolviam. Com muitos meses de dados, rode antes com `kuro-migrate` para o boot não
   esperar. Nenhuma variável de ambiente nova.
2. Confira com `kuro --json health`: `service.version` deve ser `"0.2.0"` e
   `service.model_credentials` deve ser `"enabled"`.
3. Se as tools chamam outro container pela rede Docker, troque o `docker network connect` manual
   pelo `docker-compose.override.yml` (copie do `.example`). A ligação manual some a cada
   `docker compose up`.
4. Reenvie os documentos que falharam no upload. Os que ficaram com erro aparecem em
   `kuro --json collections docs <coleção>`; remova-os com `rm-doc` antes de reenviar.
5. Classifique as tools com efeito colateral: `kuro --json tools set <tool> side_effect=true`, ou
   pela tela de Tools. Sem isso, o filtro "Efeito colateral" da fila de revisão fica vazio.

---

## 0.1.0 — 2026-09-30

Versão que entrou em produção, até o commit `5de73fe`. É a base das notas acima: o contrato de
`/chat` e `/analyze`, os agentes como código (`kuro agents apply`), o fluxo draft → eval → promote,
o modo shadow, o RAG por coleção, a autenticação por escopo e o console. Tudo isso está descrito em
[integracao.md](integracao.md) e no [README](../README.md).

---

## Para quem mantém o Kuro: como registrar uma versão

Toda mudança que **quem integra ou opera** perceberia ganha uma linha aqui no mesmo PR: campo novo
ou removido, status HTTP diferente, texto de erro, default que mudou, variável de ambiente, passo de
deploy. Refatoração interna, teste e ajuste visual do console não entram.

1. Durante o desenvolvimento, as linhas vão para uma seção `## Não lançada` no topo, nas mesmas
   subseções.
2. No lançamento, troque `Não lançada` pelo número e pela data, suba a `version` do
   `pyproject.toml` (é o que o `/health` mostra) e crie a tag: `git tag v0.3.0`.
3. Regra para escolher o número: precisa de ação de quem integra → MENOR; só correção → CORREÇÃO.

Modelo de entrada:

```markdown
## 0.3.0 — AAAA-MM-DD

Uma frase sobre o tema da versão.

### Ação necessária
1. **O que mudou, em uma linha.** Por que mudou. **O que fazer:** o passo concreto.

### Novo
- **Nome da coisa**: o que faz e onde está documentada.

### Corrigido
- O sintoma que deixou de acontecer.

### Como atualizar
1. Rebuild? Migração? Variável nova? Como conferir que deu certo.
```
