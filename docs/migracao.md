# Migrando uma instalação em operação

Roteiro para levar um Kuro que está no ar (a partir da `0.2.0`) para o código atual. As mudanças
uma a uma estão nas [notas de versão](notas-de-versao.md), seção **0.3.0**; aqui está a
ordem de fazer, o que conferir e o que acontece com o que já está gravado.

Resumo do risco: o contrato do `/chat` e do `/analyze` não mudou (todo campo novo é opcional), as
migrações só acrescentam colunas e cifram segredos, e nenhuma conversa é apagada. O que pode
impedir o serviço de subir ou mudar o comportamento de uma tool está na etapa 2.

## 1. Antes de mexer

```bash
uv run kuro --json health                       # anote version, auth e model_credentials
docker compose exec -T postgres pg_dump -U postgres -Fc agent_service > kuro-antes.dump
cp .env .env.antes
git rev-parse HEAD > commit-antes.txt           # para voltar a imagem, se precisar
```

O dump é o caminho de volta (ver [Voltar atrás](#6-voltar-atrás)): as migrações novas não têm
comando pronto de downgrade, e a imagem antiga não sobe com o banco migrado.

## 2. O que conferir no `.env` e nas tools

| Conferir | Por quê | Como |
|---|---|---|
| `ADMIN_API_KEY` e `RUNTIME_API_KEY` definidas | Sem chave nenhuma, o serviço **não sobe mais** fora do compose. No compose, `ALLOW_OPEN` vem ligado e ele sobe aberto: em produção, as chaves precisam existir | `kuro health` mostra `auth: enabled` |
| `CREDENTIALS_ENCRYPTION_KEY` definida **e guardada** | A migração `0017` cifra `auth` e os headers de credencial das tools `api` com ela. Perder ou trocar a chave depois faz as tools com segredo responderem "erro de configuração" | `kuro health` mostra `model_credentials: enabled`. Guarde a chave fora do servidor |
| Profile `tls` com console: `CONSOLE_BASIC_AUTH_USER` e `CONSOLE_BASIC_AUTH_HASH` | O Caddy do profile `tls` recusa subir sem o login do console, **e a API também fica sem HTTPS** até definir | [Login do console](operacao.md#login-do-console-basic_auth) |
| Tools que falam com `100.64.0.0/10` (CGNAT, Tailscale) | A trava de egress passou a recusar essa faixa | Libere o host em `TOOL_EGRESS_ALLOWLIST` |
| Tools `kind="python"` | Atributo iniciado por `_`, `import a.b` e nomes privados de módulo passam a ser recusados | `kuro --json tools list`; se houver alguma, teste com `tools invoke` depois de subir |
| `docker-compose.override.yml` (rede externa, allowlist) | Continua valendo; confira só se algum volume que **o serviço escreve** foi montado por você | A imagem roda como uid `10001`: `chown -R 10001:10001` nesse volume |
| Quem lê `kuro sessions show --json`, `session_show` (MCP) ou `/observability/sessions/{id}/transcript` | O formato mudou (ver [sessões antigas](#5-o-que-acontece-com-as-sessões-já-gravadas)) | Ajuste o script ou a automação que lê esses campos |

Variáveis novas, todas opcionais e desligadas por padrão: `RATE_LIMIT_PER_MINUTE`,
`RUN_RETENTION_DAYS` / `RUN_RETENTION_MODE`, `TRACE_REDACT_KEYS`, `TRACE_WRITE_QUEUE_MAX`,
`POSTGRES_PASSWORD` (o padrão é a senha de antes). Não defina nenhuma delas na migração: ligue
depois, uma de cada vez. A tabela completa está em [Configuração](operacao.md#configuração).

## 3. Atualizar

```bash
git pull
KURO_BUILD_SHA=$(git rev-parse --short HEAD) \
  docker compose --profile ui up -d --build      # + --profile tls / ip / observability, se usar
```

O serviço e o console sobem juntos, de propósito: um console novo contra um serviço antigo quebra
telas. Na subida, sob um lock, rodam as migrações que faltarem (até a `0017`), os seeds e a
montagem do app; a porta só abre depois. A primeira subida pode levar alguns minutos.

O que as migrações fazem com os dados:

- `0016`: acrescenta `input_message`, `input_events` e `input_context` em `runs`, vazias (`NULL`)
  nos runs que já existem. Nada é reescrito.
- `0017`: cifra os segredos das tools `api` já gravados. Sem a `CREDENTIALS_ENCRYPTION_KEY` ela
  não falha: deixa em texto plano e avisa no log.

## 4. Conferir que deu certo

```bash
uv run kuro --json health          # auth: enabled; trace_writes com dropped: 0 e failed: 0
uv run kuro --json tools list      # nas tools com segredo, secrets_at_rest: "encrypted"
uv run kuro --json chat <agente> -m "teste" --dry-run     # uma chamada de ponta a ponta
uv run kuro --json runs list -n 3                         # o run de teste foi gravado
docker compose logs --tail 50 agent-service               # sem "Application startup failed"
```

No console, o rodapé da barra lateral mostra `console <hash> · serviço <versão>`: o hash deve ser
o do commit que você subiu. Abra uma sessão antiga e uma tool com segredo (`tools invoke` com
`--dry-run`) antes de liberar o tráfego.

Se o servidor reiniciar, os três containers do núcleo voltam sozinhos: o `postgres` ganhou
`restart: unless-stopped`. Antes, só o serviço e o console voltavam, e o serviço ficava
reiniciando em laço com `failed to resolve host 'postgres'`.

## 5. O que acontece com as sessões já gravadas

Nada é apagado nem reescrito, e **não há retrocarga**: o que foi gravado no formato antigo fica
como foi gravado.

- **Conversa em andamento.** O histórico da sessão (o que o agente lembra) fica nas tabelas do
  Agno e não muda. Uma chamada só com `message`, como a integração faz hoje, se comporta
  exatamente como antes; a mesma `session_id` continua a conversa.
- **Console, tela da sessão.** Um turno antigo (ou qualquer turno novo que ainda chegue só com
  `message`) aparece como um cartão **"entrada não estruturada"**, recolhido, com o texto original
  inteiro. O console deixou de tentar recortar esse texto pelos marcadores (`FALA DO CONTATO`,
  `[ALUNO]`, `[ATENDENTE]`): as bolhas por autor, o painel de contexto com "mudou" e as linhas de
  tool por turno só existem para turnos que chegam com `events` e `context`. As linhas de tool,
  a resposta, tokens e custo aparecem nos dois casos.
- **Listas de execuções e de sessões.** A coluna de mensagem mostra a fala pura quando o run é
  estruturado; num run antigo, a mensagem inteira, como antes.
- **`kuro sessions show`, `session_show` (MCP) e a TUI.** Passam a ler a linha do tempo: run antigo
  vem com `structured: false` e o texto em `raw_input`. O resumo da sessão e as memórias do
  contato saíram do `session_show`; continuam em `/observability/sessions/{id}/transcript`, cujos
  itens perderam `fala`, `contexto`, `contexto_igual_ao_anterior` e `temporal` e ganharam `entrada`.
- **Sessão mista.** Uma sessão que começa no formato antigo e passa ao novo funciona: o "contexto
  mudou" compara só turnos estruturados e pula os antigos no meio.
- **Retenção.** `RUN_RETENTION_DAYS` vem desligada. Ligada, apaga ou anonimiza runs do trace store
  mais antigos que o prazo, de qualquer formato; não toca no histórico das sessões do Agno.

## 6. Voltar atrás

Voltar só a imagem não funciona: o serviço antigo não sobe com o banco na `0017`. O caminho é
restaurar o dump junto com o código de antes:

```bash
docker compose stop agent-service frontend
docker compose exec -T postgres dropdb -U postgres agent_service
docker compose exec -T postgres createdb -U postgres agent_service
docker compose exec -T postgres pg_restore -U postgres -d agent_service < kuro-antes.dump
git checkout $(cat commit-antes.txt) && cp .env.antes .env
docker compose --profile ui up -d --build
```

O que foi gravado depois da migração (runs, sessões, edições de agente) se perde na volta. Por
isso confira a etapa 4 antes de liberar o tráfego.

## 7. Depois: passar a integração para a entrada estruturada

É opcional e pode ser feito sem pressa: enquanto a integração mandar tudo em `message`, o serviço
funciona como antes e as sessões aparecem como "entrada não estruturada". O mapeamento de cada
trecho do texto atual para `events` e `context` está em
[Entrada estruturada](integracao.md#entrada-estruturada). Três cuidados:

1. **O texto que o modelo lê muda de forma.** O servidor monta um gabarito fixo (`# Contexto`,
   `## <título do bloco>`, `# Conversa desde a sua última resposta`, `# Mensagem nova do contato`).
   Instruções e descrições de tool que citam o formato antigo (um bloco `[SISTEMA]`, as marcações
   `[ALUNO]`/`[ATENDENTE]`) precisam ser reescritas para os títulos dos blocos.
2. **Faça pelo draft.** `agents promote <agente> --to <agente>-draft`, ajuste o prompt no draft,
   mande a entrada estruturada para o draft em shadow ou com `kuro eval --compare`, e só então
   promova. O passo a passo está em [integração](integracao.md).
3. **Mande só o que aconteceu desde a última resposta.** O histórico da sessão já guarda os turnos
   anteriores; repetir a conversa inteira em `events` a cada chamada duplica o que o modelo lê.
   Agentes procedurais não aceitam `events` nem `context` (422): os dados conhecidos vão em
   `dependencies`.
