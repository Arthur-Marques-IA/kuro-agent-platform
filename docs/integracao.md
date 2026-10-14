# Integrar um sistema ao Kuro

Guia para quem integra um sistema ao Kuro, escrito a partir do caso do RegenteUDSP (R4, R6 e R8).
Os agentes são criados e mantidos **no repositório do sistema integrado**, e este guia explica como
fazer isso: o contrato de chamada, o que fazer em cada erro, o modo shadow e o fluxo de mudança
draft → eval → promote.

Ao atualizar o Kuro, leia as [notas de versão](notas-de-versao.md): cada versão diz o que exige
ação do sistema integrado. A versão no ar aparece em `GET /health` (`version`).

## 1. Quem faz o quê

O Kuro decide e o sistema integrado valida e executa.

| Kuro | Sistema integrado (ex.: Regente) |
|---|---|
| Prompt, modelo e parâmetros | Regras de negócio: preço, desconto, alçada, elegibilidade |
| Decisão estruturada, validada contra o `response_schema` | Revalidar todo número da decisão contra a fonte de verdade |
| Versão da configuração que decidiu (`agent_version`, `config_hash`) | Efeitos colaterais: mensagem, cobrança, acordo |
| Registro de cada execução: tokens, custo, latência, erros | Concorrência: claim por conversa, debounce, daemon |
| Concordância com o legado e avaliação antes de publicar | Fallback quando o Kuro falha, e as chaves liga/desliga |

**A saída da IA não é verdade de negócio.** Um `valor_proposto` ou `oferta_id` que vem do Kuro é
uma escolha do modelo. O sistema integrado confere o valor na fonte antes de usar.

## 2. Acesso

- URL: `https://<host>`. Suba o Kuro com o profile `tls`, porque a chave trafega no header.
- `Authorization: Bearer <RUNTIME_API_KEY>` para executar agentes (`/chat`, `/analyze`) e gravar
  score e referência. A chave `ADMIN_API_KEY` é para a CI que cria e publica agentes. Não use a
  chave admin em produção.
- `GET /ready` (aberta) responde 200 quando o processo está de pé e o banco responde, e 503 quando
  não. É ela que o circuit breaker deve olhar, e não o `/health`.

## 3. O contrato do `/analyze`

```http
POST /analyze
Authorization: Bearer <RUNTIME_API_KEY>
Content-Type: application/json

{
  "agent_type": "r8",
  "document": "{\"lead\": {...}, \"catalogo\": [...], \"historico\": [...], \"ultima_mensagem\": \"...\"}",
  "dependencies": {"canal": "whatsapp"},
  "session_id": "conv-98231",
  "metadata": {"conversation_id": "98231", "ciclo": "webhook", "lead_id": 4412}
}
```

- `document` é o contexto que o seu sistema já monta (hoje o prompt do `ServiceLLM::chatJson()`),
  como texto ou JSON serializado. O teto é `MAX_INPUT_CHARS` (200 mil caracteres).
- `dependencies` são campos declarados em `dependency_fields` do agente, validados por tipo e por
  `enum`. **Eles entram no contexto do modelo.**
- `session_id` agrupa as decisões de uma conversa em `/observability/sessions`. Não cria
  histórico: o analista continua one-shot, e o contexto é sempre o que você manda.
- `metadata` guarda até 20 pares chave/valor que **não vão para o modelo**. Eles ficam gravados
  no run e servem de filtro: `kuro runs list --meta conversation_id=98231`.

Resposta 200:

```json
{
  "agent_type": "r8",
  "result": {"acao": "responder", "enviar_ficha": false, "lead": {"oferta_id": 42, "valor_proposto": 890.0}},
  "run_id": "5b1e…",
  "trace_id": "3f0c…",
  "agent_version": 7,
  "config_hash": "62e6a369f9ea",
  "sources": []
}
```

Guarde `run_id`, `agent_version` e `config_hash` junto com a decisão no seu banco. O `run_id` é o
id da decisão na trilha de auditoria (decisão → validação → efeito), e
`kuro runs show <run_id>` mostra tudo o que aconteceu naquela execução.

### Fontes consultadas (`sources`)

Um agente com `knowledge_collection` consulta a base durante o run. A resposta do `/chat` e do
`/analyze` traz em `sources` os documentos que essas buscas devolveram — as citações da resposta.
No `/chat/stream`, o mesmo campo chega no evento `sources` (`{"sources": [...]}`), logo antes do
`done`, só quando o run terminou sem erro.

```json
"sources": [
  {"title": "Política de trocas", "document": "docs.exemplo.com", "source_url": "https://docs.exemplo.com/trocas",
   "content_id": "a1b2…", "collection": "manuais", "score": 0.91, "chunk_ids": ["c9…", "d4…"]},
  {"title": "Manual de garantia", "document": "Manual de garantia", "source_url": null,
   "content_id": "e5f6…", "collection": "manuais", "score": 0.72, "chunk_ids": ["7a…"]}
]
```

- Um item por documento (ou página de site): os trechos do mesmo documento vêm juntos em
  `chunk_ids`, mesmo que o modelo tenha buscado mais de uma vez. A ordem é do melhor `score` para o
  pior.
- `title` é o título da página num site e o nome do documento num arquivo. `source_url` só existe
  quando o conteúdo veio de uma URL — é o link para mostrar ao usuário.
- É o que o agente **leu**, não necessariamente o que ele citou no texto. Sem busca, `[]`; num
  agente procedural, sempre `[]`.
- O texto de cada trecho não vem na resposta: está no trace (`kuro runs show <run_id>`, span
  `search_knowledge_base`, `metadata.references`).

## 4. Erros e fallback

| Status | Significa | O que fazer |
|---|---|---|
| 200 | Decisão válida contra o schema, com `enum` respeitado | Revalidar os números e executar |
| 401 / 403 | Chave ausente ou de escopo errado | Erro de configuração: alertar, não repetir |
| 404 | `agent_type` não existe | Erro de configuração: alertar, não repetir |
| 409 | Agente procedural: outra mensagem da mesma sessão ainda está sendo processada | Esperar a resposta dela e reenviar |
| 502 / 504 num agente procedural | Pode ter vindo depois da ação já executada | Ler `GET /agents/{t}/procedures/{session_id}` antes do fallback |
| 422 | Requisição inválida: dependency faltando ou de tipo errado, texto grande demais, agente não é `analysis` | Bug de quem chama: não repetir, alertar |
| 502 | Falha do provedor de modelo ou saída que não fecha com o schema | **Fallback.** Pode tentar no próximo ciclo |
| 503 com `Retry-After` | Sem vaga de execução (a fila de espera cheia, ou a espera nela esgotada), ou banco fora | **Fallback agora**, repetir depois do `Retry-After` |
| 503 com `error` | Configuração do Kuro: `model_provider_not_configured` (sem chave do provedor do modelo) ou `encryption_not_configured` (sem `CREDENTIALS_ENCRYPTION_KEY`). O `detail` diz o que falta | **Fallback** e alertar quem opera o Kuro: repetir não resolve |
| 504 | A execução passou do `timeout_seconds` do agente | **Fallback.** Veja o run no `kuro runs` |
| Rede ou timeout do cliente | O Kuro não respondeu | **Fallback** |

- **Repetir é seguro.** O `/analyze` não tem efeito colateral: repetir só gera outro run. Quem
  garante que a mesma mensagem não vire duas ações é o claim do seu sistema, como já acontece hoje.
- **O timeout do cliente deve cobrir a fila e o agente**: `QUEUE_MAX_WAIT_SECONDS + timeout_seconds
  + 5`. Com os padrões e um agente de 30 s, isso dá 65 s. Acima de `MAX_CONCURRENT_RUNS` (16
  execuções simultâneas), a chamada espera uma vaga por até `QUEUE_MAX_WAIT_SECONDS` (30 s) antes de
  o run começar, e o `timeout_seconds` só conta depois disso. Com o timeout certo, o 503 ou o 504
  do Kuro chega antes, e o run fica registrado com o motivo. Um run com status `interrupted` quer
  dizer que quem chamou desistiu antes do fim. Credencial faltando não aparece assim: ela responde
  503 antes de o run começar.
- **Prefere falhar rápido a esperar?** Um daemon que já repete no próximo ciclo, como o do Regente,
  pode pedir ao operador do Kuro `QUEUE_MAX_WAIT_SECONDS=0`. Sem fila, quem passa do limite recebe
  503 na hora, e `timeout_seconds + 5` volta a bastar.
- `GET /health` mostra `model_credentials`: `enabled`, ou o motivo de não dar para cadastrar chaves
  de modelo. `kuro health` mostra o mesmo.
- **Fallback** é o que o seu sistema já faz sem IA: transferir para humano, não responder, ou
  deixar para o próximo ciclo. Para o R6 a regra é firme: sem decisão válida, nada de efeito
  financeiro.

### Agente procedural: o estado da conversa

Um agente `kind="procedural"` é chamado pelo `/chat`, como um conversacional, e a resposta traz
também `state` — a etapa em que a conversa está, o que já foi coletado e o que falta. No
`/chat/stream`, o mesmo objeto chega no evento `state`, antes do `done`.

```json
{"content": "Para entender o problema, me diga a categoria.",
 "state": {"stage": "problema", "stage_index": 1, "stages_total": 4,
           "collected": {"cpf": "12345678901", "nome": "Ana"},
           "missing": [{"name": "categoria", "label": "Categoria", "type": "string", "enum": ["internet", "tv"]}],
           "invalid": {}, "done": false, "result": null}}
```

- **Acabou quando `state.done` é verdadeiro.** O resultado vem em `state.result`: os dados
  coletados (`collected`) e o que cada etapa `action` devolveu (`actions`).
- **409: outra mensagem da mesma sessão está em andamento.** Espere a resposta dela e reenvie. É
  essa trava que impede uma ação de rodar duas vezes. O debounce por conversa do seu sistema evita
  quase todos os casos.
- **A tool de uma etapa `action`** recebe os dados coletados como argumentos e em `dependencies`,
  mais `dependencies.idempotency_key`. A chave é a mesma enquanto os dados não mudam: se a ação
  passou do tempo e a pessoa confirmou de novo, a nova tentativa chega com a mesma chave, e o seu
  sistema reconhece que é o mesmo pedido. Corrigir um dado gera outra chave. Para mandá-la no
  header que o seu sistema espera, declare na tool um parâmetro de header com o nome do header:
  `{"name": "Idempotency-Key", "location": "header", "source": "dependency", "dependency": "idempotency_key"}`.
  `dry_run` chega a ela como em qualquer tool.
- `GET /agents/{t}/procedures/{session_id}` devolve o `state` de uma conversa e aceita a chave
  `runtime`. O funil, `GET /agents/{t}/procedures` (conversas paradas em cada etapa, sem os testes
  em `dry_run`), exige `admin`.
- **Um 502 ou 504 pode chegar depois de a ação ter rodado**: ela é gravada antes da resposta. Antes
  de cair no fallback, leia o estado da sessão; se a etapa `action` está `done`, o efeito aconteceu.

## 5. O agente como código

O repositório do sistema integrado guarda um JSON por agente. O formato é o mesmo do
`POST /agents`:

```json
{
  "agent_type": "r8",
  "name": "R8 — vendas",
  "kind": "analysis",
  "instructions": ["Você é o agente de vendas da FASPEC...", "Nunca cite preço fora do catálogo."],
  "model_provider": "google",
  "model_id": "gemini-2.5-flash",
  "model_params": {"temperature": 0.3, "thinking_budget": 0},
  "timeout_seconds": 30,
  "response_schema": [
    {"name": "acao", "type": "string", "required": true, "enum": ["responder", "resolver", "transferir", "nada"]},
    {"name": "etapa", "type": "string", "enum": ["qualificacao", "negociacao", "fechamento"]},
    {"name": "resposta", "type": "string", "required": true},
    {"name": "enviar_ficha", "type": "boolean", "required": true},
    {"name": "lead", "type": "object", "fields": [
      {"name": "oferta_id", "type": "integer"},
      {"name": "valor_proposto", "type": "number"},
      {"name": "parcelas", "type": "integer"}
    ]}
  ]
}
```

- **`enum`** restringe a saída: o provedor recebe os valores permitidos, e uma resposta fora
  deles vira 502, em vez de uma `acao` inventada. Vale para `string`, `integer` e `number`, dentro
  de `items` também.
- **`model_params`** aceita `temperature`, `top_p`, `max_tokens`, `reasoning` (`off`, `low`,
  `medium`, `high`; google e openai), `thinking_budget` (ajuste fino do Gemini 2.5) e
  `prompt_cache`. Sem ele, vale o padrão do provedor. Para ter paridade com o agente legado,
  repita a temperatura dele.
- **`prompt_cache`** (`5m` ou `1h`, só anthropic) guarda as instructions e as tools no cache do
  Claude, e as chamadas seguintes pagam uma fração desse trecho. Gemini e OpenAI fazem esse cache
  sozinhos quando o começo do prompt se repete. Em todos, os tokens lidos do cache aparecem na span
  do modelo (`kuro runs show <run_id>`, `metadata.cache_read_tokens`). O que cresce ao longo de uma
  conversa é o **histórico**, e isso o cache não resolve: limite com `num_history_runs` ou ligue
  `session_summary`.
- **`model_id`**: `kuro providers models google` marca os modelos `preview` e `alias` (`-latest`).
  Os dois podem mudar sem aviso; em produção, fixe um modelo estável. A marcação é deduzida do
  nome, porque nenhum provedor a informa na listagem.
- **`timeout_seconds`** vai de 1 a 600. Sem ele, vale o `RUN_TIMEOUT_SECONDS` do serviço (90 s).

Qualquer mudança nesses campos gera uma versão nova da configuração (`kuro agents revisions r8`).

## 6. Tools que chamam o seu sistema

### Rede Docker

Quando o Kuro e o seu sistema rodam em Docker na mesma máquina, as tools alcançam o seu sistema pelo
nome do container. Não use `docker network connect`: a ligação some no próximo `docker compose up`.
Use o override que o compose lê sozinho:

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
# ajuste o nome da rede (docker network ls) e o host
docker compose up -d
```

O exemplo faz duas coisas, e as duas são necessárias:

1. põe o `agent-service` na rede do seu sistema (`networks`, com `external: true`);
2. libera o host em `TOOL_EGRESS_ALLOWLIST`. Dentro da rede Docker o container responde num IP
   privado, e as tools só alcançam endereços públicos por padrão. Sem a liberação, a chamada volta
   como "Chamada recusada".

O sentido inverso já funciona com a mesma rede: o seu sistema chama o Kuro em
`http://agent-service:8000`.

### Modo teste (`dry_run`)

Mande `"dry_run": true` no `/chat` ou no `/analyze` quando a execução for um teste. O Kuro não
decide o que simular. Ele avisa as tools, e a API de cada uma decide:

- toda chamada HTTP de tool (`kind="api"` e `kind="python"`) leva o header `X-Kuro-Dry-Run: true`;
- um parâmetro `{"source": "dependency", "dependency": "dry_run"}` recebe `true` ou `false`. O agente
  não precisa declarar esse campo em `dependency_fields`, e um `dry_run` enviado nas `dependencies`
  não tem efeito: vale o flag da requisição;
- o run fica com `metadata.dry_run = "true"` (`kuro runs list --meta dry_run=true`).

O Playground do console manda `dry_run` ligado por padrão. Na CLI, use `--dry-run` em `kuro chat`,
`kuro analyze`, `kuro eval` e `kuro tools invoke`. Tool com efeito colateral (cobrança, acordo,
mensagem) deve tratar o header e, nesse caso, só registrar a intenção.

### Provisionar tools e agentes

Não faça "GET para ver se existe, POST se der 404". Use o `apply`, que cria ou atualiza:

```bash
kuro --json tools apply -f tools/consulta_lead.json
kuro --json agents apply -f agentes/
```

Se fizer pela API, trate os status separadamente: **401** é chave ausente, **403** é chave inválida
ou sem escopo admin, e só **404** quer dizer que o item não existe. Tratar "qualquer coisa que não
seja 200" como "não existe" esconde erro de autenticação e tenta criar o que já existe.

## 7. Mudar um agente sem risco: draft → eval → promote

```bash
export KURO_API_URL=https://kuro.exemplo KURO_API_KEY=<ADMIN_API_KEY>

kuro agents promote r8 --to r8-draft --yes            # o draft começa igual à produção
kuro agents apply -f agentes/r8-draft.json --dry-run  # valida no servidor, não grava
kuro agents apply -f agentes/r8-draft.json            # aplica no draft
kuro eval r8-draft -f casos/r8.jsonl -r regras/r8.json --compare r8   # sai com 1 se piorou
kuro agents promote r8-draft --to r8 --yes            # só se o eval passou
```

Numa CI, o `--dry-run` roda no PR e o resto no merge, **com o promote condicionado ao código de
saída do eval**. `kuro agents apply -f agentes/` aplica o diretório inteiro e só envia os campos
que mudaram, então aplicar de novo não gera versão. `kuro agents export -o agentes/` traz o que já
existe no servidor para o repositório.

Casos (`casos/r8.jsonl`, um por linha):

```json
{"id": "desconto-acima", "input": {"lead": {...}, "ultima_mensagem": "faz por 500?"}, "expected": {"acao": "transferir"}}
```

Regras de política (`regras/r8.json`), para o que nenhuma saída pode fazer:

```json
[
  {"name": "ficha só em fechamento", "when": {"field": "enviar_ficha", "op": "eq", "value": true},
   "field": "etapa", "op": "eq", "value": "fechamento"},
  {"name": "teto da alçada", "field": "lead.valor_proposto", "op": "lte", "value": {"$dep": "valor_maximo"}}
]
```

A comparação é determinística, campo a campo. Os operadores são `eq`, `ne`, `in`, `not_in`, `lt`,
`lte`, `gt`, `gte`, `exists` e `not_exists`. `{"$dep": "x"}` lê o valor das `dependencies` do caso.

## 8. Modo shadow

O agente legado continua respondendo ao usuário. O Kuro decide em silêncio, sobre a mesma
entrada:

```
mensagem → claim → monta o contexto → legado decide (e executa, como hoje)
                                    ↘ Kuro /analyze (metadata: conversation_id) → run_id
                                    → POST /observability/references {run_id, reference: <decisão do legado>}
```

```http
POST /observability/references
Authorization: Bearer <RUNTIME_API_KEY>

{"run_id": "5b1e…", "reference": {"acao": "transferir", "departamento": "Financeiro"}, "source": "legacy"}
```

- A referência tem o mesmo formato do `result`. Só os campos presentes nela entram na comparação.
- Se a referência chegar logo depois do `/analyze` e der 404, repita após 1 s: a gravação do run
  sai do caminho da resposta.
- Uma falha no shadow nunca pode afetar o fluxo legado: chame o Kuro com timeout curto e ignore
  os erros.

Com os dados acumulados:

```bash
kuro runs agreement -a r8                   # concordância campo a campo e por versão da configuração
kuro runs agreement -a r8 --version 7 --fields acao,etapa
kuro runs export -a r8 -o casos/r8.jsonl    # vira o dataset do kuro eval
```

Promova do shadow para produção quando a concordância nos campos de decisão (`acao`,
`departamento`, `oferta_id`...) atingir a meta que vocês definirem. As discordâncias vêm listadas,
e cada uma aponta para um `run_id`.

## 9. Esboço de cliente em PHP

```php
final class KuroClient
{
    public function __construct(private string $url, private string $key, private int $timeout = 65) {}

    /** @return array{result: array, run_id: string, agent_version: int, config_hash: string}|null  null = fallback */
    public function analyze(string $agent, string $document, array $deps = [], array $meta = [], ?string $session = null): ?array
    {
        $body = ['agent_type' => $agent, 'document' => $document, 'dependencies' => $deps ?: null,
                 'metadata' => $meta ?: null, 'session_id' => $session];
        [$status, $json] = $this->post('/analyze', $body);
        if ($status === 200) {
            return $json;
        }
        if (in_array($status, [401, 403, 404, 422], true)) {
            error_log("Kuro $agent: configuração/requisição inválida ($status): " . json_encode($json));
        }
        return null; // 502/503/504/rede: fallback
    }

    public function reference(string $runId, array $decision): void
    {
        $this->post('/observability/references', ['run_id' => $runId, 'reference' => $decision, 'source' => 'legacy']);
    }

    private function post(string $path, array $body): array
    {
        $ch = curl_init($this->url . $path);
        curl_setopt_array($ch, [
            CURLOPT_POST => true, CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => $this->timeout,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json', 'Authorization: Bearer ' . $this->key],
            CURLOPT_POSTFIELDS => json_encode($body, JSON_UNESCAPED_UNICODE),
        ]);
        $raw = curl_exec($ch);
        $status = $raw === false ? 0 : curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        return [$status, $raw === false ? null : json_decode($raw, true)];
    }
}
```

## 10. Checklist antes de ligar em produção

- [ ] Profile `tls` ativo e `ADMIN_API_KEY`/`RUNTIME_API_KEY` definidas; o sistema integrado usa só
      a runtime.
- [ ] O circuit breaker olha o `/ready`, e o fallback está testado para 502, 503, 504 e rede.
- [ ] O timeout do cliente cobre a fila e o agente (`QUEUE_MAX_WAIT_SECONDS + timeout_seconds + 5`), ou o
  Kuro roda com `QUEUE_MAX_WAIT_SECONDS=0`.
- [ ] `run_id`, `agent_version` e `config_hash` ficam gravados junto com cada decisão.
- [ ] Todo número da decisão é revalidado na fonte antes de qualquer efeito.
- [ ] O agente tem `enum` nos campos de decisão e `model_params` com a temperatura do legado.
- [ ] O dataset e as regras do `kuro eval` estão no repositório, e o promote depende do eval.
- [ ] O shadow rodou até a concordância atingir a meta (`kuro runs agreement`).
- [ ] `MAX_CONCURRENT_RUNS` está dimensionado para o pico de conversas simultâneas.
- [ ] `kuro health` mostra o cadastro de chaves de modelo ligado e o provedor do agente com credencial.
- [ ] O modelo do agente é estável (nem `preview` nem `alias` em `kuro providers models`).
- [ ] Tools com efeito colateral tratam `X-Kuro-Dry-Run`, e os testes usam `dry_run`.
- [ ] Se as tools chamam o seu sistema pela rede Docker, a rede está no
      `docker-compose.override.yml` e o host em `TOOL_EGRESS_ALLOWLIST`.
