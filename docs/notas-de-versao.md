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

Nenhuma, mas muda o comportamento no limite: acima de `MAX_CONCURRENT_RUNS`, a chamada **espera**
na fila (até `QUEUE_MAX_WAIT_SECONDS`, 30 s) em vez de receber 503 na hora, e só então o run começa
a contar o `timeout_seconds`. O timeout do seu cliente precisa cobrir as duas esperas
(`QUEUE_MAX_WAIT_SECONDS + timeout_seconds + 5`); um cliente com `timeout_seconds + 5`, como o
[guia de integração](integracao.md#4-erros-e-fallback) recomendava, pode desistir com o run ainda na fila.
Para o comportamento antigo (503 na hora), use `QUEUE_MAX_WAIT_SECONDS=0`.

### Novo

- **Trechos consultados no trace.** A span `search_knowledge_base` de um run passa a trazer
  `metadata.references`: a pergunta, a collection e cada trecho recuperado (`rank`, `score`,
  `document`, `content_id`, `chunk_id`, `chunk`, `content` e, de sites, `source_url`/`page_title`).
  `GET /collections/{nome}/search` devolve esses mesmos campos ao lado de `content` e `metadata`.
  O que o modelo recebe não muda. Ver [o que o agente leu](conceitos.md#o-que-o-agente-leu).
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

### Corrigido

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
  (os sites das collections). O agendador de ressincronização liga no startup. Um sync em
  andamento renova um heartbeat a cada 30 s; sem sinal por 2 minutos (o processo caiu no meio),
  ele é marcado `failed` e pode ser disparado de novo. A trava no banco impede dois syncs da mesma
  fonte, inclusive entre réplicas; com mais de uma, deixe `WEB_SOURCE_SCHEDULER=true` em uma só.

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
