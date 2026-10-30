<p align="center">
  <img src="docs/marca/kuro-corvo.svg" alt="Kuro, o corvo mascote da plataforma" width="176">
</p>

<h1 align="center">Kuro Agent Platform</h1>

<p align="center"><strong>Build. Evaluate. Operate.</strong></p>

<p align="center">
  Uma plataforma para construir, avaliar e operar agentes de IA em produção,<br>
  por API, CLI, console web e <strong>MCP</strong>.
</p>

<p align="center">
  <a href="docs/quickstart.md">Quickstart</a> ·
  <a href="docs/mcp.md">MCP</a> ·
  <a href="docs/conceitos.md">Conceitos</a> ·
  <a href="docs/console.md">Console</a> ·
  <a href="ROADMAP.md">Roadmap</a>
</p>

No MCP do Kuro, o agente não usa a plataforma só para pegar ferramentas: ele **opera a própria
plataforma**. Um Claude, um Codex ou um agente supervisor seu consegue criar agentes, investigar
execuções que falharam, rodar avaliações e promover uma versão nova. Operações sensíveis, como
promover e remover, pedem a confirmação de uma pessoa.

```bash
uv run kuro agents apply -f atendimento.json     # cria o agente
uv run kuro chat atendimento -m "meu pedido não chegou"
uv run kuro runs list --agent atendimento        # o que aconteceu, com tokens e custo
```

---

## Por que o Kuro

Colocar um agente no ar é fácil. O difícil começa depois: saber por que ele respondeu
aquilo, mudar o prompt sem quebrar o que funcionava e provar que a versão nova é melhor.
E isso para dez agentes, não para um.

O Kuro trata cada agente como um serviço de produção:

| | |
|---|---|
| 🧩 **Um contrato, muitos agentes** | `POST /chat` é igual para qualquer agente. Criar ou editar um agente não exige deploy nem restart. |
| 🕰️ **Tudo versionado** | Cada mudança de prompt, modelo, tools, schema ou regras gera uma versão. Cada execução registra a versão que respondeu. |
| ✅ **Mudança provada antes de publicar** | Draft → eval → promote, e modo shadow para comparar com o sistema que já existe antes de trocar. |
| 🔍 **Cada execução explicada** | Chamadas ao modelo, tools, trechos da base de conhecimento, tokens, latência e custo de cada run. |
| 🔒 **O modelo não escolhe dado sensível** | O CPF ou o id da conta usados numa chamada à sua API são injetados pelo servidor. O modelo não consegue inventar nem trocar o valor. |
| 🛠️ **Tools sem escrever código** | Descreva uma API HTTP em JSON e ela vira uma tool. As tools só alcançam endereços públicos: endereços privados e internos são recusados, salvo os liberados explicitamente. |

## Operando a plataforma pelo MCP

*Operate agents through agents.*

Na maioria das plataformas, o MCP serve para dar ferramentas a um agente. No Kuro, ele também é
a interface de controle da própria plataforma. O servidor `kuro-mcp` expõe as operações como
tools tipadas: agentes, versões, execuções, avaliações, bases de conhecimento. Assim, um
assistente de IA percorre o mesmo ciclo que uma pessoa faria no console: **inspeciona** as
execuções, **diagnostica** a causa, **corrige** num rascunho, **avalia** contra casos reais e
**publica**, com a confirmação de uma pessoa.

Uma tarde comum com um agente de atendimento de uma loja virtual:

```text
Você:   O agente de atendimento piorou essa semana? Olha as conversas em que alguma tool falhou.

Claude: [runs_list agent_type=atendimento tool_failed=true]
        12 execuções desde segunda. 9 delas chamaram `rastrear-pedido` com o código
        de rastreio em minúsculas, e a transportadora devolveu 404.
        [run_show 7f3a…] O modelo copia o código como o cliente digitou.

Você:   Corrige isso num rascunho e me mostra se resolve.

Claude: [agent_promote atendimento → atendimento-draft]
        [agent_set atendimento-draft instructions=[…, "Envie o código de rastreio em maiúsculas."]]
        [runs_export agent_type=atendimento] → 40 casos reais, incluindo os 9 que falharam
        [eval atendimento-draft compare_with=atendimento]
        Rascunho: 39/40 (97,5%). Produção: 31/40 (77,5%). Nenhuma regra violada.

Você:   Pode publicar.

Claude: [agent_promote atendimento-draft → atendimento]
        ⚠ O Kuro pediu sua confirmação para promover. Confirma?
```

A mesma execução, aberta no [`kuro dash`](docs/tui.md):

<p align="center">
  <img src="docs/telas/tui-trace.png" alt="Trace de uma execução no kuro dash: a tool rastrear-pedido falhou com HTTP 404 e o modelo tentou de novo" width="820">
</p>

O que torna isso seguro de deixar nas mãos de um agente:

- **Teste por padrão.** Pelo MCP, `chat`, `analyze`, `tool_invoke` e `eval` rodam em `dry_run`:
  cada tool recebe o aviso de que é teste, e o run fica marcado. Evitar o efeito colateral cabe à
  tool, que decide o que simular.
- **Operações sensíveis passam por uma pessoa.** Pelo MCP, remover, restaurar e promover pedem
  confirmação no cliente. Se o cliente não suportar a confirmação, a tool devolve o comando da
  CLI para a pessoa rodar.
- **Comparação antes de publicar.** O fluxo draft → eval → promote compara a versão candidata
  com a atual, e o `eval` com `compare_with` reprova a candidata se ela ficar pior.

Para conectar o Claude Code, o Codex ou outro cliente, local ou num servidor via SSH, veja
[docs/mcp.md](docs/mcp.md).

## O ciclo de vida de um agente

1. **Construir.** O agente é um JSON: instruções, modelo, tools, base de conhecimento (RAG),
   memória. Há três tipos: conversacional, analista (one-shot, devolve um JSON validado por
   schema) e procedural (um fluxo em etapas que o servidor conduz, como coletar dados,
   confirmar e executar).
2. **Avaliar.** Casos de teste com o resultado esperado e regras que nunca podem ser violadas
   (por exemplo, "o desconto nunca passa do teto do cliente").
3. **Publicar.** O rascunho é promovido só depois do eval. Antes de trocar o sistema legado, o
   modo shadow deixa o Kuro decidir em silêncio e mede a concordância campo a campo.
4. **Operar.** Traces, custo por agente e por versão, fila de revisão (tools que falharam, ações
   com efeito colateral, 👎). O feedback do admin sobre uma conversa vira uma regra que o agente
   segue nas próximas.

<p align="center">
  <img src="docs/telas/tui-panorama.png" alt="kuro dash, aba Panorama: execuções, taxa de erro, latência e custo por agente, e as tools que estão falhando" width="820">
</p>

Os conceitos em detalhe estão em [docs/conceitos.md](docs/conceitos.md).

---

## Comece em 5 minutos

**Pré-requisitos:** Docker e uma chave do Google Gemini. O passo a passo completo, com o console,
o painel no terminal e o MCP, está no [quickstart](docs/quickstart.md).

```bash
cp .env.example .env
# edite o .env: GOOGLE_API_KEY=... e CREDENTIALS_ENCRYPTION_KEY=...
# gere a chave de cifra com:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

docker compose up -d --build                       # núcleo: postgres, agent-service
uv run kuro health                                 # serviço, autenticação, Langfuse, provedores
```

A API fica em http://127.0.0.1:58000 (`/docs` tem o OpenAPI). O console web e o Langfuse são
opcionais, cada um num profile do Compose; portas, HTTPS e variáveis estão em
[operação](docs/operacao.md).

**Crie e teste o primeiro agente:**

```bash
uv run kuro agents apply -f - <<'EOF'
{
  "agent_type": "atendimento",
  "name": "Atendimento da loja",
  "instructions": ["Você atende clientes de uma loja virtual.", "Responda de forma objetiva e cordial."]
}
EOF

uv run kuro chat atendimento -m "meu pedido não chegou"
```

Ou pela API, de qualquer linguagem:

```bash
curl -X POST http://127.0.0.1:58000/chat -H "Content-Type: application/json" \
  -d '{"agent_type": "atendimento", "user_id": "u1", "session_id": "s1", "message": "meu pedido não chegou"}'
```

> **Windows:** use `127.0.0.1` para a API, não `localhost`. Com o Docker Desktop,
> `localhost:58000` pode tentar IPv6 primeiro e travar por 30 s.

## Arquitetura

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagramas/tres-portas-escuro.svg">
  <img alt="Quem opera (outros sistemas, agentes de IA, pessoas), por onde (API, MCP, CLI, console web) e o que é operado (o agent-service, com modelos, Postgres e Langfuse)" src="docs/diagramas/tres-portas-claro.svg">
</picture>

- **API**: o contrato estável que os outros sistemas chamam.
- **MCP (`kuro-mcp`)**: as operações da plataforma como tools, para agentes de IA.
- **CLI (`kuro`)**: para operar pelo terminal, por pessoas ou por agentes. Inclui o
  [`kuro dash`](docs/tui.md), um painel com as execuções ao vivo.
- **Console web**: playground, logs, traces e edição visual dos agentes.

As portas fazem a mesma coisa. A [matriz de paridade](docs/cli.md#paridade-entre-api-cli-e-console)
lista cada operação e as poucas exceções. Componentes e o fluxo de uma mensagem estão em
[docs/arquitetura.md](docs/arquitetura.md).

---

## Documentação

| Documento | O que tem |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | do zero ao primeiro agente, ao `kuro dash` e ao MCP, com as telas |
| [docs/conceitos.md](docs/conceitos.md) | agentes, tools, base de conhecimento (RAG), memória, modelos, anexos |
| [docs/mcp.md](docs/mcp.md) | o servidor MCP: conectar clientes, as tools e a confirmação de ações destrutivas |
| [docs/integracao.md](docs/integracao.md) | como outro sistema chama o Kuro: contrato, erros, shadow, exemplo em PHP |
| [docs/cli.md](docs/cli.md) | o shell, os comandos e o uso por agentes de IA |
| [docs/console.md](docs/console.md) | o console web |
| [docs/tui.md](docs/tui.md) | `kuro dash`: execuções ao vivo, trace e panorama no terminal |
| [docs/observabilidade.md](docs/observabilidade.md) | runs, trace store, Langfuse |
| [docs/arquitetura.md](docs/arquitetura.md) | componentes, stack e o fluxo de uma mensagem |
| [docs/operacao.md](docs/operacao.md) | configuração, HTTPS, deploy, segurança e limitações |
| [docs/desenvolvimento.md](docs/desenvolvimento.md) | rodar os testes, estrutura do código, como contribuir |
| [docs/notas-de-versao.md](docs/notas-de-versao.md) | o que mudou em cada versão e como atualizar |
| [AGENTS.md](AGENTS.md) | receitas curtas da CLI para agentes de IA |
| [ROADMAP.md](ROADMAP.md) | o plano: diagnóstico, prioridades e o que cortar |
