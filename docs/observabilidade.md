# Observabilidade

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagramas/observabilidade-escuro.svg">
  <img alt="Caminho dos traces da execução até o console, a CLI e outros módulos" src="diagramas/observabilidade-claro.svg">
</picture>

Cada run registra a mensagem, as `dependencies`, a resposta, cada modelo
chamado (tokens, latência, custo) e cada tool call (argumentos, resultado,
erro), com `user_id`, `session_id`, agente, versão do prompt e a `metadata` que
quem chamou mandou. O `run_id` é gerado antes do run, então toda resposta aponta
para o próprio trace. Tudo isso fica no trace store local (tabelas `runs`,
`run_spans`, `run_scores`, `run_metadata`, `run_references` e `chunk_feedback` no Postgres do
serviço), gravado
fora do caminho da resposta, com status sempre terminal (`success`, `error` ou
`interrupted`), mais a versão da configuração que rodou (`agent_version`,
`config_hash`). O custo vem do Agno quando ele informa; senão é estimado pelos
tokens com a tabela de `models/pricing.py` (sobrescreva com `MODEL_PRICES`) — um
modelo fora da tabela fica sem custo, nunca com um valor inventado. Com
`LANGFUSE_ENABLED=true` os traces também vão para o Langfuse, que vira um
exportador a mais.

```bash
uv run kuro runs list --agent suporte -n 5            # latência, tokens, custo, 👍/👎
uv run kuro runs show <run_id>                        # spans e scores
curl "http://127.0.0.1:58000/observability/runs?status=error&limit=20"
curl "http://127.0.0.1:58000/observability/sessions?limit=50"
curl "http://127.0.0.1:58000/observability/stats?agent_type=suporte"
```

Filtros: `agent_type`, `prompt_version`, `status` (`success`/`error`/`interrupted`),
`user_id`, `session_id`, `since`/`until` (ISO 8601), `limit` (≤ 100) e `cursor`.
`/sessions` e `/stats` são `GROUP BY` sobre o histórico inteiro. Com
`TRACE_STORE_BACKEND=langfuse` a leitura passa a vir da API do Langfuse, que
não agrupa por sessão nem por dia: aí eles agregam só um lote das execuções
recentes (o campo `scanned` diz quantas).

Os trechos que o RAG devolveu também levam uma marcação do operador (relevante, irrelevante ou
errado/desatualizado) em `/observability/chunk-feedback`. Ela só registra: nada é escondido do
agente. O detalhe está em [conceitos](conceitos.md#feedback-por-trecho).

O panorama dos Logs (`GET /observability/overview`) agrega o período num pedido só: totais
contra o período anterior, a série por hora, dia ou semana (execuções por resultado, custo,
latência p95), as tools que estão falhando e os números por agente e por versão. O console o
desenha na página Logs ([como ler o gráfico](console.md#o-gráfico-do-panorama-logs)), e o
`kuro dash` na aba Panorama ([tui.md](tui.md)). Os testes em `dry_run` ficam de fora, a menos que
`include_dry_run=true`.

Quando ligado, o Langfuse roda no compose sem ninguém precisar abrir a UI dele. Se precisar, ela fica em `http://localhost:3100`, só na máquina
local (login em `LANGFUSE_INIT_USER_EMAIL`/`LANGFUSE_INIT_USER_PASSWORD`). Para
usar o Langfuse Cloud, remova os serviços `langfuse-*` do compose e aponte
`LANGFUSE_BASE_URL=https://cloud.langfuse.com`. Troque todos os valores
marcados `CHANGEME` no compose em qualquer ambiente que não seja a sua máquina.

## Checagens automáticas

Em produção a maioria das execuções não tem nota, feedback nem referência: nada avisa que uma
resposta saiu ruim. Um agente pode declarar **checagens**, regras determinísticas (sem modelo)
avaliadas em toda execução real, `dry_run` incluído, assim que o run termina, com sucesso ou erro.
Elas usam o mesmo vocabulário das regras do `kuro eval` (`field`, `op`, `value`, `when`) e o mesmo
avaliador (`evaluation.py`).

```json
"checks": [
  {"name": "resposta curta", "field": "output_chars", "op": "gte", "value": 10, "severity": "warn"},
  {"name": "consultou a ficha", "field": "tools.consultar_ficha.called", "op": "eq", "value": true,
   "when": {"field": "dependencies.fluxo", "op": "eq", "value": "cobranca"}, "severity": "fail"}
]
```

Cadastro: `checks` no agente (API, `kuro agents set suporte checks='[...]'`, `apply`/`export`, MCP e a
seção Checagens do formulário no console). Nome, campo e operador desconhecidos, valor faltando e
padrão de regex perigoso dão 422 com a explicação. Mudar as checagens muda a versão da configuração
do agente (`config_hash`); agente sem checagens mantém a versão que já tinha.

**Campos disponíveis** (os fatos do run; um caminho que não existe no run, como `result.x` num
agente conversacional, é "ausente": só `exists`/`not_exists` o enxergam):

| Campo | O que é |
|---|---|
| `output` | o texto da resposta (`""` se não houve) |
| `output_chars` | tamanho de `output` |
| `status` | `success`, `error` ou `interrupted` |
| `latency_ms` | duração do run |
| `input_tokens`, `output_tokens` | tokens do run |
| `tool_calls`, `tool_failures` | chamadas de tool e quantas falharam (o mesmo critério do run) |
| `tools.<nome>.called` | a tool foi chamada? (`false` mesmo que nunca tenha sido) |
| `tools.<nome>.calls` | quantas vezes (0 se não foi) |
| `tools.<nome>.ok` | nenhuma chamada falhou (ausente se não foi chamada) |
| `tools.<nome>.args.<campo>` | argumento da **última** chamada (ausente se não foi chamada) |
| `result.<campo>` | saída estruturada de um agente analista (caminho com ponto para objetos) |
| `dependencies.<campo>` | as `dependencies` do pedido |
| `metadata.<campo>` | a `metadata` do pedido (sempre texto) |

`<nome>` é o nome da tool como aparece nos spans de `kuro runs show`: numa toolkit builtin é o da
função (`add`), não o da tool cadastrada (`calculator`). Os valores são lidos antes do mascaramento de
`TRACE_REDACT_KEYS`, mas o `actual` gravado sai mascarado quando alguma parte do caminho é uma
dessas chaves; texto longo no `actual` é truncado.

**Operadores:** `eq ne in not_in lt lte gt gte exists not_exists` e os de texto `contains`
(trecho de um texto, ou item de uma lista) e `matches` (regex, `re.search`). `when` (opcional, mesma
forma) deixa a checagem de fora dos runs em que não vale: ela não é avaliada nem entra na taxa.
`{"$dep": "campo"}` em `value` lê de `dependencies`.

**Limites do `matches`.** O `re` do Python não tem timeout e a avaliação roda na thread que grava os
traces, então a proteção é recusar antes: padrão de até 200 caracteres; `contains` e `matches` olham só
os primeiros 20.000 caracteres do texto; recusa referência a grupo (`\1`) e grupo repetido com
`* + {n,}` que já contenha repetição ou alternação (`(a+)+`, `(a|aa)*`; use uma classe: `[ab]*`).
Um padrão assim que escape do cadastro (banco editado à mão) vira "erro de avaliação", sem rodar.

**Severidade** (`warn` ou `fail`, padrão `fail`) é só rótulo para quem lê a fila: nunca muda o
`status` do run nem a resposta do `/chat`.

**Onde fica o resultado.** Uma linha em `run_scores` por checagem avaliada, com `source = "check"` e
`name = "check:<nome>"` (as notas humanas e as do `kuro eval` têm `source` nulo): `value` 1 passou,
0 reprovou, -1 erro de avaliação (o campo ou o padrão estava inválido: defeito da checagem, não do
run); o detalhe (regra, esperado, visto, severidade) vai em `comment`. Reaproveitar `run_scores`
dá filtro e agregação por nome iguais no Postgres e no SQLite e não mexe em `runs`; as linhas não
contam como 👍/👎 nem aparecem em `scores` do trace, e sim em `run.checks`. A avaliação roda na
thread de gravação, fora do caminho da resposta; uma exceção nela vira log, nunca falha do run nem
perde a gravação dele.

**Ler.** `kuro runs list -a suporte --check-failed` (qualquer checagem reprovada, de qualquer
severidade; erro de avaliação não conta) e `--check "resposta curta"` (só essa); na API,
`GET /observability/runs?check_failed=true&check=...`; no MCP, `runs_list(check_failed=true)`; no
console, o filtro "Checagem reprovada" da fila de revisão. `runs show`, a tela da execução e a linha
do tempo da sessão trazem as checagens avaliadas (`checks` no run e em cada turno). O panorama
(`/observability/overview`) traz, por agente, `checks`: `evaluated`, `failed`, `failure_rate` e
`errors` de cada checagem no período, respeitando `include_dry_run`.
`kuro eval --agent-checks` soma às regras do eval as checagens do agente que leem `result.*`,
`output`, `output_chars`, `status` e `dependencies.*` (o `/analyze` só devolve isso); as demais
saem listadas em `skipped_checks`.

Exemplos genéricos:

```json
[
  {"name": "resposta vazia ou curta", "field": "output_chars", "op": "gte", "value": 20,
   "when": {"field": "status", "op": "eq", "value": "success"}, "severity": "warn"},
  {"name": "tool obrigatória chamada", "field": "tools.buscar_pedido.called", "op": "eq", "value": true},
  {"name": "gravou no máximo uma vez", "field": "tools.registrar_pedido.calls", "op": "lte", "value": 1}
]
```

**O limite.** É determinístico: confere fatos (existe, quantas vezes, qual valor, casa com um padrão), não
julga se a resposta está certa, educada ou fiel à base. Para isso continuam o 👍/👎, a amostra
aleatória e o `kuro eval` com `expected`.
