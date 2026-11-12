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

## Atualizando

O serviço e o console são duas imagens, e o console depende dos campos que o serviço devolve:
um console novo contra um serviço antigo quebra telas (ex.: a base de conhecimento). Por isso
reconstrua **as duas juntas**, no mesmo `up`, com todos os profiles que você usa:

```bash
git pull
KURO_BUILD_SHA=$(git rev-parse --short HEAD) \
  docker compose --profile ui up -d --build        # + --profile tls / ip / observability, se usar
uv run kuro health                                 # `version` do serviço
```

Leia antes a seção **Como atualizar** das [notas de versão](notas-de-versao.md); para uma
instalação em operação, siga o [roteiro de migração](migracao.md) (backup, o que conferir no
`.env`, como voltar atrás). `docker compose
build agent-service` sozinho (ou `up -d` sem `--profile ui`) deixa a imagem do console para trás.

**Conferir:** o rodapé da barra lateral do console mostra `console <hash> · serviço <versão>`. O
hash deve ser o do `git rev-parse --short HEAD` (sem `KURO_BUILD_SHA` aparece `dev`), e, se o
serviço for mais antigo que o mínimo que o console exige, ou não informar a versão, aparece
**Versões incompatíveis** (o motivo fica no tooltip). A versão mínima é a constante
`MIN_SERVICE_VERSION` em `frontend/src/lib/service-compat.ts`. Ela compara o número de versão do
serviço (`pyproject.toml`), que só sobe num lançamento: entre duas versões lançadas, o hash do
console é o que diz se as imagens saíram do mesmo commit.

## HTTPS (profile `tls`)

A chave de API viaja num header. Em HTTP puro ela vai em texto claro no fio,
junto com as mensagens do cliente — para um serviço chamado por outra
plataforma, isso não serve. O profile `tls` sobe um [Caddy](https://caddyserver.com)
na frente, que resolve certificado e renovação sozinho:

```bash
# no .env: KURO_API_DOMAIN=kuro.suaempresa.com e KURO_TLS_EMAIL=voce@suaempresa.com
docker compose --profile tls up -d
```

**Só sobe com a autenticação ligada.** O profile põe o serviço na internet; sem
`ADMIN_API_KEY`/`RUNTIME_API_KEY`, o `caddy` recusa começar e diz o que falta no log
(`docker compose logs caddy`). `KURO_TLS_ALLOW_OPEN=1` pula a verificação, só para teste local.

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

### Login do console (basic_auth)

O console não tem login próprio, e o servidor dele chama a API com a chave admin. Publicado sem
barreira, quem achasse o domínio do console teria acesso total: agentes, traces, conversas e
credenciais. Por isso o Caddy do profile `tls` pede usuário e senha (HTTP basic auth) no bloco do
console (`KURO_CONSOLE_DOMAIN`), e **só sobe com as duas variáveis definidas**, mesmo que você não
suba o profile `ui`.

1. Gere o hash da senha (bcrypt) com o próprio Caddy:

   ```bash
   docker run --rm caddy:2-alpine caddy hash-password --plaintext 'uma-senha-forte'
   ```

2. Ponha usuário e hash no `.env`, **com o hash entre aspas simples**:

   ```bash
   CONSOLE_BASIC_AUTH_USER=admin
   CONSOLE_BASIC_AUTH_HASH='$2a$14$...'   # o que o hash-password imprimiu, inteiro
   ```

3. `docker compose --profile tls up -d` (de novo, se já estava no ar). O navegador pede usuário e
   senha ao abrir o console; a senha é a do passo 1, não o hash.

**O `$` do hash.** O Compose interpola o `.env`: sem aspas, ou com aspas duplas, cada `$palavra`
do bcrypt vira uma variável vazia, o hash chega cortado e o `docker compose` avisa
`The "..." variable is not set`. Aspas simples desligam a interpolação. O escape `$$` só vale para
um valor escrito direto no `docker-compose.yml`, não no `.env`. Não ponha a senha em texto claro
na variável: ela precisa ser o hash.

**Sem as variáveis**, o `up` termina, mas o container do Caddy não sobe o proxy: ele sai com erro e
o `restart` tenta de novo a cada ~30 s, sem publicar nem a API nem o console. O motivo e o comando
para gerar o hash aparecem em `docker compose --profile tls logs caddy`. O mesmo acontece se o hash
não tiver cara de bcrypt (60 caracteres começando com `$2a$`), que é o sintoma das aspas
esquecidas. A checagem fica no container (`docker/caddy-tls.sh`), e não num `${VAR:?}` do compose,
porque o Compose interpola o arquivo inteiro antes de olhar os profiles: um `:?` ali faria até o
`docker compose up -d` sem profile falhar.

**A API não passa pelo basic_auth.** `/chat`, `/analyze`, `/mcp`, `/health` e o resto, no
`KURO_API_DOMAIN`, continuam só com a autenticação por chave (`ADMIN_API_KEY`/`RUNTIME_API_KEY`).
Também não passa o `127.0.0.1:3000` (`FRONTEND_BIND`), que só a própria máquina alcança. Para trocar
a senha, gere outro hash, troque no `.env` e rode `docker compose --profile tls up -d caddy`.
É uma barreira de curto prazo: um usuário e uma senha para todo mundo que usa o console.

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
| `ADMIN_API_KEY` / `RUNTIME_API_KEY` | — | chaves de API; **sem nenhuma o serviço não sobe** (salvo `ALLOW_OPEN=1`). Aceitam lista `nova,antiga` para [rotacionar](#rotacionar-as-chaves) |
| `ALLOW_OPEN` | `false` (`1` no `docker-compose.yml`) | `1` deixa subir **sem chave**, aberto a quem alcança a porta, com aviso no log. Só para a máquina de quem desenvolve; com chave, não faz diferença. Ver [Segurança](#segurança-e-limitações-atuais) |
| `KURO_API_DOMAIN` / `KURO_TLS_EMAIL` | `localhost` / — | domínio e e-mail do certificado (profile `tls`) |
| `KURO_PUBLIC_IP` / `KURO_IP_PORT` | descoberto / `58443` | IP e porta do HTTPS pelo IP (profile `ip`) |
| `KURO_PUBLIC_URL` | — | endereço do serviço visto de fora, se nenhum dos profiles acerta (o `kuro mcp-config` e o contrato de integração usam) |
| `AGENT_SERVICE_BIND` | `127.0.0.1:58000` | onde a API é publicada no host |
| `TRACE_STORE_BACKEND` | `db` | de onde `kuro runs` e `/observability/*` leem: `db` ou `langfuse` |
| `RUN_TIMEOUT_SECONDS` | `90` | tempo limite padrão de uma execução (o agente pode ter `timeout_seconds`); estourou, 504 |
| `MAX_CONCURRENT_RUNS` | `16` | execuções simultâneas por processo; acima disso, a chamada espera na fila |
| `QUEUE_MAX_SIZE` | `64` | chamadas esperando vaga, por processo; fila cheia = 503 com `Retry-After` |
| `TRACE_WRITE_QUEUE_MAX` | `1000` | gravações de trace (`runs`/`run_spans`) esperando o banco, por processo; fila cheia = a gravação é descartada e contada, a resposta não espera. Ver [gravação de trace](#gravação-de-trace) |
| `TRACE_WRITE_SHUTDOWN_SECONDS` | `5` | no encerramento, quanto esperar as gravações de trace pendentes; fique bem abaixo dos 10 s do `docker stop` antes do SIGKILL |
| `RUN_RETENTION_DAYS` | — (sem prazo) | prazo do trace store local, em dias; vazio ou `0` desliga. Ver [retenção e dados pessoais](#retenção-e-dados-pessoais) |
| `RUN_RETENTION_MODE` | `delete` | o que a retenção faz com o run vencido: `delete` (apaga com os dependentes) ou `anonymize` (mantém os números, apaga o conteúdo) |
| `TRACE_REDACT_KEYS` | — (nada) | chaves mascaradas na gravação do trace, separadas por vírgula (`cpf,telefone`): o valor vira `[redigido]`. Só por chave, nunca em texto livre |
| `RATE_LIMIT_PER_MINUTE` | — (desligado) | chamadas por minuto por chave de API no `/chat`, `/chat/stream` e `/analyze`; acima disso, 429 com `Retry-After`. Em memória, por processo. Ver [limite de requisições](#limite-de-requisições) |
| `QUEUE_MAX_WAIT_SECONDS` | `30` | espera máxima na fila antes do 503; `0` desliga a fila. Deixe abaixo do timeout de quem chama |
| `CORS_ORIGINS` | — (a lista do AgentOS) | páginas, em outro domínio, que podem ler as respostas da API no navegador; `none` fecha. Ver [segurança](#segurança-e-limitações-atuais) |
| `IDEMPOTENCY_TTL_HOURS` | `24` | por quanto tempo um `Idempotency-Key` no `/chat`/`/analyze` devolve a resposta guardada ([integração](integracao.md#repetir-sem-duplicar-idempotency-key)) |
| `AUX_MODEL_ID` / `AUX_MODEL_PROVIDER` | — (o do agente) | modelo barato para extrair memória e resumir a sessão |
| `MEMORY_CONTEXT_LIMIT` | `10` | quantas memórias de longo prazo (as mais recentes) entram no prompt |
| `MODEL_PRICES` | tabela embutida | JSON `{"modelo": [USD/1M entrada, USD/1M saída]}` para o custo estimado sem Langfuse |
| `GOOGLE_API_KEY` | — | chave do Gemini (provedor padrão) |
| `CREDENTIALS_ENCRYPTION_KEY` | — | chave Fernet que cifra as credenciais de modelo e os segredos das tools `api`; **obrigatória** para cadastrar chaves de modelo. Sem ela, os segredos de tools ficam em texto plano. Ver [segurança](#segurança-e-limitações-atuais) |
| `DEFAULT_MODEL_PROVIDER` / `DEFAULT_MODEL_ID` | `google` / `gemini-2.5-flash` | modelo de quem não define um |
| `DATABASE_URL` | localhost | sobrescrito dentro do compose |
| `POSTGRES_PASSWORD` | `postgres` | senha do Postgres do compose (o banco, e a URL de conexão do agent-service e do Langfuse). Só vale na criação do volume: ver [senha do Postgres](#senha-do-postgres) |
| `MAX_ATTACHMENT_MB` | `20` | limite por anexo |
| `MAX_INPUT_CHARS` | `200000` | teto do texto de entrada (`message` do `/chat`, `document` do `/analyze`) |
| `CUSTOM_PYTHON_TOOLS_ENABLED` | `false` | liga a execução de tools `python` |
| `TOOL_EGRESS_ALLOWLIST` | — | hosts internos que as tools podem alcançar (vazio = só endereços públicos) — vale também para a coleta de sites |
| `CORRELATION_METADATA_KEYS` | `ciclo_id` | chaves de `metadata` que ligam runs de um mesmo fluxo; a tela do run mostra os irmãos em ordem (vazio desliga) |
| `WEB_SOURCE_MAX_PAGES` | `500` | teto de páginas por site, qualquer que seja o `max_pages` pedido |
| `WEB_SOURCE_CONCURRENCY` | `3` | páginas processadas ao mesmo tempo por site (download + formatação) |
| `WEB_SOURCE_REQUEST_DELAY_MS` | `300` | intervalo mínimo entre requisições ao mesmo site |
| `WEB_SOURCE_MAX_FILE_MB` | `20` | teto de cada arquivo linkado (PDF, DOCX...) que a coleta baixa; as páginas têm 5 MB |
| `WEB_SOURCE_USER_AGENT` | `KuroBot/1.0 (...)` | como o coletor se apresenta (é o nome que o `robots.txt` enxerga) |
| `WEB_SOURCE_SCHEDULER` | `true` | ressincroniza os sites com `refresh` agendado (o próximo sync entra na fila de jobs) |
| `JOBS_ENABLED` | `true` | roda o worker da [fila de jobs](#fila-de-jobs) nesta réplica; desligado, ela só enfileira |
| `JOBS_CONCURRENCY` | `2` | jobs rodando ao mesmo tempo nesta réplica |
| `JOBS_POLL_SECONDS` | `5` | de quanto em quanto tempo o worker olha a fila quando nada o acordou |
| `JOBS_LEASE_SECONDS` | `60` | validade do lease de um job rodando (renovado a cada terço); processo morto libera o job depois disso |
| `JOBS_MAX_ATTEMPTS` | `3` | tentativas antes de o job virar `failed` |
| `JOBS_BACKOFF_SECONDS` | `30` | espera antes da 2ª tentativa; dobra a cada uma, até 1 hora |
| `JOBS_LISTEN` | `true` | `LISTEN/NOTIFY` do Postgres para o worker acordar na hora (o polling continua como reserva) |
| `MEM0_ENABLED` / `MEM0_API_KEY` | `false` / — | habilita agentes com `memory_backend: "mem0"` |
| `LANGFUSE_ENABLED` | `false` | liga o exportador — exige o extra `observability` e o profile de mesmo nome |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | valores de dev | conexão com o Langfuse |
| `LANGFUSE_TIMEOUT_SECONDS` | `20` | timeout do envio de traces |
| `AGENT_SERVICE_URL` (frontend) | — | endereço interno da API, só no servidor Next.js |
| `AGENT_SERVICE_PUBLIC_URL` (frontend) | — | endereço exibido nos exemplos de integração; vazio, vale o que o agent-service conhece (`KURO_PUBLIC_URL`, `KURO_API_DOMAIN`, profile `ip`) |
| `KURO_API_URL` / `KURO_JSON` / `KURO_NO_INPUT` (CLI) | `http://127.0.0.1:58000` | endereço, saída JSON e modo não interativo |

## Mais de um worker ou réplica

Parte do estado do serviço vive na memória de cada processo, não no banco:

- **Cache de agentes e tools.** O `Agent` montado e cada tool construída ficam guardados no
  processo. A cada requisição, uma consulta só ao banco traz a definição do agente e os carimbos
  (`updated_at`) da nota de feedback e das tools dele; se nada mudou, o agente em cache é usado,
  senão ele é remontado, reaproveitando as tools que não mudaram. Como a comparação é sempre com o banco, uma edição feita por
  qualquer processo vale na requisição seguinte de todos — não há invalidação a propagar. O custo
  é que cada processo monta o seu agente na primeira chamada depois de subir ou de uma edição.
- **Vagas de execução e fila de espera.** `MAX_CONCURRENT_RUNS` e `QUEUE_MAX_SIZE` são por
  processo: com N workers (ou réplicas), o serviço aceita até N × `MAX_CONCURRENT_RUNS` execuções
  ao mesmo tempo e N × `QUEUE_MAX_SIZE` chamadas esperando. Divida os valores por N se o limite
  que importa é o do provedor de LLM ou o da máquina. O 503 de fila cheia é decidido por processo,
  então uma réplica pode recusar enquanto outra ainda tem vaga — por isso o `Retry-After`.
- **Tools Python simultâneas** (32 por processo) e a lista de modelos dos provedores (`providers
  models`, cache de 15 min) também são por processo.

O que precisa ser coerente entre processos fica no Postgres: idempotência, estado dos
procedimentos (o 409 de sessão ocupada), versões da configuração e a [fila de jobs](#fila-de-jobs).
O MCP pela URL é a exceção: precisa de um worker só (ver [HTTPS](#https-profile-tls)).

## A subida: migração, seeds e várias réplicas

Importar `agent_service.main` não toca no banco: tudo acontece na subida (o lifespan do app),
nesta ordem:

1. **Configuração:** as origens de CORS e a autenticação (sem chave de API, só com
   `ALLOW_OPEN=1`). Errada, a subida para aqui, antes de qualquer acesso ao banco.
2. **Sob um lock** — `pg_advisory_lock(7743220957235996530)` (a chave é fixa,
   `MIGRATION_LOCK_KEY` em `migrations/__init__.py`), numa conexão só dele:
   1. **migração e seeds**;
   2. **o app:** o AgentOS lê os agentes e as collections do banco já migrado (e o Agno cria a
      extensão `vector`, as tabelas de vetores e as `agno_*` dele);
   3. **o que roda em background:** o MCP em `/mcp`, o acerto das fontes de site e o worker da
      [fila de jobs](#fila-de-jobs).

   O lock é solto quando a subida termina. Duas réplicas (ou `--workers N`) subindo juntas fazem
   isso uma de cada vez: a segunda loga `Esperando outra instância terminar de migrar o banco`
   e, quando pega o lock, acha o banco já migrado e segue. Sem isso, além da migração em dobro, o
   `CREATE EXTENSION/TABLE IF NOT EXISTS` do Agno em paralelo derrubava uma delas com
   `duplicate key`. O `uv run kuro-migrate` usa o mesmo lock.

Uma falha em qualquer passo aparece no log como o motivo seguido de
`Application startup failed. Exiting.`, e o processo sai (código 3). O uvicorn só abre a porta
depois da subida, então até lá uma conexão é recusada, como antes. Quem roda o uvicorn com
`--lifespan off` pula a subida inteira: toda requisição recebe 503 `O serviço ainda está subindo.` Um container que trava esperando o lock
é outra réplica migrando (ou uma sessão que segurou o lock): `SELECT * FROM pg_locks WHERE
locktype = 'advisory'` mostra quem.

## Imagem do serviço

O `Dockerfile` tem dois estágios: o `builder` (com `build-essential` e o `uv`) instala as
dependências em `/app/.venv`, e a imagem final copia só o `/app` para uma `python:3.12-slim`
limpa, sem compilador. Nenhuma biblioteca de sistema a mais é preciso em runtime (o
`psycopg[binary]` traz a própria libpq).

O processo roda como o usuário **`kuro`, uid/gid `10001`**, não como root. O código e o venv
são do root e só leitura para ele. O que o container escreve em disco:

- `/tmp`: arquivos temporários das tools;
- `/home/kuro` (o `HOME`): a sessão do `kuro chat` rodado no container (`~/.kuro/sessions.json`).
  Some quando o container é recriado, como antes;
- nenhum volume do compose: o agent-service só monta o `kuro_public` em modo leitura (o Caddy do
  profile `ip` grava nele com permissão 644). Os dados ficam no Postgres.

`docker compose exec agent-service kuro ...` continua funcionando, agora como o uid 10001. Se um
`docker-compose.override.yml` seu monta um volume ou pasta em que o serviço **escreve**, o dono
dele precisa ser o 10001, porque o volume já existente foi criado pelo root:

```bash
docker compose run --rm --no-deps --user root --entrypoint chown agent-service -R 10001:10001 /caminho/no/container
```

Para depurar como root: `docker compose exec --user root agent-service sh`.

## Senha do Postgres

`POSTGRES_PASSWORD` (padrão `postgres`, o valor de antes) é lida pelo serviço `postgres` e pelas
URLs de conexão do agent-service e do Langfuse, todas montadas pelo compose. Use uma senha que não
quebre a URL (sem `@ : / # ?`, nem `$`, que o compose interpreta), por exemplo
`python -c "import secrets; print(secrets.token_hex(24))"`. Rodando o serviço fora do Docker
(`uv run`), troque também a senha no `DATABASE_URL` do `.env`.

A imagem do Postgres só aplica `POSTGRES_PASSWORD` quando cria o volume (PGDATA vazio). Numa
instalação que já existe, mudar a variável sozinha deixa o agent-service sem conseguir conectar.
Troque a senha no banco **antes**:

```bash
docker compose exec postgres psql -U postgres -c "ALTER USER postgres PASSWORD 'a-senha-nova';"
# depois: POSTGRES_PASSWORD=a-senha-nova no .env
docker compose up -d     # recria o agent-service (e o Langfuse, se em uso) com a URL nova
```

## Limite de requisições

`RATE_LIMIT_PER_MINUTE=N` limita cada chave de API a N chamadas no `/chat`, `/chat/stream` e
`/analyze` (somadas) em qualquer janela de 60 s. Passou disso, a resposta é **429** com
`Retry-After` (os segundos até uma vaga abrir) e `{"detail", "error": "rate_limited"}`; a chamada
recusada não conta. Vazio ou `0`, o padrão, desliga. As outras rotas não são limitadas.

- **Por chave:** cada chave tem a sua cota (numa rotação, a nova e a antiga contam separado). A
  chave guardada na memória é um hash, nunca a chave. O console usa a chave admin para todo
  mundo que está nele, então divide uma cota só com a CLI e o MCP.
- **Serviço aberto** (sem chave configurada): conta por IP de quem conectou. Atrás do Caddy
  (profiles `tls` e `ip`) é o IP do proxy, e todo o tráfego cai numa cota só; o
  `X-Forwarded-For` é ignorado de propósito.
- **Por processo, em memória:** não é compartilhado entre workers ou réplicas (N processos = N
  vezes o limite) e zera num restart. É uma trava contra um cliente em loop gastando token, não
  uma cota contratual. Para cota entre réplicas, limite no proxy da frente.

## Gravação de trace

Cada execução é gravada no trace store local (`runs`, `run_spans`, `run_metadata`) depois da
resposta, por uma fila própria do processo: uma thread de escrita (`kuro-trace-writer`), separada
do executor que o resto do serviço usa, com até `TRACE_WRITE_QUEUE_MAX` gravações pendentes. Com o
banco lento a fila cresce; cheia, a gravação daquele run é **descartada** (a resposta do `/chat`
nunca espera nem falha por isso) e sai um aviso no log, no máximo um a cada 10 s. Ao desligar, o
serviço para de aceitar, espera até `TRACE_WRITE_SHUTDOWN_SECONDS` e loga quantas ficaram para trás.

Os contadores estão em `GET /health`, em `trace_writes`:

| Campo | O que conta |
|---|---|
| `pending` | na fila mais a que está sendo gravada agora |
| `max_pending` | o teto (`TRACE_WRITE_QUEUE_MAX`) |
| `written` | gravadas desde que o processo subiu |
| `dropped` | descartadas por fila cheia (ou chegadas durante o encerramento) |
| `failed` | erro do banco ao gravar (logado com o `run_id`; o run some do `kuro runs`) |

`dropped` ou `failed` subindo = runs faltando em `kuro runs` e nos dashboards: olhe o banco antes
de subir o teto. `pending` perto de `max_pending` é o aviso antes do descarte.

## Fila de jobs

O trabalho em background (hoje, a sincronização de sites) passa por uma fila no próprio Postgres,
a tabela `jobs` — sem broker. Cada réplica com `JOBS_ENABLED=true` roda `JOBS_CONCURRENCY` tarefas
que pegam o próximo job com `FOR UPDATE SKIP LOCKED`, então várias réplicas dividem a fila sem
rodar o mesmo job duas vezes. Quem roda um job renova um lease (`locked_until`); se o processo
morrer, o lease vence e o job volta para a fila (com backoff) até `JOBS_MAX_ATTEMPTS`. Agendar é
enfileirar com `run_after` no futuro: o `refresh` de um site é o próximo job, gravado na mesma
transação que fecha o atual. Ao desligar, os jobs em andamento voltam para a fila sem gastar
tentativa.

```bash
uv run kuro jobs list --status failed     # contagem por status e os jobs (GET /jobs)
uv run kuro jobs show <id>                # payload, tentativas, lease e o último erro
uv run kuro jobs retry <id>               # failed/cancelled de volta na fila, tentativas zeradas
uv run kuro jobs cancel <id>              # para um job na fila ou rodando (em qualquer réplica)
```

O status que importa ao usuário continua no recurso: a fonte de site mostra `running`, o
progresso e o erro como antes. `/jobs` é para o operador destravar (escopo `admin`).

## Retenção e dados pessoais

### O que fica guardado, e onde

| Onde | O que tem de conversa | Prazo |
|---|---|---|
| `runs` | a mensagem montada (`message`), a fala pura (`input_message`), os eventos e blocos de contexto da entrada estruturada (`input_events`, `input_context`) e a resposta (`output`) | `RUN_RETENTION_DAYS` |
| `run_spans` | na raiz, a entrada do run: `message`, `dependencies` e a lista de anexos (nome, tipo, tamanho; o conteúdo do anexo não); em cada tool, os argumentos, o resultado e, se falhou, o começo do resultado em `status_message`; na busca na base, a pergunta e o texto dos trechos (`metadata.references`); num procedural, o `state` com os campos coletados | `RUN_RETENTION_DAYS` |
| `run_metadata`, `run_scores`, `run_references`, `chunk_feedback` | a correlação mandada por quem chama, os votos e seus comentários, a decisão do legado (shadow) e as marcações de trecho com a pergunta da busca | `RUN_RETENTION_DAYS` |
| `agno_sessions` e `agno_runs` (sessões do Agno) | o histórico que volta como contexto nos turnos seguintes: cada mensagem, resposta e **anexo** | sem prazo — `kuro sessions delete <id>` apaga uma |
| memórias (`agno_memories`, Mem0) | fatos extraídos sobre o usuário, com `memory_backend` diferente de `none` | sem prazo |
| `procedure_runs` | o estado de cada conversa de um agente procedural: os campos coletados (CPF, nome...) e a última resposta | sem prazo |
| `idempotency_keys` | a resposta inteira de um `/chat`/`/analyze` com `Idempotency-Key` | `IDEMPOTENCY_TTL_HOURS` (24 h; as vencidas são apagadas sozinhas) |
| `agent_feedback_notes` e versões | as regras escritas a partir do feedback, que podem citar a conversa | sem prazo |
| Langfuse (se ligado) | uma cópia dos traces, inclusive o texto mandado ao modelo | o do próprio Langfuse (configure lá) |

### Retenção: `RUN_RETENTION_DAYS`

Desligada por padrão. Com `RUN_RETENTION_DAYS=90`, um job da [fila de jobs](#fila-de-jobs)
(`kind` `trace_retention`) trata os runs com início há mais de 90 dias, roda de novo a cada 6 h e
aparece em `kuro jobs list`. Vale para as tabelas da primeira parte da tabela acima — o trace store
local — e para nada mais:

- `RUN_RETENTION_MODE=delete` (padrão): apaga o run e o que depende dele (`run_spans`,
  `run_metadata`, `run_scores`, `run_references`, `chunk_feedback`). Some de `kuro runs`, dos
  dashboards e das estatísticas. **Exporte antes** o que quiser guardar: as referências do shadow
  (`kuro runs export`) e as marcações de trecho `relevant` (o gabarito do eval de RAG).
- `RUN_RETENTION_MODE=anonymize`: mantém as linhas — tokens, custo, latência, status, datas,
  agente, versões, tools chamadas e falhas, votos, `user_id`, `session_id` e a `metadata` — e apaga o
  conteúdo: `message`, `input_message`, `input_events`, `input_context`, `output`, a entrada e a
  saída de todos os spans (com `dependencies` e anexos), o `status_message` das tools, a pergunta e
  o texto dos trechos em `metadata.references`, o comentário dos votos e a referência do shadow; a
  pergunta das marcações de trecho vira `[removido]`. O run ganha a metadata
  `retention=anonymized` (`kuro runs list --meta retention=anonymized`). `user_id`, `session_id` e a
  `metadata` de correlação são identificadores e **ficam**: se forem dado pessoal, mascare-os na
  origem ou use `delete`.

Trabalha em lotes de 500 runs, uma transação curta por lote, sem travar a tabela. Com várias
réplicas roda **um** de cada vez: o job tem uma `dedupe_key` fixa e a fila só aceita um ativo por
chave. Rodar de novo não refaz nada (os apagados sumiram; os anonimizados têm o marcador). Trocar
`delete` por `anonymize` não traz nada de volta; trocar `anonymize` por `delete` apaga também os
já anonimizados. Desligar (`RUN_RETENTION_DAYS=` e reiniciar) tira da fila o job agendado. Precisa
de pelo menos uma réplica com `JOBS_ENABLED=true`.

### Redação na gravação: `TRACE_REDACT_KEYS`

Com `TRACE_REDACT_KEYS=cpf,telefone`, o trace grava `"[redigido]"` no lugar do valor de toda chave
`cpf` ou `telefone` (comparação exata do nome, sem diferenciar maiúsculas), em qualquer
profundidade, em:

- `dependencies` e os outros campos da entrada do run (span raiz);
- argumentos e resultados das tools, inclusive o resultado que chega como texto JSON (o corpo da
  resposta de uma tool de API) e o `status_message` de uma tool que falhou;
- a metadata dos spans, o `state` de um procedural e a `metadata` do run (pela chave).

Só o que é **gravado** muda: o modelo recebe as `dependencies` reais e as tools são chamadas com o
valor real. O que já estava gravado não é reescrito. Limites:

- **Não é varredura de texto livre.** O CPF que o contato digita na mensagem fica em `message`,
  `input_message`, `input_events`, `output` e em qualquer texto que não seja JSON. Para isso, a
  retenção.
- O `kuro runs export` e as rules com `{"$dep": ...}` no eval passam a ver `[redigido]` nas chaves
  mascaradas.
- Com o Langfuse ligado, o cliente dele recebe o mesmo mascaramento (`mask` para a entrada e a saída
  que o Kuro envia; `mask_otel_spans` para os spans do instrumentor do Agno: atributo cujo nome
  termina na chave e atributo JSON). O texto das mensagens enviadas ao modelo, que o instrumentor
  também exporta, **não** é mascarado — e as `dependencies` vão no contexto do modelo. Se isso
  importa, deixe o Langfuse desligado ou configure a retenção dele.
- As sessões do Agno, as memórias e o `procedure_runs` não passam por aqui.

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
  `ADMIN_API_KEY` e `RUNTIME_API_KEY` no `.env`. Aberto, quem alcança a porta lê
  toda conversa que passou por ali (`/observability/runs`, `/sessions`), reescreve
  o prompt de um agente em produção e lê as credenciais de modelo. Por isso, **sem
  chave nenhuma o serviço não sobe**: o log diz o que definir. Rodar aberto de
  propósito pede `ALLOW_OPEN=1`; aí ele sobe com um aviso no log, o `/health`
  responde `"auth": "disabled"` e o `kuro health` mostra em vermelho.

  O `docker-compose.yml` liga o `ALLOW_OPEN` por padrão, para o `docker compose up`
  de quem desenvolve funcionar sem chave: a porta só é publicada no loopback. Os
  profiles que publicam para fora (`tls` e `ip`) conferem o `/health` e recusam subir
  com a autenticação desligada, então não herdam isso em silêncio. Duas coisas que
  não são conferidas: um `AGENT_SERVICE_BIND` fora do loopback (defina as chaves
  antes de mudá-lo) e um proxy seu na frente. Para desligar no compose,
  `ALLOW_OPEN=0` no `.env` (vazio conta como não definido e vira `1`). Fora do
  compose (`uv run uvicorn`, outra orquestração), o padrão é fechado.

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
- **Segredos em repouso: `CREDENTIALS_ENCRYPTION_KEY`.** A mesma chave Fernet cifra as
  credenciais de modelo e os segredos das tools `kind="api"`: `auth.token`, `auth.value`,
  `auth.password` e o valor dos headers com nome de credencial (`Authorization`, `X-Api-Key`,
  `X-...-Token`, `Cookie`...), valor a valor, guardados como `enc:v1:<token Fernet>`. O resto da
  config fica legível. A leitura pela API mascara esses campos; quem decifra é a chamada da tool,
  na hora de montar a requisição.
  - **Sem a chave**, credencial de modelo é recusada (503 `encryption_not_configured`), mas tool
    com segredo é gravada em texto plano, como antes desta versão, com um aviso no log: o
    `.env.example` traz a chave vazia e recusar quebraria quem já cadastra tools assim. `GET
    /tools/{nome}` mostra `secrets_at_rest: "plaintext"`; depois de configurar a chave, regrave a
    tool (`kuro tools get <tool> --editable > t.json && kuro tools apply -f t.json`) para cifrar.
  - **Guarde a chave** junto com o backup do banco: um backup sem ela não decifra nada. Trocar ou
    perder a chave deixa cada tool com segredo cifrado respondendo "Erro de configuração da tool: o
    segredo em ... não pôde ser decifrado" (`failure: "config"`), sem chamar o destino, até o
    segredo ser cadastrado de novo. Não há rotação com duas chaves ao mesmo tempo.
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
