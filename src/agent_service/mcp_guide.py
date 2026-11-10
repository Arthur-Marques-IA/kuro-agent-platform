"""O guia que a tool `guide` do MCP devolve: os conceitos que um agente precisa para operar o Kuro
sem enxergar este repositório. Quem conecta pelo MCP trabalha em outro projeto (o sistema que
integra), então "veja o AGENTS.md" não leva a lugar nenhum. A fonte longa continua em
docs/conceitos.md e no AGENTS.md; aqui fica o essencial de cada tema, curto.
"""

TOPICS: dict[str, str] = {
    "fluxo": """\
# Mudar um agente em produção sem risco

1. agent_promote(source=<prod>, to=<prod>-draft): cria ou atualiza o draft, uma cópia do de produção.
2. agent_set/agent_apply no draft. agent_get(editable=true) devolve exatamente os campos aceitos.
3. Teste o draft:
   - kind=analysis: eval(draft, cases=..., compare_with=<prod>). runs_export transforma runs com
     referência (modo shadow) em casos.
   - kind=conversational/procedural: session_replay(<uma conversa real>, agent_type=draft,
     compare_with=<prod>, repeat=3, focus=["<tool>.<arg>"]). Escolha conversas que mostram o
     problema (runs_list com feedback="down", tool_failed, side_effect ou search).
4. agent_promote(source=draft, to=<prod>): pede confirmação à pessoa.

Alterar direto um agente que atendeu de verdade nos últimos dias pede confirmação: o caminho é o draft.
Cada mudança de configuração vira uma versão (agent_revisions); cada run grava agent_version.
""",
    "tools": """\
# Tools: kinds e de onde vem cada parâmetro

kind: builtin (toolkit pronta; veja tool_catalog), api (qualquer API HTTP, sem código) ou python
(código; não se cria pelo MCP no servidor).

Cada parâmetro de uma tool kind=api declara `source`:
- "model" (padrão): o modelo preenche. É o único que aparece no schema dele.
- "dependency": o servidor injeta dependencies[<campo>] da requisição. Use para dado que o modelo
  não pode escolher (CPF, id de conta): fica fora do schema, o modelo não inventa nem troca. O
  agente precisa declarar o campo em dependency_fields. As dependencies também entram no contexto
  do modelo: isso protege qual valor vai na chamada, não esconde o valor do provedor de LLM.
- "const": valor fixo em `value`.

Parâmetro source=model de tipo object exige `fields`; de tipo array exige `items` (o mesmo
vocabulário do response_schema). Sem eles, 422 no cadastro.
  {"name": "ids", "type": "array", "location": "body", "items": {"type": "integer"}}
  {"name": "filtros", "type": "object", "location": "body",
   "fields": [{"name": "status", "type": "string", "required": true}]}

`required` é cobrado antes da chamada HTTP. `location`: path, query, body ou header.
Segredos vão em config.auth ({"type": "api_key", "header": "X-...-Token", "value": "..."}) e voltam
mascarados; devolver a máscara num update mantém o valor guardado.
`side_effect: true` marca a tool que muda algo fora (fecha acordo, envia mensagem): runs_list
filtra por isso, e num agente procedural ela só entra como etapa action.
Egress: toda tool só alcança endereços públicos (conferido antes de cada chamada). Para um serviço
interno legítimo, quem opera o servidor libera o host em TOOL_EGRESS_ALLOWLIST.
Teste sem agente: tool_invoke(tool_name, arguments, dependencies). Em dry_run (padrão) a tool
recebe o header X-Kuro-Dry-Run: true e decide o que simular.
""",
    "analysis": """\
# Agente analista (kind=analysis)

One-shot, sem sessão nem histórico: recebe `document` (texto; JSON como texto serve) e devolve
`result` validado contra o response_schema. Rode com analyze.

response_schema: campos {name, type, required, description, enum}. type: string, integer, number,
boolean, object (exige `fields`) ou array (exige `items`, que é {type, fields?}).
  [{"name": "acao", "type": "string", "required": true, "enum": ["responder", "escalar"]},
   {"name": "parcelas", "type": "array", "items": {"type": "object", "fields": [
      {"name": "numero", "type": "integer", "required": true}]}}]

num_history_runs e memory_backend dão 422 (não fazem nada num one-shot); a nota de feedback não se
aplica: corrija as instructions num draft.

Teste: eval(agent_type, cases=[{id, input, dependencies, expected}], rules=[...], compare_with).
Comparação campo a campo; objetos viram caminhos (acordo.qtd_parcelas). Regras:
{name, field, op: eq|ne|in|not_in|lt|lte|gt|gte|exists|not_exists, value (ou {"$dep": "campo"}), when?};
violação reprova o caso. run_feedback(value=0, reference=...) grava a saída certa de um run, que
runs_export devolve como caso.
""",
    "conversational": """\
# Agente conversacional (kind=conversational, o padrão)

Responde texto, com histórico (num_history_runs) e, se ligado, memória (memory_backend: none, auto,
agentic, mem0). Converse com chat; continue a conversa repassando o session_id devolvido.
`dependencies` são os dados que o sistema integrado manda (declarados em dependency_fields).

Teste de regressão: session_replay. Ele reenvia as falas de uma conversa guardada, na ordem e numa
sessão nova, porque o que o agente sabe num turno pode ter chegado só num turno anterior (o bloco
de dados que o sistema integrado manda uma vez). Mandar um turno solto pelo chat testa outra coisa.
Use focus=["<tool>.<arg>"] para resumir a decisão (ex.: "regente_r6_decidir.acao") e repeat para
ver a variação: o modelo não é determinístico.

Ensinar a partir de uma conversa: feedback_send(agent_type, session_id, message) vira uma regra na
nota do agente (feedback_show, feedback_remove, feedback_rollback). Vale na mensagem seguinte —
faça num draft.
""",
    "procedural": """\
# Agente procedural (kind=procedural)

Fluxo em etapas que o SERVIDOR conduz; o modelo só extrai valores e redige a resposta.
  "stages": [
    {"id": "identificacao", "goal": "Identificar o cliente",
     "fields": [{"name": "cpf", "label": "CPF", "required": true, "pattern": "^\\\\d{11}$"}]},
    {"id": "problema", "fields": [{"name": "categoria", "required": true, "enum": ["internet", "tv"]}]},
    {"id": "confirmacao", "type": "confirm", "goal": "Confira os dados:"},
    {"id": "abrir", "type": "action", "tool": "abrir_chamado"}]

Etapas: collect (padrão; fields no formato de dependency_fields, com enum e pattern), confirm (texto
montado dos dados, sem o modelo) e action (o servidor chama a tool com os dados; só depois de um
confirm; nunca roda duas vezes). A etapa atual é a primeira ainda não pronta; corrigir um dado volta
o fluxo; mudar um dado depois do confirm desfaz a confirmação. Tool com side_effect não entra em
`tools` de um procedural: efeito só como etapa action. O tipo não muda depois de criado.

chat devolve `state` {stage, collected, missing, invalid, done, result}; 409 = outra mensagem da
mesma sessão em andamento. procedure_state(agent_type, session_id) e procedure_funnel(agent_type).
""",
    "rag": """\
# Base de conhecimento (collections)

O agente busca na collection de knowledge_collection (agent_set). collection_search mostra o mesmo
que o agente enxerga. Indexe com collection_add (texto) ou collection_add_site (crawl, sitemap,
page; em background, acompanhe com collection_sources e, se falhar, jobs_list/job_retry).

O embedder e o idioma são escolhidos em collection_create e não mudam; search_mode (vector|hybrid),
max_results e vector_weight mudam com collection_set e viram versões (collection_versions).

Trecho errado num run: run_chunk_feedback(run_id, chunk_id, label="wrong") → corrija com
collection_edit_doc (mesmo content_id, revisão nova, resolve as marcações) → collection_retest
confere se a pergunta marcada agora traz o documento. Página de site se corrige na fonte e
collection_sync_site.
""",
    "investigar": """\
# Investigar uma resposta ruim

- Achar o run: runs_list com agent_type, since/until (o horário de um print), search (um pedaço do
  session_id, do user_id ou de metadata; acha `…-r6v2` a partir de `…-r6`), meta={"conversation_id":
  ...} ou feedback="down". run_related mostra os runs do mesmo ciclo (outros agentes).
- Ler: run_show (spans de modelo e tools, com entrada e saída) e session_show (a conversa inteira;
  raw=true traz o prompt). Textos longos vêm cortados (truncated: true).
- Os testes (dry_run, como o chat e o session_replay daqui) ficam fora de runs_list por padrão:
  use include_tests=true ou session_id.
- Configuração da decisão: o run grava agent_version; agent_revisions(agent_type, version) mostra
  a configuração completa daquela versão.
""",
}


def guide_text(topic: str | None) -> str:
    if topic is None:
        lines = ["# Guia do Kuro: escolha um tema com guide(topic)", ""]
        lines += [f"- {name}: {text.splitlines()[0].lstrip('# ')}" for name, text in TOPICS.items()]
        return "\n".join(lines)
    return TOPICS[topic]
