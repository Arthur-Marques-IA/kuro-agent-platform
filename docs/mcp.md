# Servidor MCP do Kuro

O `kuro-mcp` expõe as operações da CLI `kuro` como tools [MCP](https://modelcontextprotocol.io).
Um agente de IA (Claude Code, ou qualquer cliente MCP) cria, testa, avalia e promove agentes do
Kuro chamando tools tipadas, sem montar comandos de shell nem interpretar o stdout.

Ele roda **na sua máquina**, por stdio, e fala com o serviço pela API HTTP — o mesmo caminho da
CLI e do console. O serviço pode estar no Docker local ou numa VPS; o MCP não acessa o banco.

```text
cliente MCP (Claude Code)  ──stdio──▶  kuro-mcp (sua máquina)  ──HTTPS──▶  agent-service (VPS)
```

## Instalar e configurar

O servidor vem no extra `mcp`, então só quem opera precisa dele (a imagem do serviço não o inclui).
A conexão usa as mesmas variáveis da CLI:

| Variável | Para quê |
|---|---|
| `KURO_API_URL` | Endereço do serviço, ex.: `https://kuro.suaempresa.com`. Padrão: `http://127.0.0.1:58000` |
| `KURO_API_KEY` | Chave de escopo **admin**. Sem ela, quase toda tool responde 401 |
| `KURO_CA_BUNDLE` | `.pem` da CA, se o certificado do serviço vem de uma CA própria |
| `KURO_INSECURE=1` | Não valida o certificado. Só para teste local com certificado autoassinado |
| `KURO_TIMEOUT` | Timeout das requisições em segundos (padrão 300 — um `eval` longo leva tempo) |

### O jeito mais rápido: pergunte ao servidor

No servidor onde o Kuro roda, um comando imprime a configuração pronta para copiar, com o endereço
e a chave:

```bash
docker compose exec agent-service kuro mcp-config --url https://kuro.suaempresa.com --show-key
```

Ele devolve o `claude mcp add ...` e o `.mcp.json`. Os dois rodam o `kuro-mcp` com o
[uv](https://docs.astral.sh/uv/) direto do GitHub (`uvx --from "agent-service[mcp] @ git+..."`),
sem clonar o repositório: na sua máquina só precisa do `uv` e de acesso de leitura ao repositório.
O `uvx` guarda o pacote em cache e instala a `master` do momento em que rodou pela primeira vez.
Quando o servidor for atualizado, atualize o cliente uma vez para as tools baterem com a API:
`uvx --refresh --from "agent-service[mcp] @ git+https://github.com/Arthur-Marques-IA/microservice_agents" kuro-mcp`
(ele sobe e fica esperando; feche com Ctrl+C). Quando o projeto tiver tags de versão, o
`mcp-config` passará a fixar a mesma versão do servidor.
Sem `--show-key` a chave sai mascarada, para não ficar no histórico do terminal por descuido; sem
`--url` ele avisa que o endereço que enxerga de dentro do container não serve para a sua máquina.

### Claude Code, a partir de um clone

Pelo terminal, a partir do clone deste repositório:

```bash
claude mcp add kuro --env KURO_API_URL=https://kuro.suaempresa.com --env KURO_API_KEY=sua-chave-admin -- uv --directory /caminho/para/Microservice_Agent run --extra mcp kuro-mcp
```

Ou num `.mcp.json` do projeto onde seus agentes trabalham. A chave fica fora do arquivo: o Claude
Code expande `${KURO_API_KEY}` a partir do seu ambiente, então o `.mcp.json` pode ir para o git.

```json
{
  "mcpServers": {
    "kuro": {
      "command": "uv",
      "args": ["--directory", "/caminho/para/Microservice_Agent", "run", "--extra", "mcp", "kuro-mcp"],
      "env": {
        "KURO_API_URL": "https://kuro.suaempresa.com",
        "KURO_API_KEY": "${KURO_API_KEY}"
      }
    }
  }
}
```

Confira com a tool `health`: ela devolve a versão do serviço, se a autenticação está ligada e os
provedores com credencial. Uma falha de conexão diz para onde tentou ir.

## O que dá para fazer

| Área | Tools |
|---|---|
| Diagnóstico e modelos | `health`, `providers_list`, `provider_models`, `credentials_list` |
| Agentes | `agents_list`, `agent_get`, `agent_apply`, `agent_set`, `agent_versions`, `agent_revisions`, `agent_integration`, `agent_rollback`\*, `agent_promote`\*, `agent_delete`\* |
| Executar | `chat`, `analyze`, `eval`, `tool_invoke` |
| Feedback | `feedback_show`, `feedback_versions`, `feedback_send`, `feedback_remove`, `feedback_rollback`, `feedback_clear`\* |
| Tools | `tools_list`, `tool_get`, `tool_catalog`, `tool_apply`, `tool_invoke`, `tool_delete`\* |
| Execuções | `runs_list`, `run_show`, `runs_stats`, `runs_overview`, `runs_agreement`, `run_score` |
| Conversas | `sessions_list`, `session_show`, `session_delete`\* |
| Bases de conhecimento | `collections_list`, `collection_docs`, `collection_search`, `collection_add`, `collection_rm_doc`\* |

\* Pede confirmação ao usuário (ver abaixo).

O fluxo seguro para mudar um agente em produção é o mesmo da CLI, e o servidor o descreve ao
cliente nas instruções de conexão:

1. `agent_promote(source="r8", to="r8-draft")` cria o draft como cópia da produção;
2. `agent_set` / `agent_apply` no draft;
3. `eval(agent_type="r8-draft", cases_file="casos.jsonl", compare_with="r8")` — veja `verdict.ok`;
4. `agent_promote(source="r8-draft", to="r8")`, que pede confirmação.

## Confirmação do usuário

Toda operação que na CLI exige `--yes` exige, aqui, **confirmação da pessoa**: remover agente,
tool, conversa ou documento, restaurar uma versão do prompt, promover e zerar a nota de feedback.
O servidor pede pelo próprio cliente MCP (*elicitation*), que mostra a pergunta à pessoa e devolve
a resposta. Recusar ou cancelar não envia nada ao serviço.

Não existe parâmetro que o modelo possa preencher para se autoconfirmar. Se o cliente MCP não
suporta elicitation, a tool falha e devolve o comando equivalente da CLI (ex.:
`kuro agents delete suporte --yes`), para a própria pessoa rodar no terminal.

**Editar um agente em produção também pede confirmação.** `agent_set` e `agent_apply` perguntam à
pessoa quando o agente atendeu execuções reais (fora de `dry_run`) nos últimos 7 dias: o agente
de IA não edita produção sozinho, e a pergunta lembra do caminho seguro, que é editar um draft e
promover depois do eval. Draft, agente novo e `dry_run=true` (só validar) seguem sem pergunta,
então o fluxo draft → eval → promote não fica mais lento. Na CLI não muda nada: lá quem roda o
comando já é a pessoa.

Editar uma tool (`tool_apply`) ou as regras de feedback não pede confirmação: tem histórico e
versão para voltar. As tools que sobrescrevem levam `destructiveHint`, porque sobrescrevem
o que existia. As de leitura levam `readOnlyHint`, e só `run_score` e `collection_add`, que apenas
acrescentam, ficam sem nenhuma das duas. É por essas anotações que o cliente MCP decide o que
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
  `feedback_send`. O `user_id` padrão é `mcp` (o da CLI é `cli`).
- **Respostas enxutas.** Listas vêm resumidas e textos longos (traces, transcrições) são cortados
  em 4000 caracteres com um aviso, para não estourar o contexto do modelo. O `eval` devolve só os
  casos que falharam, junto com o resumo.
- **Arquivos são caminhos locais.** `analyze(document_file=...)`, `eval(cases_file=..., rules_file=...)`
  e `collection_add(file=...)` leem da máquina onde o `kuro-mcp` roda, não da VPS.

## Desenvolvimento

O servidor fica em `src/agent_service/mcp_server.py` e usa o mesmo cliente HTTP da CLI
(`agent_service/cli/client.py`). Os testes (`tests/test_mcp_server.py`) ligam um cliente MCP em
memória a uma API falsa (`httpx.MockTransport`), sem rede e sem serviço no ar:

```bash
uv run pytest tests/test_mcp_server.py
```

Para ver as tools num cliente de verdade, o MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run --extra mcp kuro-mcp
```
