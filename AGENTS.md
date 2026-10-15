# Operando o agent-service pelo terminal (para agentes de IA)

Use a CLI `kuro` em vez de `curl` para criar, editar e testar agentes, tools e
execuções. Ela fala com a API HTTP do serviço, que roda via Docker em
`http://127.0.0.1:58000` (você pode trocar a URL com `--url` ou `KURO_API_URL`).

```bash
uv run kuro health --json          # rode primeiro: serviço, autenticação, Langfuse, provedores
uv run kuro <comando> --help       # ajuda de qualquer comando
```

Se o serviço exigir autenticação, exporte `KURO_API_KEY` (a chave de escopo admin) —
sem ela os comandos saem com 1 e `{"status": 401}`. Dentro do container ela já está
configurada: `docker compose exec agent-service kuro health --json`.

Com o serviço atrás de HTTPS, `KURO_API_URL=https://...`. Se o certificado vem de uma CA
própria, aponte-a com `KURO_CA_BUNDLE=/caminho/ca.pem`; num teste local com certificado
autoassinado, `--insecure`. Erro de certificado sai com 3 e diz "o certificado de ... não
foi aceito" — é diferente de serviço fora do ar, não adianta reiniciar container.

**Tem as tools `kuro` (servidor MCP `kuro-mcp`)?** Prefira-as à CLI: são as mesmas operações,
tipadas, e o guia está em `docs/mcp.md`. Para conectá-lo a um serviço num servidor, a pessoa roda
`kuro connect usuario@servidor` na máquina dela (MCP pelo SSH, sem porta aberta); com domínio, o
serviço também atende o MCP em `/mcp`. Diferenças que importam:
- `chat`, `analyze`, `tool_invoke` e `eval` rodam em `dry_run` por padrão, e `runs_list` deixa os
  testes de fora — ache o seu run por `run_show(run_id)`, `runs_list(session_id=...)` ou `include_tests=true`;
- pelo SSH e pela URL o MCP roda no servidor: `document_file`, `cases_file`, `rules_file` e o `file`
  de `collection_add` são recusados — mande o conteúdo inline (`document`, `cases`, `rules`, `text`);
- `chat` não reaproveita a sessão da CLI, então repasse o `session_id` que ele devolve;
- remover, restaurar e promover pedem confirmação à pessoa. Não tente contornar: se o
  cliente não suportar, a tool devolve o comando da CLI para a pessoa rodar.

## Convenções

- **Sempre use `--json`** (aceito em qualquer posição: `kuro agents list --json`).
  A saída de dados vai para o stdout; os erros saem em JSON no stderr (`{"error", "status", "detail"}`).
- **Códigos de saída:** `0` indica sucesso. `1` indica que a operação falhou: erro da API, tool com `ok: false` ou erro durante o chat.
  `2` indica uso incorreto (argumento ou dependency faltando). `3` indica que o serviço está inacessível.
- **Nada fica esperando entrada sem TTY.** Seletores e prompts só aparecem em terminal interativo;
  se faltar algo, o comando falha. Para garantir isso, use `--no-input` ou `KURO_NO_INPUT=1`.
- Valores `chave=valor` são lidos como JSON quando possível (`n=3`, `ativo=true`, `tags='["a"]'`).

## Receitas

```bash
# Agentes
kuro --json agents list
kuro --json agents get suporte
kuro --json agents get suporte --editable > suporte.json   # só os campos editáveis
kuro --json agents apply -f suporte.json                  # cria ou atualiza (também aceita -f - para stdin)
kuro --json agents apply -f agentes/ --dry-run            # diretório: um JSON por agente; valida no servidor sem gravar
kuro --json agents export -o agentes/                     # um <agent_type>.json por agente (sem os seed)
kuro --json agents set suporte num_history_runs=5 tools='["calculator"]'   # altera só esses campos
kuro --json agents versions suporte
kuro --json agents rollback suporte 3 --yes                # reaplica as instructions da v3 (vira uma versão nova)
kuro --json agents revisions suporte                      # versões da configuração inteira (modelo, tools, schema, regras...)
kuro --json agents revisions suporte 4                    # a configuração completa da v4
kuro --json agents delete suporte --yes

# Testar um agente (a sessão fica salva por agente, então mensagens seguidas continuam a conversa)
kuro --json chat suporte -m "meu wifi caiu" -d cpf=12345678900
kuro --json chat suporte -m "recomeçar" --new-session
echo "mensagem longa" | kuro --json chat suporte
# retorna {content, run_id, trace_id, session_id, usage, error, sources}
# `sources`: documentos da base que as buscas do run devolveram (título, source_url, content_id,
# score, chunk_ids) — também no /chat, no /analyze e no evento `sources` do /chat/stream.
kuro --json chat suporte -m "fecha o acordo" --dry-run   # teste: tools recebem X-Kuro-Dry-Run: true
# `dry_run: true` no /chat e no /analyze (também `--dry-run` em analyze, eval e tools invoke):
# as tools recebem o header e `dependencies.dry_run` (sem declarar em dependency_fields);
# o run fica com metadata.dry_run=true. Quem implementa a tool decide o que simular.

# Anexos: imagem, áudio, vídeo ou arquivo (PDF, DOCX, CSV, TXT...) direto ao modelo
kuro --json chat suporte -m "o que tem nessa foto?" --attach foto.png       # -a, repetível
kuro --json analyze extrator -a contrato.pdf                                # PDF sem extrair texto antes
# Pela API: `attachments: [{content_base64|url, mime_type, filename}]` no /chat e no /analyze.
# O modelo do agente precisa suportar o tipo (ex.: Gemini). Limite: MAX_ATTACHMENT_MB (20).
# No REPL do chat, os anexos vão só na 1ª mensagem. Em conversas, o anexo fica no histórico da
# sessão (Postgres) e volta como contexto nos turnos seguintes — para anexos grandes ou
# recorrentes prefira `analyze` (sem sessão).

# Agentes analistas (one-shot, sem sessão) — kind="analysis"
# `document` é texto: -f aceita qualquer arquivo de texto (inclusive .json) e stdin também.
# O response_schema aceita campos simples (string/integer/number/boolean) e compostos:
# "object" (exige `fields`) e "array" (exige `items`). Sem fields/items o schema sai inválido
# para o provedor, então isso é recusado com 422 no cadastro.
kuro --json agents apply -f - <<'EOF'
{"agent_type": "extrator-contrato", "name": "Extrator de contrato", "instructions": ["Extraia os campos do contrato."],
 "kind": "analysis", "response_schema": [{"name": "valor", "type": "number", "required": true},
                                          {"name": "cliente", "type": "object",
                                           "fields": [{"name": "nome", "type": "string", "required": true}]},
                                          {"name": "parcelas", "type": "array",
                                           "items": {"type": "object", "fields": [
                                               {"name": "numero", "type": "integer", "required": true},
                                               {"name": "valor", "type": "number", "required": true}]}}]}
EOF
kuro --json analyze extrator-contrato -f contrato.txt        # ou stdin; devolve {result: {...}}
kuro --json analyze classificador -m '{"mensagens": [...]}'  # texto direto, sem arquivo temporário
cat conversa.json | kuro --json analyze classificador        # texto/JSON por stdin
# /chat e /analyze devolvem `agent_version` e `config_hash`: a configuração que decidiu.
# /analyze aceita `session_id` (agrupa por conversa) e `metadata` (correlação; não vai ao modelo).
# Memória de longo prazo: `memory_backend` = none (padrão) | auto (extraída em paralelo, modelo auxiliar AUX_MODEL_ID)
#   | agentic (o modelo decide, tool update_user_memory) | mem0. `session_summary: true` resume o que sai de num_history_runs.
# Campos do agente: `model_params` {temperature, top_p, max_tokens, reasoning: off|low|medium|high,
#   prompt_cache: off|5m|1h (só anthropic; Gemini/OpenAI cacheiam sozinhos)}, `timeout_seconds`
# (504 ao estourar) e `enum` em qualquer campo de response_schema/dependency_fields.
# Integração de outro sistema (erros, fallback, shadow): docs/integracao.md.

# Agente procedural — fluxo em etapas que o SERVIDOR conduz (coletar → confirmar → executar)
# stages: collect (fields, com enum/pattern), confirm (texto montado dos dados, sem LLM) e
# action (tool, só depois de um confirm). /chat devolve `state` {stage, collected, missing, invalid,
# done, result}; 409 = outra mensagem da mesma sessão em andamento. Formato: docs/conceitos.md.
kuro --json chat abertura-chamado -m "sou a Ana, cpf 12345678901"   # out["state"]["missing"]
kuro --json agents procedures abertura-chamado                        # funil por etapa
kuro --json agents procedures abertura-chamado <session_id>           # estado de uma conversa
# Tool com side_effect=true não entra em `tools` de um procedural (vira etapa action).

# Mudar um agente em produção sem risco: draft → eval → promote
kuro --json agents promote r8 --to r8-draft --yes            # cria/atualiza o draft (cópia do prod)
kuro --json agents set r8-draft instructions='["..."]'       # mexa só no draft
kuro --json eval r8-draft -f casos.jsonl -r regras.json --compare r8   # sai com 1 se piorou
kuro --json agents promote r8-draft --to r8 --yes            # só depois do eval passar
# casos.jsonl: {"id", "input": {...}|"texto", "dependencies": {...}, "expected": {"acao": "...", ...}} por linha.
# Comparação determinística campo a campo; objetos viram caminhos (acordo.qtd_parcelas).
# regras.json: [{"name", "field", "op", "value", "when"?}] — op: eq ne in not_in lt lte gt gte exists not_exists;
# "value": {"$dep": "teto"} lê das dependencies do caso. Violação reprova o caso sempre.
# Saída 0 se pass_rate >= --min-pass (0.9) e sem violação; com --compare, 1 se o candidato ficar pior.
# Em kind="analysis": `num_history_runs` e `memory_backend` dão 422 (não fazem nada num
# agente one-shot) e a nota de feedback não se aplica — ajuste as instructions.
# Teto do texto de entrada: MAX_INPUT_CHARS (200000 caracteres), no /chat e no /analyze.

# Feedback de conversa vira instrução (agentes conversacionais)
# A nota é uma lista de regras com id, não um texto solto: o merge edita e remove
# regra por id, e a resposta traz o `diff` do que mudou. Regras parecidas demais
# são fundidas pelo servidor, sem depender de o modelo obedecer.
kuro --json chat suporte -m "meu wifi caiu"
kuro --json agents feedback suporte -m "deveria confirmar o CPF antes de dar detalhes"  # usa a sessão salva
kuro --json agents feedback suporte --show                    # regras atuais, com os ids
kuro --json agents feedback suporte --remove a1b2c3           # apaga uma regra
kuro --json agents feedback suporte --versions                # histórico
kuro --json agents feedback suporte --rollback 2              # volta para as regras da v2
kuro --json agents feedback suporte --clear --yes             # zera a nota e o histórico

# Tools (sem montar agente)
kuro --json tools list
kuro --json tools get calculator                # builtins listam as funções disponíveis
kuro --json tools get cep --editable > cep.json # só os campos editáveis
kuro --json tools apply -f cep.json             # cria (precisa de tool_name e kind) ou atualiza
kuro --json tools set cep enabled=false         # altera só esses campos (kind não muda)
kuro --json tools delete cep --yes              # trava se algum agente usa a tool
kuro --json tools invoke calculator --fn add -a a=2 -a b=3
kuro --json tools invoke ficha -a assunto=fatura -d cpf=12345678900   # -d simula o `dependencies` do /chat

# Execuções (trace store no Postgres do serviço; funciona sem Langfuse)
kuro --json runs list --agent suporte -n 5
kuro --json runs show <run_id>                  # mensagem, resposta, spans (LLM/tools), scores
kuro --json runs score <run_id> 1 --comment "resposta correta"
kuro --json runs stats --agent suporte          # total, erros, tokens, custo e série diária
kuro --json runs tail --agent suporte           # acompanha ao vivo; um objeto JSON por execução, Ctrl+C sai
kuro --json runs sessions --agent suporte       # execuções agrupadas por sessão (tokens, custo, erros)
kuro --json runs list --agent r8 --meta conversation_id=98231   # pela metadata que quem chamou mandou
kuro --json runs list --agent r8 --version 7    # só runs de uma versão da configuração
# Fila de revisão: o que vale um humano olhar (complexidade 1–3 pelas tools de negócio distintas)
kuro --json runs list -a r8 -c 3 --no-tests      # complexidade 3, sem os testes do Playground
kuro --json runs list -a r8 --tool-failed        # o agente respondeu, mas alguma tool falhou
kuro --json runs list -a r8 --side-effect --min-chars 20   # chamou tool com efeito colateral; sem "ok"/"oi"
kuro --json runs list -a r8 --feedback down      # com 👎
kuro --json runs list -a r8 --sample 20 --no-tests   # amostra aleatória (pega o erro que nenhum filtro aponta)
# Panorama do dashboard (totais vs período anterior, série, agentes e versões, tools falhando):
# GET /observability/overview?since=...&agent_type=...&include_dry_run=false&tz=America/Sao_Paulo

# Modo shadow (o legado responde; o Kuro decide em silêncio e é comparado)
kuro --json runs reference <run_id> -f decisao_legado.json   # o sistema integrado usa POST /observability/references
kuro --json runs agreement -a r8                # concordância com a referência, por campo e por versão
kuro --json runs export -a r8 -o casos.jsonl    # runs com referência viram dataset do kuro eval

# Conversas guardadas (a transcrição em si)
# O user_id é o mesmo do chat: `cli` por padrão, ou KURO_USER_ID.
kuro --json sessions list --agent suporte
kuro --json sessions show <session_id>          # transcrição: cada mensagem, a resposta e os tokens
kuro --json sessions rename <session_id> "Cliente X"
kuro --json sessions delete <session_id> --yes  # apaga a conversa (não tem volta); o registro em `kuro runs` continua

# Integração: como outro módulo chama este agente (endpoint, cURL, dependências obrigatórias)
kuro --json agents integrate suporte

# Bases de conhecimento (RAG) — o agente consulta a que estiver em knowledge_collection
kuro --json collections list
kuro --json collections get manuais
kuro --json collections set manuais label="Manuais v2"
kuro --json collections embedders                            # provedores de embedding e quais têm credencial
kuro --json collections create manuais --label "Manuais do produto"
# O embedder é escolhido na criação e não muda depois (a tabela de vetores é de
# um embedder só; para trocar, crie outra collection e reindexe). Sem --embedder
# é google. `ollama` não usa chave de API nenhuma:
kuro --json collections create interna --label "Interna" --embedder ollama
cat manual.txt | kuro --json collections add manuais --title "Manual v2"
kuro --json collections add manuais -f manual.pdf             # arquivo (PDF, DOCX, CSV, TXT, MD...)
kuro --json collections docs manuais                          # o que está indexado, com o status
kuro --json collections rm-doc manuais <content_id> --yes     # tira um documento da base (não tem volta)
kuro --json collections search manuais "prazo de garantia"   # o mesmo que o agente enxerga (score, documento, origem)
# Sites: coleta em background (crawl | sitemap | page), robots.txt, egress travado, um agente
# formata cada página sem reescrever (conferido; reprovado = texto extraído), ressincronização incremental.
kuro --json collections add-site manuais https://docs.exemplo.com/guia --max-pages 100 --refresh weekly --wait
kuro --json collections sources manuais [<source_id>]        # status; com o id, página a página
kuro --json collections sync manuais <source_id>             # coleta de novo (só reprocessa o que mudou)
kuro --json collections rm-site manuais <source_id> --yes    # tira o site e as páginas dele
kuro --json jobs list --status failed                       # a fila de jobs (syncs de site): contagem e jobs
kuro --json jobs retry <job_id>                             # ou show / cancel
# No trace, a span `search_knowledge_base` traz `metadata.references`: query, e cada trecho com
# rank, score, document, content_id, chunk, section, source_url e o texto exato que o modelo leu.
kuro --json agents set suporte knowledge_collection=manuais

# Modelos: provedores suportados e credenciais
kuro --json providers list
kuro --json providers models google          # modelos que o provedor oferece AGORA (lidos da API dele; cache 15 min, --refresh)
# cada modelo traz `channel`: stable | preview | alias (-latest) — deduzido do nome; não use preview/alias em produção
kuro --json credentials list --provider google
kuro --json credentials test <credential_id>    # valida a chave sem gastar tokens; sai com 1 se falhar
KEY=... kuro --json credentials add -p google -l "Produção" --api-key-env KEY
```

## Tools: de onde vem cada parâmetro

Cada parâmetro de uma tool `kind="api"` declara um `source`:

- `"model"` (padrão): o modelo preenche — é o único que aparece no schema dele.
- `"dependency"`: o servidor injeta `dependencies[<campo>]` da requisição do `/chat`.
  Use isto para dado que **o modelo não pode escolher** (CPF, id de conta): o parâmetro
  fica fora do schema da tool, então o modelo não consegue inventar nem trocar o valor.
  O agente é obrigado a declarar o campo em `dependency_fields`.
  Atenção: todas as `dependencies` enviadas também entram no contexto do modelo. Isso
  protege qual valor vai na chamada, mas não esconde o valor do provedor de LLM.
- `"const"`: valor fixo em `value`.

`required` é cobrado antes da chamada HTTP; faltando um, a tool devolve o que faltou.

Um parâmetro `source="model"` de tipo `object` exige `fields`, e de tipo `array` exige `items` —
o mesmo vocabulário do `response_schema` de um agente analista:

```json
{"name": "ids",     "type": "array",  "location": "body", "items": {"type": "integer"}}
{"name": "filtros", "type": "object", "location": "body",
 "fields": [{"name": "status", "type": "string", "required": true}]}
```

Sem eles o schema declarado ao modelo sai como `{"type":"array"}` pelado, que o provedor recusa —
então isso é 422 no cadastro. Em `source="dependency"`/`"const"` não se aplica: esses parâmetros
não entram no schema do modelo.

Toda tool só alcança endereços **públicos**: o destino é resolvido e conferido antes de cada
chamada (com a URL já montada, porque um parâmetro `location="path"` pode compor o host). Salvar
uma tool apontando para dentro falha com 422; uma chamada recusada devolve o motivo ao modelo.
Para um serviço interno legítimo, libere o host em `TOOL_EGRESS_ALLOWLIST`. O mesmo vale para o
`httpx` das tools `kind="python"`.
Tools chamando outro container Docker: copie `docker-compose.override.example.yml` para
`docker-compose.override.yml` (rede externa + `TOOL_EGRESS_ALLOWLIST` com o nome do host) —
`docker network connect` some no próximo `up`. Detalhes em `docs/integracao.md`.

Configuração faltando no serviço responde **503** com `{"detail", "error"}`:
`model_provider_not_configured` (sem chave do provedor do agente) ou `encryption_not_configured`
(sem `CREDENTIALS_ENCRYPTION_KEY`; o `/health` mostra em `model_credentials`).

Para testar sem montar agente: `kuro tools invoke <tool> --args-json '{"x": 1}'`,
com `-d campo=valor` para os parâmetros `source="dependency"`.

O formato do `apply` é o mesmo do `POST /agents`: `agent_type`, `name`, `instructions`, `tools`,
`model_provider`, `model_id`, `model_credential_id`, `knowledge_collection`, `dependency_fields`,
`memory_backend`, `num_history_runs`, `kind` e `response_schema`. Editar `instructions` gera uma nova versão do prompt.

## Notas de versão

Mudou algo que quem integra ou opera percebe (campo, status HTTP, texto de erro, default,
variável de ambiente, passo de deploy)? Registre em `docs/notas-de-versao.md`, na seção
`## Não lançada` do topo, no mesmo commit — o formato e as regras de numeração estão no fim do
arquivo. A versão no ar sai de `pyproject.toml` e aparece em `GET /health` (`version`).

## Onde está o resto

Este arquivo é só a receita rápida da CLI. Conceitos em `docs/conceitos.md`, comandos em
`docs/cli.md`, o servidor MCP em `docs/mcp.md`, o painel `kuro dash` (só para humanos, precisa
de TTY) em `docs/tui.md`, integração em `docs/integracao.md`, configuração e segurança em
`docs/operacao.md`, testes e estrutura do código em `docs/desenvolvimento.md`. O índice está
no `README.md`. Links relativos entre documentos são conferidos por `tests/test_docs_links.py`.
