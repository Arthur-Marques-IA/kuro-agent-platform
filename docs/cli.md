# A CLI `kuro`

Tudo o que o console faz, você também faz pelo terminal. A CLI conversa com a
API em `http://127.0.0.1:58000` (mude com `--url` ou `KURO_API_URL`) e não
precisa de banco local.

## Paridade entre API, CLI e console

As três fazem a mesma coisa. Não há operação que só exista numa delas — o que
se configura pelo console dá para configurar pela CLI, e vice-versa:

| O que | API | CLI | Console |
|---|---|---|---|
| Agentes: listar, ver, criar, editar, excluir | `/agents` | `agents list\|get\|apply\|set\|delete` | Agentes |
| Versões do prompt e rollback | `/agents/{t}/versions` | `agents versions\|rollback` | aba Versões |
| Versões da configuração inteira | `/agents/{t}/revisions` | `agents revisions` | aba Versões |
| Promover (draft → prod) | `/agents/{t}/promote` | `agents promote` | Promover |
| Validar sem gravar | `POST`/`PUT /agents?dry_run=true` | `agents apply --dry-run` | — |
| Agentes como código (diretório) | — | `agents export`, `agents apply -f <dir>` | — |
| Avaliar contra um dataset | — | `eval` | — |
| Agente procedural: estado de uma conversa e funil | `/agents/{t}/procedures` | `agents procedures` | faixa do chat, aba Execuções |
| O que o agente aprendeu (regras de feedback) | `/agents/{t}/feedback` | `agents feedback` | aba Aprendizado |
| Histórico das regras e rollback | `/agents/{t}/feedback/versions` | `agents feedback --versions\|--rollback` | aba Aprendizado |
| Contrato de integração | `/agents/{t}/integration` | `agents integrate` | aba Integração |
| Conversar (com anexos) | `/chat`, `/chat/stream` | `chat`, `chat -a` | Playground |
| Analisar documento | `/analyze` | `analyze` | Análise |
| Tools: CRUD, catálogo, invocar | `/tools` | `tools ...` | Tools |
| Testar tool com `dependencies` | `/tools/{n}/invoke` | `tools invoke -d` | Testar tool |
| Collections: CRUD e busca | `/collections` | `collections ...` | Conhecimento |
| Embedder de cada collection | `/collections/embedders` | `collections embedders` | diálogo nova coleção |
| Indexar texto e arquivo | `/collections/{n}/documents`, `/files` | `collections add`, `add -f` | abas Texto e Arquivo |
| Listar e apagar documento indexado | `/collections/{n}/documents` | `collections docs\|rm-doc` | tabela da coleção |
| Sites: coletar, acompanhar, ressincronizar | `/collections/{n}/sources` | `collections add-site\|sources\|sync\|set-site\|rm-site` | aba Site e painel Sites |
| Provedores e credenciais de modelo | `/model-providers`, `/model-credentials` | `providers`, `credentials` | Chaves de API |
| Modelos disponíveis no provedor (ao vivo) | `/model-providers/{p}/models` | `providers models <p>` | seletor de modelo do agente |
| Execuções, traces e scores | `/observability/*` | `runs ...`, `dash` | Logs |
| Panorama (totais, agentes, tools falhando) | `/observability/overview` | `dash` (aba Panorama) | Logs |
| Shadow: referência e concordância | `/observability/references`, `/agreement` | `runs reference\|agreement` | aba Execuções (analistas) |
| Dataset a partir do shadow | `/observability/export` | `runs export` | — |
| Feedback por trecho do RAG | `/observability/chunk-feedback` | `runs chunk-feedback\|chunk-unmark\|chunk-marks` | botões nos Trechos consultados |
| Documentos marcados como errados | `/observability/chunk-feedback/flagged` | `runs chunk-flagged` | Conhecimento |
| Corrigir documento (revisão nova) | `PUT /collections/{n}/documents/{id}` | `collections edit-doc\|revisions\|retest` | Conhecimento → Corrigir |
| Fechar marcação sem editar | `/observability/chunk-feedback/{id}/status` | `runs chunk-dismiss\|chunk-resolve\|chunk-reopen` | Corrigir → Não procede |
| Gabarito de RAG (JSONL) | `/observability/chunk-feedback/export` | `runs chunk-export` | — |
| Conversas salvas, renomear e apagar | `/sessions` | `sessions list\|show\|rename\|delete` | Conversas |
| Fila de jobs (sync de sites): ver e destravar | `/jobs` | `jobs list\|show\|retry\|cancel` | — |

As exceções são limitações reais, não esquecimento:
**`runs tail`**, que acompanha execuções ao
vivo, existe só na CLI (no console, a lista de Logs atualiza sozinha);
**testar uma chave de API antes de salvá-la** existe só no console, porque na
CLI o caminho é cadastrar e rodar `credentials test`; e o que é fluxo de CI —
**`eval`, `runs export`, `agents export`/`apply -f <dir>` e `--dry-run`** — fica
na CLI, porque trabalha com arquivos do repositório de quem mantém os agentes.

O [servidor MCP](mcp.md) oferece estas mesmas operações como tools para agentes de
IA, com duas exceções de propósito: cadastrar ou trocar uma chave de modelo (a
chave passaria pelo contexto do modelo) e os comandos interativos (shell, `edit`,
`dash`).

## O shell do Kuro

Rode `uv run kuro` sem argumentos e você entra num shell com menus. A `/` é
opcional, e qualquer comando da CLI funciona lá dentro:

```text
$ uv run kuro
kuro · http://127.0.0.1:58000 · /help para comandos
kuro> /agents
? Agente:  suporte  —  Agente de Suporte
? Agente de Suporte (suporte):
  » Testar (chat)
    Testar em sessão nova
    Ver definição
    Editar no editor
    Histórico do prompt
    Restaurar uma versão do prompt
    Remover
```

| No shell | O que faz |
|---|---|
| `/agents` | escolhe um agente e abre as ações: testar, ver, editar, versões, restaurar, remover |
| `/chat <agente>` | conversa direto com o agente |
| `/analyze <agente>` | analisa um documento com um agente `analysis` |
| `/agents feedback <agente>` | ensina o agente a partir da última conversa |
| `/agents integrate <agente>` | mostra como outro módulo chama o agente |
| `/tools` | escolhe uma tool para ver e invocar |
| `/collections` | bases de conhecimento |
| `/runs` · `/runs show <run_id>` | execuções recentes e o detalhe de uma |
| `/runs tail` · `/runs stats` | execuções ao vivo e o resumo com custo |
| `/dash` | painel em tela cheia: execuções ao vivo, trace e panorama ([detalhes](tui.md)) |
| `/sessions` · `/sessions show <id>` | conversas guardadas e a transcrição de uma |
| `/providers` · `/credentials` | provedores e chaves de modelo |
| `/health` | diagnóstico do serviço |
| `/help` · `/sair` | ajuda e saída |

## O dia a dia, comando a comando

**Criar e ajustar um agente**

```bash
uv run kuro agents apply -f suporte.json        # cria ou atualiza a partir de um arquivo
uv run kuro agents edit suporte                 # abre a definição no seu $EDITOR e salva o que mudar
uv run kuro agents set suporte num_history_runs=5 tools='["cep"]'   # muda só esses campos
uv run kuro agents versions suporte             # histórico do prompt
uv run kuro agents rollback suporte 3           # volta as instructions para as da v3
uv run kuro agents revisions suporte            # versões da configuração inteira (a atual marcada)
```

**Mudar com segurança: draft, eval e promote** (agentes `analysis`)

```bash
uv run kuro agents promote r8 --to r8-draft --yes         # cria/atualiza o draft a partir do prod
uv run kuro agents set r8-draft model_id=gemini-2.5-pro   # mexe só no draft
uv run kuro eval r8-draft -f casos.jsonl -r regras.json --compare r8   # sai com 1 se piorou
uv run kuro agents promote r8-draft --to r8 --yes         # publica
```

O dataset é JSONL, um caso por linha: `{"id", "input", "dependencies", "expected"}`.
A comparação é determinística, campo a campo (objetos viram caminhos como
`acordo.qtd_parcelas`), e as regras de política (`-r`) valem para toda saída —
por exemplo `{"field": "acordo.valor_acordo", "op": "lte", "value": {"$dep": "teto"}}`.
Cada run avaliado recebe a nota `eval` (1/0) no trace store. Detalhes em
`uv run kuro eval --help`.

**Testar**

```bash
uv run kuro chat suporte                        # conversa contínua; /nova troca de sessão, /sair volta
uv run kuro chat suporte -m "meu wifi caiu" -d cpf=12345678900     # uma mensagem, com dependencies
uv run kuro chat suporte -m "o que tem nessa foto?" -a foto.png    # com anexo
uv run kuro analyze extrator-contrato -a contrato.pdf              # agente analista
```

A sessão do chat fica salva por agente, então mensagens seguidas continuam a
mesma conversa. Use `--new-session` para recomeçar. Com um agente procedural, o rodapé mostra a etapa:
`[2/4 problema] faltando: descricao`, e `kuro agents procedures <agente> [sessão]` mostra o
funil ou o estado de uma conversa.

Com um agente que consulta uma base de conhecimento, o rodapé de `chat` e de `analyze` lista as
fontes (`[1] Política de trocas — https://...  (0.91)`); com `--json`, elas vêm em `sources`.

**Ensinar com feedback**

```bash
uv run kuro agents feedback suporte -m "deveria confirmar o CPF antes de dar detalhes"
uv run kuro agents feedback suporte --show          # as regras atuais, com os ids
uv run kuro agents feedback suporte --remove a1b2c3 # apaga uma regra
uv run kuro agents feedback suporte --versions      # histórico
uv run kuro agents feedback suporte --rollback 2    # volta para as regras da v2
```

**Investigar o que aconteceu**

```bash
uv run kuro runs list --agent suporte           # execuções com latência, tokens, custo e status
uv run kuro runs show <run_id>                  # chamadas ao modelo, tools e avaliações
uv run kuro runs score <run_id> 1 --comment "resposta correta"
uv run kuro runs chunk-feedback <run_id> <chunk_id> wrong --comment "tabela de 2023"   # relevant | irrelevant | wrong
uv run kuro runs chunk-flagged -c manuais       # documentos com marcações abertas de errado, com os runs
uv run kuro collections edit-doc manuais <id> -t "texto corrigido"   # revisão nova; resolve as abertas
uv run kuro collections retest manuais <id>     # a pergunta marcada agora traz o documento?
uv run kuro collections reindex manuais --yes   # recorta os documentos com a regra de trechos atual
uv run kuro runs chunk-export -c manuais -o gabarito.jsonl   # marcações relevant: pergunta → trechos
uv run kuro runs tail --agent suporte           # acompanha as execuções ao vivo (Ctrl+C sai)
uv run kuro runs stats --agent suporte          # total, erros, tokens, custo e série diária
uv run kuro runs sessions --agent suporte       # execuções agrupadas por sessão (o mesmo recorte dos Logs)
uv run kuro sessions list                       # conversas guardadas deste user_id
uv run kuro sessions show <session_id>          # a transcrição, mensagem a mensagem
```

`runs` lê as execuções registradas (tokens, custo, spans); `sessions` lê a
conversa em si. Os dois funcionam sem Langfuse.

Para acompanhar com calma, o painel em tela cheia junta as duas coisas: as execuções
chegando, o trace de cada uma (Enter) e o panorama do período. Ele precisa do extra
`tui`, e o guia está em [tui.md](tui.md).

```bash
uv run --extra tui kuro dash --agent suporte
```

**Tools, conhecimento e modelos**

```bash
uv run kuro tools                               # escolhe uma tool e invoca
uv run kuro tools invoke calculator --fn add -a a=2 -a b=3
uv run kuro tools invoke ficha -a assunto=fatura -d cpf=12345678900  # -d simula o dependencies do /chat
uv run kuro tools apply -f cep.json             # cria ou atualiza uma tool
uv run kuro tools set cep enabled=false         # muda só esses campos
uv run kuro collections embedders               # quem gera os vetores, e quem já tem credencial
uv run kuro collections add manuais -f manual.pdf   # indexa um arquivo
uv run kuro collections docs manuais            # o que está indexado, com o status
uv run kuro collections rm-doc manuais <id> --yes   # tira um documento da base
uv run kuro collections search manuais "prazo de garantia"   # com o modo e o k da collection
uv run kuro collections create manuais --label "Manuais" --mode hybrid --k 6   # busca híbrida
uv run kuro collections set manuais search_mode=hybrid vector_weight=0.7    # vira uma versão nova
uv run kuro collections versions manuais        # histórico da configuração de busca
uv run kuro runs list --collection manuais --collection-version 2   # runs que buscaram com a v2
uv run kuro collections add-site manuais https://docs.exemplo.com/guia --refresh weekly --wait
                                                # --no-files: sem os PDF/DOCX/PPTX/XLSX linkados
uv run kuro collections sources manuais         # sites e o status da sincronização
uv run kuro credentials test <credential_id>    # valida a chave sem gastar tokens
uv run kuro health                              # serviço, Langfuse e provedores
```

Qualquer comando aceita `--help`, por exemplo `uv run kuro chat --help`.

> **Dica:** instale com `uv tool install -e .` para chamar só `kuro` de
> qualquer pasta, e com a inicialização mais rápida.

**Manutenção no servidor, sem console.** A CLI já vem instalada na imagem, com
a URL e a chave configuradas — dá para operar tudo de dentro do container:

```bash
docker compose exec agent-service kuro health
docker compose exec agent-service kuro --json agents list
docker compose exec -it agent-service kuro            # shell interativo
```

Fora do servidor, aponte a CLI com as duas variáveis:

```bash
export KURO_API_URL=https://kuro.interno
export KURO_API_KEY=...        # a ADMIN_API_KEY
uv run kuro health
```

## Para agentes de IA e scripts

A mesma CLI funciona sem ninguém no teclado: `--json` devolve dados no stdout
e erros em JSON no stderr, os códigos de saída são previsíveis (`0` sucesso,
`1` falha, `2` uso incorreto, `3` serviço inacessível) e `--no-input` garante
que nada fica esperando resposta. As receitas estão em **[AGENTS.md](../AGENTS.md)**.

Para um agente que fala MCP, como o Claude Code, o caminho mais direto é o
**[servidor MCP](mcp.md)** (`kuro-mcp`). Ele expõe as mesmas operações como tools
tipadas, sem o agente montar comandos nem interpretar o stdout. Nele, remover,
restaurar e promover pedem confirmação à pessoa, em vez de aceitar um `--yes`.
