# Servidor MCP do Kuro

O servidor MCP do Kuro expõe as operações da CLI `kuro` como tools [MCP](https://modelcontextprotocol.io).
Um agente de IA (Claude Code, ou qualquer cliente MCP) cria, testa, avalia e promove agentes do
Kuro chamando tools tipadas, sem montar comandos de shell nem interpretar o stdout. O MCP fala com
o serviço pela API, o mesmo caminho da CLI e do console, e não acessa o banco.

## Conectar

Três jeitos, do mais simples ao que exige mais do servidor:

| Jeito | Quando | Precisa de |
|---|---|---|
| [Pelo SSH](#pelo-ssh-kuro-connect) (`kuro connect`) | Você entra no servidor por SSH. **O padrão** | SSH e o uv na sua máquina |
| [Pela URL](#pela-url-mcp) (`/mcp`) | O serviço tem domínio com HTTPS (profile `tls`) | Nada além do Claude Code |
| [Com o `kuro-mcp` na sua máquina](#com-o-kuro-mcp-na-sua-máquina) | Sem SSH nem domínio; ou o serviço local | O uv, e a porta aberta no firewall do provedor |

### Pelo SSH: `kuro connect`

Na sua máquina, um comando (troque `root@meu-servidor` pelo seu acesso SSH):

```bash
uvx --from "agent-service[mcp] @ git+https://github.com/Arthur-Marques-IA/microservice_agents" kuro connect root@meu-servidor
```

O MCP passa a rodar **dentro do container** do serviço, chamado pelo SSH, e fala com o serviço por
dentro:

```text
Claude Code  ──ssh──▶  docker exec -i <container> kuro-mcp  ──▶  agent-service (mesmo servidor)
```

Por isso não precisa de porta aberta, de certificado nem da chave de API na sua máquina: o
container já tem a chave admin, e o firewall do provedor deixa o SSH passar. O comando confere cada
passo antes do próximo:

1. entra pelo SSH sem senha, porque o Claude Code não tem como digitar nada. Se não entrar, diz
   qual dos três casos é:
   - **o servidor pede senha:** oferece criar uma chave SSH e instalá-la (pede a senha uma última
     vez). Com `--passphrase`, a chave nova nasce com senha de proteção e vai para o ssh-agent;
   - **a sua chave tem senha de proteção (passphrase) e não está num ssh-agent:** manda carregá-la
     (`ssh-add`; no Windows, o serviço `ssh-agent` ligado), em vez de criar outra chave;
   - **o servidor só aceita chave** e nenhuma das suas foi aceita: pôr a chave pública no
     `authorized_keys` pelo console do provedor, ou apontar a certa com `-i`. Pedir senha não adiantaria;
2. acha o container do `agent-service` (`docker ps` pelo rótulo do compose). Com mais de um Kuro na
   máquina, escolha com `--container`;
3. confere se a imagem já tem o MCP. Imagem de antes desta versão: `docker compose up -d --build`;
4. sobe o MCP com o mesmo comando que o Claude Code vai usar e chama a tool `health`;
5. só então registra no Claude Code (`claude mcp add`, escopo `user`). Se já existir um servidor
   `kuro`, pergunta antes de trocar (ou `--yes`).

| Opção | Para quê |
|---|---|
| `-p 2222`, `-i ~/.ssh/chave`, `-o Opção=valor` | Porta, chave e opções do ssh. Um alias do `~/.ssh/config` também serve como destino |
| `--container nome` | O container, quando há mais de um Kuro rodando |
| `--name`, `--scope` | Nome e escopo do registro no Claude Code (padrão `kuro`, `user`) |
| `--print` | Testa, mas não registra: mostra o comando e o `.mcp.json` para outro cliente (Claude Desktop, Cursor) |
| `--passphrase` | Se precisar criar a chave SSH, cria com senha de proteção e a carrega no ssh-agent |

O usuário do SSH precisa poder usar o Docker: `root`, ou alguém no grupo `docker`.

O comando registrado leva keepalive (`ServerAliveInterval=15`, `ServerAliveCountMax=3`,
`ConnectTimeout=15`). Com a rede caída (troca de Wi-Fi, notebook que dormiu), o ssh desiste em
cerca de 45 s em vez de deixar a tool pendurada até o timeout do cliente. O Claude Code não religa
sozinho um servidor que caiu: rode `/mcp` e reconecte o `kuro`. Quem registrou antes desta versão
ganha o keepalive rodando o `kuro connect` de novo.

### Pela URL: `/mcp`

O próprio serviço atende o MCP em `/mcp` (Streamable HTTP), exigindo a chave **admin**. Com um
domínio e o HTTPS do profile `tls` (certificado do Let's Encrypt), não precisa instalar nada:

```bash
claude mcp add --transport http kuro https://kuro.suaempresa.com/mcp -s user --header "Authorization: Bearer sua-chave-admin"
```

`kuro mcp-config --show-key`, no servidor, imprime esse comando pronto quando há domínio no `.env`.

Só com certificado público: o Claude Code não confia na CA própria do profile `ip`, e por HTTP puro
a chave iria em texto claro. O serviço precisa rodar com **um worker** só (o padrão do compose),
porque a confirmação de remover e promover volta para a mesma sessão.

**Sem domínio próprio**, um DNS curinga dá o certificado público do mesmo jeito:
`KURO_API_DOMAIN=203-0-113-10.sslip.io` (o IP da VPS com hífens) e
`docker compose --profile tls up -d`. Precisa das portas 80 e 443 livres e abertas no firewall do
provedor; as condições estão em [Sem domínio próprio: sslip.io](operacao.md#sem-domínio-próprio-sslipio).
Depois, o `kuro mcp-config` imprime o `claude mcp add --transport http` com
`https://203-0-113-10.sslip.io/mcp`.

### Com o `kuro-mcp` na sua máquina

O `kuro-mcp` roda na sua máquina, por stdio, e fala com a URL do serviço:

```text
Claude Code  ──stdio──▶  kuro-mcp (sua máquina)  ──HTTPS──▶  agent-service
```

No servidor, `kuro mcp-config` imprime o comando pronto, em bash e PowerShell:

```bash
docker compose exec agent-service kuro mcp-config --show-key
```

O endereço sai sozinho, nesta ordem:

| No servidor | Endereço que o MCP usa |
|---|---|
| `KURO_PUBLIC_URL=https://...` no `.env` | Esse, como está |
| `KURO_API_DOMAIN=kuro.suaempresa.com` no `.env` (profile `tls`, Let's Encrypt) | `https://kuro.suaempresa.com` |
| O profile `ip` de pé (`docker compose --profile ip up -d`) | `https://<ip-público>:58443`, com o certificado da CA para salvar |
| Nenhum dos três | `http://127.0.0.1:58000`, que só serve para um MCP rodando no próprio servidor |

`--url` sobrepõe tudo isso. Com o profile `ip`, a saída traz também o comando que salva o
certificado da CA em `~/.kuro/`; ele chega pela sua sessão SSH, então é o autêntico. A porta 58443
precisa estar aberta no **firewall do painel do provedor**, que fica fora da VPS: se o MCP diz que
a porta "não respondeu", é ele. Sem acesso ao painel, use o SSH.

A conexão usa as mesmas variáveis da CLI:

| Variável | Para quê |
|---|---|
| `KURO_API_URL` | Endereço do serviço, ex.: `https://kuro.suaempresa.com`. Padrão: `http://127.0.0.1:58000` |
| `KURO_API_KEY` | Chave de escopo **admin**. Sem ela, quase toda tool responde 401 |
| `KURO_CA_BUNDLE` | `.pem` da CA, se o certificado do serviço vem de uma CA própria |
| `KURO_INSECURE=1` | Não valida o certificado. Só para teste local com certificado autoassinado |
| `KURO_TIMEOUT` | Timeout das requisições em segundos (padrão 300 — um `eval` longo leva tempo) |

A partir de um clone deste repositório, com o serviço local:

```bash
claude mcp add kuro --env KURO_API_URL=http://127.0.0.1:58000 --env KURO_API_KEY="$KURO_API_KEY" -- uv --directory "$PWD" run kuro-mcp
```

Ou num `.mcp.json` do projeto onde seus agentes trabalham. A chave fica fora do arquivo: o Claude
Code expande `${KURO_API_KEY}` a partir do seu ambiente, então o `.mcp.json` pode ir para o git.

```json
{
  "mcpServers": {
    "kuro": {
      "command": "uv",
      "args": ["--directory", "/caminho/para/Microservice_Agent", "run", "kuro-mcp"],
      "env": {
        "KURO_API_URL": "https://kuro.suaempresa.com",
        "KURO_API_KEY": "${KURO_API_KEY}"
      }
    }
  }
}
```

### Conferir

Abra uma sessão nova do Claude Code e peça "use a tool health do kuro": ela devolve a versão do
serviço, se a autenticação está ligada e os provedores com credencial. Uma falha de conexão diz
para onde tentou ir. O `claude mcp list` mostrar "Connected" só quer dizer que o processo do MCP
subiu; quem confirma que ele alcança o serviço é a tool `health`.

## O que dá para fazer

| Área | Tools |
|---|---|
| Diagnóstico e modelos | `health`, `providers_list`, `provider_models`, `credentials_list`, `credentials_test` |
| Agentes | `agents_list`, `agent_get`, `agent_apply`, `agent_set`, `agent_versions`, `agent_revisions`, `agent_integration`, `agents_export`, `agent_rollback`\*, `agent_promote`\*, `agent_delete`\* |
| Executar | `chat`, `analyze`, `eval`, `tool_invoke` |
| Feedback | `feedback_show`, `feedback_versions`, `feedback_send`, `feedback_remove`, `feedback_rollback`, `feedback_clear`\* |
| Tools | `tools_list`, `tool_get`, `tool_catalog`, `tool_apply`, `tool_invoke`, `tool_delete`\* |
| Execuções | `runs_list`, `run_show`, `runs_stats`, `runs_overview`, `runs_sessions`, `run_score` |
| Modo shadow | `runs_agreement`, `runs_reference`, `runs_export` |
| Feedback por trecho do RAG | `run_chunk_feedback`, `chunk_feedback_list`, `chunk_feedback_status`, `chunk_feedback_flagged`, `chunk_feedback_export` |
| Conversas | `sessions_list`, `session_show`, `session_delete`\* |
| Bases de conhecimento | `collections_list`, `collection_get`, `collection_embedders`, `collection_create`, `collection_set`, `collection_versions`, `collection_docs`, `collection_search`, `collection_add`, `collection_edit_doc`, `collection_doc_revisions`, `collection_retest`, `collection_reindex`, `collection_rm_doc`\*, `collection_add_site`, `collection_sources`, `collection_sync_site`, `collection_rm_site`\* |

\* Pede confirmação ao usuário (ver abaixo).

O fluxo seguro para mudar um agente em produção é o mesmo da CLI, e o servidor o descreve ao
cliente nas instruções de conexão:

1. `agent_promote(source="r8", to="r8-draft")` cria o draft como cópia da produção;
2. `agent_set` / `agent_apply` no draft;
3. `eval(agent_type="r8-draft", cases_file="casos.jsonl", compare_with="r8")` — veja `verdict.ok`;
4. `agent_promote(source="r8-draft", to="r8")`, que pede confirmação.

No modo shadow, o dataset do passo 3 sai das próprias execuções, também sem deixar o MCP:

1. `runs_agreement(agent_type="r8")` mostra onde o agente discorda do legado, campo a campo;
2. `runs_reference(run_id, reference={...})` grava (ou corrige) a decisão de referência de um run;
3. `runs_export(agent_type="r8")` devolve os runs com referência como casos `{id, input, dependencies,
   expected}`, prontos para `eval(cases=...)`. Nenhum caso vem cortado: o que não cabe na resposta
   fica de fora e `omitted` diz quantos (filtre com `since`/`agent_version`, ou use `kuro runs export`).

## Confirmação do usuário

Toda operação que na CLI exige `--yes` exige, aqui, **confirmação da pessoa**: remover agente,
tool, conversa ou documento, restaurar uma versão do prompt, promover e zerar a nota de feedback.
O servidor pede pelo próprio cliente MCP (*elicitation*), que mostra a pergunta à pessoa e devolve
a resposta. Recusar ou cancelar não envia nada ao serviço.

Não existe parâmetro que o modelo possa preencher para se autoconfirmar. Se o cliente MCP não
suporta elicitation, a tool falha e devolve o comando equivalente da CLI (ex.:
`kuro agents delete suporte --yes`), para a própria pessoa rodar no terminal.

Editar um agente (`agent_set`, `agent_apply`), uma tool (`tool_apply`) ou as regras de feedback não
pede confirmação, como na CLI: tem histórico e versão para voltar. Isso inclui editar direto o
agente de produção, sem passar pelo draft. Essas tools levam `destructiveHint`, porque sobrescrevem
o que existia. As de leitura levam `readOnlyHint`, e só `run_score` e `collection_add`, que apenas
acrescentam, ficam sem nenhuma das duas. `collection_add_site` aceita `include_files` (padrão: sim — os PDF, DOCX, PPTX e XLSX linkados no
escopo entram, lidos sem formatador e com a página em cada trecho). `collection_add_site` e
`collection_sync_site` levam também
`openWorldHint` (saem para a web), e a segunda `destructiveHint` (troca e remove páginas da base). É por essas anotações que o cliente MCP decide o que
pede permissão antes de chamar.

## Diferenças em relação à CLI, de propósito

- **Testes são `dry_run` por padrão.** `chat`, `analyze`, `tool_invoke` e `eval` mandam
  `dry_run: true`, e as tools do agente recebem `X-Kuro-Dry-Run: true`. Um agente testando outro
  não deveria fechar um acordo de verdade por engano. Passe `dry_run=false` quando quiser o efeito.
- **Chave de modelo não passa pelo MCP.** Cadastrar ou trocar uma credencial colocaria a chave no
  contexto do modelo. Use `kuro credentials add` ou o console; `credentials_list` só lista.
- **A sessão de chat é explícita.** A CLI guarda a conversa por agente em `~/.kuro/sessions.json`;
  o MCP não mexe nesse arquivo, para não atropelar as suas conversas. `chat` sem `session_id`
  começa uma conversa nova e devolve o `session_id`; repasse-o para continuar e para
  `feedback_send`. O `user_id` do `chat` é `mcp` (o da CLI é `cli`); `sessions_list`,
  `session_show` e `session_delete` sem `user_id` olham as conversas de todos, e `sessions_list`
  pagina (`page`, com `meta.total_pages`).
- **Os testes ficam de fora das listas por padrão.** `runs_list`, `runs_overview` e
  `procedure_funnel` tiram os runs em `dry_run`, para a visão de produção não misturar testes.
  Como o `chat` daqui é `dry_run`, o run que ele acabou de criar se acha por `run_show(run_id)`,
  por `runs_list(session_id=...)` (filtrar pela conversa inclui os testes) ou com
  `include_tests=true`. `runs_sessions` e `runs_stats` não separam os testes.
- **Anexos e correlação.** `chat` e `analyze` aceitam `attachments` (`[{url | content_base64,
  mime_type, filename}]`) e `metadata`, como o `/chat` e o `/analyze`. Prefira `url`: o base64
  passa inteiro pelo contexto do modelo. `analyze` aceita só anexos, sem `document`.
- **Respostas com teto.** Listas vêm resumidas e textos longos (traces, transcrições) são cortados
  em 4000 caracteres com um aviso, para não estourar o contexto do modelo. Além disso, a resposta
  inteira tem teto de ~30 mil caracteres: passou disso (um `run_show` com dezenas de spans, por
  exemplo), textos e listas são cortados até caber e a resposta traz `truncated: true`.
  `agent_versions` lista as versões resumidas (tamanho e começo do texto); `version=N` traz o
  texto inteiro de uma. `agent_get(editable=true)` nunca é cortado, porque volta no `agent_apply`.
  O `eval` devolve só os casos que falharam, junto com o resumo.
- **Cada parâmetro tem descrição** no schema da tool, com os valores aceitos e um exemplo.
- **Tools `kind="python"` só pela CLI ou pelo console** quando o MCP roda no servidor (SSH e `/mcp`):
  o código dela executa dentro do processo do serviço, ao alcance dos segredos do container, e
  `tool_apply` recusa criar ou alterar uma. Com o `kuro-mcp` na sua máquina, funciona como antes.
- **Arquivos por caminho, só com o `kuro-mcp` na sua máquina.** `analyze(document_file=...)`,
  `eval(cases_file=..., rules_file=...)` e `collection_add(file=...)` leem da máquina onde o MCP
  roda. Pelo SSH e pela URL ele roda no servidor, então esses parâmetros são recusados: o caminho
  seria do container, e ler o `.env` dele entregaria os segredos do serviço a quem só tem a chave
  admin. Mande o conteúdo em `document`, `cases`, `rules` ou `text`.

## Desenvolvimento

O servidor fica em `src/agent_service/mcp_server.py` e usa o mesmo cliente HTTP da CLI
(`agent_service/cli/client.py`). O `/mcp` o monta em `src/agent_service/api/mcp_routes.py`, e o
`kuro connect` fica em `src/agent_service/cli/connect.py`. Os testes ligam um cliente MCP a uma API
falsa (`httpx.MockTransport`), sem serviço no ar: em memória (`tests/test_mcp_server.py`), por HTTP
num uvicorn de verdade (`tests/test_mcp_http.py`), e o `kuro connect` com `ssh` e `claude` falsos
(`tests/test_connect.py`):

```bash
uv run pytest tests/test_mcp_server.py tests/test_mcp_http.py tests/test_connect.py
```

Para ver as tools num cliente de verdade, o MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run kuro-mcp
```
