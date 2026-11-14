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

## Composição do prompt, chamadas ao modelo e cache

Para cada run de `/chat`, `/chat/stream` e `/analyze` o trace guarda de onde veio o prompt e quanto
custou cada ida ao modelo:

- **Composição do prompt** (`prompt_composition` no trace, `prompt_chars` na listagem): as mensagens
  que o Agno montou para a **primeira** chamada ao modelo, medidas em caracteres exatos por parte:
  `instructions` (o sistema sem as partes abaixo), `session_context` (o texto de
  `agents/system_context.py`), `memories` e `session_summary` (os blocos que o Agno põe no sistema),
  `history` (falas de turnos anteriores), `history_tools` (chamadas e resultados de tool vindos do
  histórico), `turn` (a mensagem do turno), `dependencies` (o bloco `<additional context>` que o
  Agno cola nela) e `other`. `tokens_estimate` é `chars/4`, só uma ordem de grandeza; o número real de
  tokens está nas spans do modelo. Não entram as definições das tools nem o binário dos anexos
  (`attachments` é a contagem). A medição lê o `RunOutput` que o Agno entrega ao fim do stream:
  run interrompido ou com erro antes disso fica sem ela, e uma falha ao medir só deixa o campo nulo.
- **Uma span `GENERATION` por chamada**: vem dos eventos `ModelRequestStarted/Completed` do Agno, na
  ordem real em relação às tools, com `started_at`, `ended_at`, `latency_ms` (medida entre os eventos),
  tokens, `metadata.cache_read_tokens`/`cache_write_tokens`/`reasoning_tokens`,
  `metadata.time_to_first_token_ms` e `metadata.call`. Só o modelo principal do agente é aberto por
  chamada: o Agno agrega a memória, o resumo e o fluxo procedural, e ali a span continua uma só.
  Se as chamadas não somarem os tokens do agregado do Agno (ou o Agno informar o custo), o run
  fica com o agregado. O texto do prompt não é gravado nas spans: o tamanho está na composição.
- **Cache e custo**: `cache_read_tokens` e `cache_write_tokens` são campos do run. O custo estimado
  desconta o cache com `CACHE_PRICES` (`models/pricing.py`, fonte e data no comentário; sem preço de
  cache confirmado, a preço de lista). No Gemini e no OpenAI `input_tokens` já inclui o cache; no Claude
  não. Runs anteriores à migração `0019` ficam com cache 0, sem composição e com o custo de antes.
- **Panorama**: `GET /observability/overview` traz, no total e por agente, `cache_fraction`,
  `avg_prompt_chars` e `avg_history_fraction` (histórico sobre o total de caracteres, só dos runs
  medidos). Para ver um run específico, `runs list`/`runs show` (`prompt_chars`, `cache_read_tokens`).

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
