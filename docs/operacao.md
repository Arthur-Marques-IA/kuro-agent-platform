# Operação

Configuração, HTTPS, deploy, segurança e limitações.

## Subindo os serviços

**Pré-requisitos:** Docker e uma chave do Google Gemini.

```bash
cp .env.example .env
# edite o .env: GOOGLE_API_KEY=... e CREDENTIALS_ENCRYPTION_KEY=...
# gere a chave de cifra com:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

docker compose up -d --build                       # núcleo: postgres, agent-service
uv run kuro health                                 # serviço, autenticação, Langfuse, provedores
```

O núcleo sobe sozinho, com dois containers, e já registra cada execução no
próprio Postgres (`kuro runs`, `/observability/*`). Console e Langfuse são
**opcionais**, cada um num profile do Compose. O Langfuse (ClickHouse + MinIO +
Redis + worker, ~16 GB recomendados) é só um exportador a mais para análise
profunda, e a imagem nem o instala por padrão:

```bash
docker compose --profile ui up -d                              # + console web
KURO_EXTRAS="--extra observability" docker compose build agent-service   # imagem com o exportador
LANGFUSE_ENABLED=true docker compose --profile ui --profile observability up -d   # tudo
```

> Com o profile `observability` fora, `LANGFUSE_ENABLED` fica `false` por padrão.
> Ligado com o Langfuse fora do ar, cada execução pagaria o timeout do exportador.

| Serviço | Endereço | Profile | Para quê |
|---|---|---|---|
| API | http://127.0.0.1:58000 | (núcleo) | contrato de integração; `/docs` tem o OpenAPI |
| Postgres | `127.0.0.1:55432` | (núcleo) | pgvector; porta fora do padrão para não colidir |
| Console | http://localhost:3000 | `ui` | playground, agentes, tools, base de conhecimento, logs |
| Langfuse | http://localhost:3100 | `observability` | exportador opcional: análise profunda dos traces |

As portas são publicadas **só em `127.0.0.1`**. Para expor numa rede, não mude
`AGENT_SERVICE_BIND`: suba o profile `tls`, que põe HTTPS na frente.

> **Máquina com pouca RAM?** O Langfuse self-hosted (ClickHouse, MinIO, Redis
> e dois serviços) pede ~16 GB. Numa máquina de 8 GB ele sozinho deixou as
> respostas 4x mais lentas. O padrão já é sem ele: `docker compose up -d`
> (mais `--profile ui` para o console) — as execuções continuam registradas.

## HTTPS (profile `tls`)

A chave de API viaja num header. Em HTTP puro ela vai em texto claro no fio,
junto com as mensagens do cliente — para um serviço chamado por outra
plataforma, isso não serve. O profile `tls` sobe um [Caddy](https://caddyserver.com)
na frente, que resolve certificado e renovação sozinho:

```bash
# no .env: KURO_API_DOMAIN=kuro.suaempresa.com e KURO_TLS_EMAIL=voce@suaempresa.com
docker compose --profile tls up -d
```

Com o domínio apontando para a máquina e as portas 80/443 alcançáveis, o
certificado é emitido pelo Let's Encrypt no primeiro acesso e renovado antes de
vencer. A porta 80 precisa ficar aberta: é por onde a validação acontece e por
onde o `http://` é redirecionado para `https://`.

Deixando `KURO_API_DOMAIN=localhost` (o padrão), o Caddy usa uma CA interna — dá
para testar a configuração inteira sem domínio nenhum. A CLI, nesse caso, não vai
confiar no certificado; use `--insecure` **só nesse teste**, ou aponte a CA com
`KURO_CA_BUNDLE=/caminho/ca.pem` se a sua rede tem CA própria.

O `58000` continua publicado em `127.0.0.1` para a CLI local. Quem vem de fora
entra pelo 443.

Com o domínio, o MCP também fica acessível pela URL: o serviço o atende em `/mcp`, com a chave
admin (ver [MCP](mcp.md#pela-url-mcp)). O serviço precisa rodar com um worker só, o padrão.

### Sem domínio próprio: sslip.io

Para ter "URL + chave" com certificado válido sem comprar domínio, use um DNS curinga público
que resolve o próprio IP, como o [sslip.io](https://sslip.io): `203-0-113-10.sslip.io` resolve
para `203.0.113.10`. O Caddy do profile `tls` emite um certificado Let's Encrypt normal para
esse nome, e nada no código muda:

```bash
# no .env (troque pelo IP público da VPS, com hífens):
#   KURO_API_DOMAIN=203-0-113-10.sslip.io
#   KURO_TLS_EMAIL=voce@suaempresa.com
docker compose --profile tls up -d
curl https://203-0-113-10.sslip.io/health
```

Com isso, o contrato de integração (`kuro agents integrate`, o console, o MCP) já sai com a URL
`https://203-0-113-10.sslip.io`, o `kuro mcp-config` gera o comando do MCP por `/mcp` e quem
integra chama `/chat` e `/analyze` com a chave runtime.

As condições:

- **As portas 80 e 443 precisam estar livres na VPS e abertas no firewall do provedor.** O
  Let's Encrypt valida pela porta 80. Se um Traefik ou nginx já ocupa essas portas, não suba o
  profile `tls`: crie no proxy que já existe uma rota para `kuro.203-0-113-10.sslip.io` apontando
  para `127.0.0.1:58000`, e defina `KURO_PUBLIC_URL` com essa URL.
- **O nome muda junto com o IP.** Trocou o IP, troque o `KURO_API_DOMAIN` e avise quem integra.
- **É um DNS de terceiro, sem SLA:** se ele cair, a integração cai junto. Serve para piloto; em
  produção, um domínio próprio (o mesmo profile `tls`).
- **A verificar:** se o sslip.io está na Public Suffix List, o que muda o limite de certificados
  por semana do Let's Encrypt. Com o volume `caddy_data` preservado, o Caddy só emite de novo
  na renovação.

> **Windows:** use `127.0.0.1` para a API, não `localhost`. Com o Docker
> Desktop, `localhost:58000` pode tentar IPv6 primeiro e travar por 30 s.

## HTTPS pelo IP, sem domínio (profile `ip`)

Sem domínio, o profile `ip` dá HTTPS no IP público da máquina. Para o MCP de quem tem SSH no
servidor, ele não é necessário: o `kuro connect` conecta pelo SSH, sem porta aberta (ver
[MCP](mcp.md#pelo-ssh-kuro-connect)). O profile serve para a CLI, o painel e o MCP de quem não
entra no servidor.

```bash
docker compose --profile ip up -d
```

- **Só sobe com a autenticação ligada.** O profile põe o serviço na internet; sem
  `ADMIN_API_KEY`/`RUNTIME_API_KEY`, o `caddy-ip` recusa começar e diz o que falta no log
  (`docker compose logs caddy-ip`). `KURO_IP_ALLOW_OPEN=1` pula a verificação, só para teste local.
- **O IP é descoberto sozinho** (via `api.ipify.org`, com alternativas). Para fixar, ou se a
  máquina não alcança a internet, defina `KURO_PUBLIC_IP` no `.env`.
- **O certificado vem da CA interna do Caddy**, não do Let's Encrypt. Por isso dispensa domínio,
  as portas 80 e 443 e a cota de emissão, e convive com um Traefik ou nginx que já ocupe essas
  portas. Só a porta 58443 (`KURO_IP_PORT`) é publicada.
- **Quem conecta precisa confiar nessa CA.** O `kuro mcp-config` entrega o certificado pronto para
  salvar. A CLI e o painel usam o mesmo arquivo: `KURO_CA_BUNDLE=~/.kuro/kuro-ca-<ip>.pem`. No
  navegador, o console mostra o aviso de certificado desconhecido; a API e o MCP não são afetados.
- **A CA dura até 10 anos** e mora no volume `caddy_ip_data`. Os certificados do servidor duram
  horas, mas o Caddy os renova sozinho, sem mudar a CA. Uma atualização ou um `down` comum não
  mexem nela; um `docker compose down -v` apaga o volume e cria uma CA nova, e quem já conectou
  precisa salvar o certificado de novo.
- **O Docker publica a porta por cima do `ufw`, mas não do firewall do provedor.** Se o provedor
  tiver firewall no painel (Hostinger, AWS, Oracle, GCP...), a 58443 precisa estar liberada lá, e
  ele fica fora da VPS: nada no terminal mostra o bloqueio. O sintoma é a conexão ficar sem resposta
  até o timeout, e a CLI e o MCP dizem "a porta parece bloqueada por um firewall". Para conferir do
  lado do servidor, sem passar pelo firewall:
  `curl -k --resolve <ip>:58443:127.0.0.1 https://<ip>:58443/health`.
- **Se o IP da máquina mudar**, recrie o `caddy-ip` e rode o `kuro mcp-config` de novo.

O `kuro mcp-config` só publica o certificado da CA (no volume `kuro_public`, só leitura para o
agent-service). A chave privada dela fica no volume do Caddy.

## Configuração

| Variável | Padrão | Para quê |
|---|---|---|
| `ADMIN_API_KEY` / `RUNTIME_API_KEY` | — | chaves de API; **sem elas o serviço fica aberto**. Aceitam lista `nova,antiga` para [rotacionar](#rotacionar-as-chaves) |
| `KURO_API_DOMAIN` / `KURO_TLS_EMAIL` | `localhost` / — | domínio e e-mail do certificado (profile `tls`) |
| `KURO_PUBLIC_IP` / `KURO_IP_PORT` | descoberto / `58443` | IP e porta do HTTPS pelo IP (profile `ip`) |
| `KURO_PUBLIC_URL` | — | endereço do serviço visto de fora, se nenhum dos profiles acerta (o `kuro mcp-config` e o contrato de integração usam) |
| `AGENT_SERVICE_BIND` | `127.0.0.1:58000` | onde a API é publicada no host |
| `TRACE_STORE_BACKEND` | `db` | de onde `kuro runs` e `/observability/*` leem: `db` ou `langfuse` |
| `RUN_TIMEOUT_SECONDS` | `90` | tempo limite padrão de uma execução (o agente pode ter `timeout_seconds`); estourou, 504 |
| `MAX_CONCURRENT_RUNS` | `16` | execuções simultâneas por processo; acima disso, a chamada espera na fila |
| `QUEUE_MAX_SIZE` | `64` | chamadas esperando vaga, por processo; fila cheia = 503 com `Retry-After` |
| `QUEUE_MAX_WAIT_SECONDS` | `30` | espera máxima na fila antes do 503; `0` desliga a fila. Deixe abaixo do timeout de quem chama |
| `CORS_ORIGINS` | — (a lista do AgentOS) | páginas, em outro domínio, que podem ler as respostas da API no navegador; `none` fecha. Ver [segurança](#segurança-e-limitações-atuais) |
| `IDEMPOTENCY_TTL_HOURS` | `24` | por quanto tempo um `Idempotency-Key` no `/chat`/`/analyze` devolve a resposta guardada ([integração](integracao.md#repetir-sem-duplicar-idempotency-key)) |
| `AUX_MODEL_ID` / `AUX_MODEL_PROVIDER` | — (o do agente) | modelo barato para extrair memória e resumir a sessão |
| `MEMORY_CONTEXT_LIMIT` | `10` | quantas memórias de longo prazo (as mais recentes) entram no prompt |
| `MODEL_PRICES` | tabela embutida | JSON `{"modelo": [USD/1M entrada, USD/1M saída]}` para o custo estimado sem Langfuse |
| `GOOGLE_API_KEY` | — | chave do Gemini (provedor padrão) |
| `CREDENTIALS_ENCRYPTION_KEY` | — | chave Fernet que cifra as credenciais de modelo; **obrigatória** para cadastrar chaves |
| `DEFAULT_MODEL_PROVIDER` / `DEFAULT_MODEL_ID` | `google` / `gemini-2.5-flash` | modelo de quem não define um |
| `DATABASE_URL` | localhost | sobrescrito dentro do compose |
| `MAX_ATTACHMENT_MB` | `20` | limite por anexo |
| `MAX_INPUT_CHARS` | `200000` | teto do texto de entrada (`message` do `/chat`, `document` do `/analyze`) |
| `CUSTOM_PYTHON_TOOLS_ENABLED` | `false` | liga a execução de tools `python` |
| `TOOL_EGRESS_ALLOWLIST` | — | hosts internos que as tools podem alcançar (vazio = só endereços públicos) — vale também para a coleta de sites |
| `WEB_SOURCE_MAX_PAGES` | `500` | teto de páginas por site, qualquer que seja o `max_pages` pedido |
| `WEB_SOURCE_CONCURRENCY` | `3` | páginas processadas ao mesmo tempo por site (download + formatação) |
| `WEB_SOURCE_REQUEST_DELAY_MS` | `300` | intervalo mínimo entre requisições ao mesmo site |
| `WEB_SOURCE_USER_AGENT` | `KuroBot/1.0 (...)` | como o coletor se apresenta (é o nome que o `robots.txt` enxerga) |
| `WEB_SOURCE_SCHEDULER` | `true` | ressincroniza os sites com `refresh` agendado |
| `MEM0_ENABLED` / `MEM0_API_KEY` | `false` / — | habilita agentes com `memory_backend: "mem0"` |
| `LANGFUSE_ENABLED` | `false` | liga o exportador — exige o extra `observability` e o profile de mesmo nome |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | valores de dev | conexão com o Langfuse |
| `LANGFUSE_TIMEOUT_SECONDS` | `20` | timeout do envio de traces |
| `AGENT_SERVICE_URL` (frontend) | — | endereço interno da API, só no servidor Next.js |
| `AGENT_SERVICE_PUBLIC_URL` (frontend) | — | endereço exibido nos exemplos de integração; vazio, vale o que o agent-service conhece (`KURO_PUBLIC_URL`, `KURO_API_DOMAIN`, profile `ip`) |
| `KURO_API_URL` / `KURO_JSON` / `KURO_NO_INPUT` (CLI) | `http://127.0.0.1:58000` | endereço, saída JSON e modo não interativo |

## Rotacionar as chaves

`ADMIN_API_KEY` e `RUNTIME_API_KEY` aceitam uma lista separada por vírgula, e qualquer chave da
lista vale. Assim dá para trocar a chave sem derrubar quem já integra:

1. Gere a nova (`python -c "import secrets; print(secrets.token_urlsafe(32))"`) e ponha **na
   frente** da atual: `RUNTIME_API_KEY=nova,antiga`. Rode `docker compose up -d` para recriar os
   containers.
2. Entregue a nova a quem integra. Enquanto isso, as duas funcionam.
3. Quando todos tiverem trocado (os runs param de chegar com a antiga), deixe só a nova:
   `RUNTIME_API_KEY=nova` e `docker compose up -d` de novo. A antiga passa a receber 403.

A primeira da lista é a atual: é a que o próprio Kuro usa quando chama a si mesmo — a CLI no
container (`KURO_API_KEY`, que o compose copia de `ADMIN_API_KEY`), o MCP em `/mcp` e o console
(`AGENT_SERVICE_API_KEY`). Por isso a nova vai na frente. Espaços em volta das vírgulas são
ignorados; uma chave não pode conter vírgula. Num `curl` à mão durante a rotação, mande uma
chave só: `-H "Authorization: Bearer ${ADMIN_API_KEY%%,*}"` (a lista inteira recebe 403).

## Segurança e limitações atuais

Leia antes de expor o serviço fora de uma rede confiável:

- **Autenticação: defina as duas chaves antes de sair da sua máquina.**
  `ADMIN_API_KEY` e `RUNTIME_API_KEY` no `.env`. Sem elas o serviço fica aberto
  (comportamento de antes, para não quebrar quem roda local) e quem alcança a
  porta lê toda conversa que passou por ali (`/observability/runs`, `/sessions`),
  reescreve o prompt de um agente em produção e lê as credenciais de modelo.
  `kuro health` mostra em vermelho quando está aberto.

  | Escopo | Alcança | Quem usa |
  |---|---|---|
  | `RUNTIME_API_KEY` | `/chat`, `/chat/stream`, `/analyze`, scores, referência do shadow | os outros módulos da plataforma |
  | `ADMIN_API_KEY` | tudo: CRUD, traces, sessões, credenciais | console e CLI (`KURO_API_KEY`) |

  A chave de runtime é a que você entrega para fora: se vazar, o estrago é gastar
  token — não ler o histórico de todo mundo nem trocar o prompt. Para trocá-la,
  veja [Rotacionar as chaves](#rotacionar-as-chaves); chaves por integração, com
  revogação, é o passo seguinte.

  O escopo `runtime` alcança: `/chat`, `/chat/stream`, `/analyze`,
  `POST /observability/scores`, `POST /observability/references`, o estado de uma
  conversa procedural (`GET /agents/{t}/procedures/{session_id}`) e
  `GET /integration/openapi.json` — o OpenAPI só dessas rotas, para quem integra — e
  `GET /agents/{t}/openapi.json` (o mesmo, de um agente só).
  Todo o resto exige `admin`. Com auth ligada, `/docs` e `/openapi.json` também
  exigem `admin` — publicar a superfície inteira da API para quem alcança a porta
  seria entregar o mapa antes da fechadura; para ler o OpenAPI, mande o header.
  `GET /health` e `GET /ready` ficam sempre abertas: são o healthcheck do
  container e a readiness de quem chama.
- **CORS (`CORS_ORIGINS`): quem precisa é só uma página no navegador, em outro domínio,
  chamando a API direto.** O console, a CLI, o MCP e os sistemas integrados não passam por
  isso (o console chama a API pelo próprio servidor). O padrão, com a variável vazia, é a lista
  do AgentOS: `http://localhost:3000`, `https://agno.com`, `https://www.agno.com`,
  `https://app.agno.com`, `https://os.agno.com` e `https://os-stg.agno.com` — é o que deixa o
  playground do Agno funcionar. Sem playground, `CORS_ORIGINS=none`; com um front seu, liste as
  origens (`https://painel.exemplo.com`), sem caminho. `*` é recusado na subida. CORS não
  substitui a chave: a página continua precisando mandá-la, e o preflight (`OPTIONS`) é a única
  coisa que passa sem ela.
- **Tools `python` não são uma sandbox.** O namespace é restrito (imports
  liberados, nomes perigosos bloqueados), mas isso barra erro e abuso
  acidental, não um autor mal-intencionado. Por isso vêm desligadas. Ligadas
  (`CUSTOM_PYTHON_TOOLS_ENABLED=true`), **chave admin = execução de código no
  container**, com acesso aos segredos dele. O MCP que roda no servidor (pelo SSH e
  por `/mcp`) não cria nem altera tools `python`; a CLI e o console, sim.
- **Use o profile `tls` fora da sua máquina.** A chave de API vai num header:
  sem HTTPS, ela e as mensagens do cliente trafegam em texto claro. Ver
  [HTTPS](operacao.md#https-profile-tls). O serviço em si fala HTTP — quem termina o TLS é
  o Caddy na frente.
- **Tools só alcançam endereços públicos.** O destino é resolvido e conferido
  antes de cada chamada, nos dois caminhos de rede (`kind="api"` e o `httpx` das
  tools Python). Libere um host interno legítimo em `TOOL_EGRESS_ALLOWLIST`.
  Limite conhecido: DNS rebinding — para isso, política de saída no ambiente.
- **Agentes e collections criados depois do boot** funcionam no `/chat` na
  hora, mas só aparecem nas rotas nativas do AgentOS (e no playground de
  os.agno.com) depois de um restart.
- **Mem0 hospedado** envia as mensagens dos usuários para os servidores do Mem0.
- **Langfuse self-hosted é pesado** (~16 GB recomendados). Por isso é opcional
  e desligado por padrão — o serviço registra as execuções sem ele.
