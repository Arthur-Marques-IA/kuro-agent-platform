# Console web

| Página | O que tem |
|---|---|
| **Playground** `/chat` | conversa em streaming com qualquer agente conversacional; URL por conversa, histórico reidratado; edição de `dependencies` e de blocos de `context`; simular atendente humano (`events`); anexos; 👍/👎 — e o 👎 pergunta o que mudar e ensina o agente |
| **Análise** `/analyze` | roda um agente analista: documento (ou anexo) entra, o objeto do `response_schema` sai, campo a campo (rótulo → valor, `enum` em selo; o JSON cru no botão **JSON**); os campos que o agente devolve aparecem numa tabela (tipo, obrigatório, descrição, valores permitidos) |
| **Agentes** `/agents` | configuração (só os campos alterados vão no `PUT`; [o formulário](#o-formulário-do-agente)), tipo do agente, parâmetros de geração e tempo limite, editor da saída estruturada com valores permitidos (`enum`); **Versões** (configuração inteira e prompt com diff) e **Promover**; **Aprendizado** (as regras vindas de feedback, editáveis, com histórico e rollback); execuções, com a concordância do shadow nos analistas; conversas e a aba Integração com o contrato vindo do backend |
| **Tools** `/tools` | lista agrupada por integração (prefixo do nome ou host da API; as de sistema recolhidas no fim), filtros em chip (tipo, efeito colateral, não classificada, desligada, sem agente), por linha a 1ª frase da descrição, método e host e os agentes que usam, e um detalhe com descrição inteira, URL e parâmetros; ligar/desligar pelo menu; criação e edição dos três tipos, com formulário próprio por tipo, campos de dentro de parâmetros `object`/`array`, botão **Testar** (com `dependencies`) e origem de cada parâmetro |
| **Conhecimento** `/knowledge` | documentos com status, escolha do embedder na criação, texto, arquivo ou site em qualquer coleção (sites com progresso, páginas e ressincronização), teste de busca semântica com score e origem |
| **Credenciais de modelos** `/models` | chaves dos provedores de LLM por provedor, teste de chave; a chave lida do ambiente do serviço (`GOOGLE_API_KEY`) aparece como linha somente leitura, só com o nome da variável. Não são as chaves de acesso ao Kuro (`ADMIN_API_KEY`/`RUNTIME_API_KEY`) |
| **Logs** `/logs` | o **Panorama** do período (KPIs contra o período anterior, a série no tempo, tools falhando, custo e versões por agente) e, para investigar, as listas de **Sessões** `/logs/sessions` e **Execuções** `/logs/runs`; cada sessão e cada run abrem em detalhe ([abaixo](#logs-panorama-sessões-e-execuções)) |
| **Trace** `/runs/[id]` | cascata de spans (agente → modelo → tools), versão da configuração, tokens, custo, avaliações, metadata de quem chamou e a referência do shadow |

**O Kuro no canto** (telas a partir de 1280 px, ao lado da sidebar): o corvo do [`kuro dash`](tui.md) consulta as execuções a cada 5 s (com backoff em erro; para com a aba oculta ou minimizado) e reage como lá — bate as asas com execução nova (testes do Playground contam), grasna de olho vermelho com erro, interrupção ou tool falhando, fica de cabeça para baixo com a API fora do ar e dorme com a observabilidade desligada; o balão diz o que chegou e o clique abre o run ou os Logs. Nas telas do formulário de agente (criar e a aba Configuração), cuja barra de salvar fica no rodapé, ele sai da tela.

## Busca global (`Ctrl+K`) e a barra lateral

A busca (`Ctrl+K`, `⌘K` no Mac, ou **Buscar** na sidebar) acha ações, páginas, agentes, **tools**
e **bases de conhecimento** (pelo nome ou pelo identificador; essas duas só aparecem com algo
digitado, até 6 de cada) e as conversas deste navegador. As tools e as collections são recarregadas
ao abrir a busca; se a chamada falhar, valem as que a página já tinha, sem aviso. Escolher uma tool
abre `/tools?q=<nome>` com a lista já filtrada; uma base abre `/knowledge?collection=<nome>`.

**Colar um id** leva direto a ele, pelo formato que o serviço gera:

| Colado | Opções |
|---|---|
| UUID (`3f2b8c1e-9a4d-4e7f-b2c6-0d1e2f3a4b5c`) | **Abrir execução** (`/runs/<id>`) e **Abrir sessão** (`/logs/sessions/<id>`): o `run_id` é um UUID, e o Playground também cria a sessão com um |
| `analyze-…`, `cli-…`, `mcp-…` seguidos de hex | **Abrir sessão** — os `session_id` que o `/analyze`, a CLI e o MCP criam |
| qualquer outra coisa | nenhuma: um `session_id` livre, mandado por quem integra, se acha pela busca das **Sessões** nos Logs |

Com a observabilidade ligada, a busca confere o id em segundo plano (uma chamada às execuções) e
diz "encontrada nos Logs" ou "não encontrada nos Logs"; se só a sessão existe, ela vem primeiro.
Enter abre na hora, sem esperar a conferência.

A barra lateral **Conversas deste navegador** lista só as conversas do Playground feitas com o
`user_id` deste navegador. As de integrações, da CLI e de outras pessoas estão nos Logs: o atalho
**Todas** ao lado do título abre `/logs/sessions`.

## O formulário do agente

A aba Configuração de um agente (e a tela Novo agente) é uma página só, com um índice das seções —
Identidade, Tipo, Prompt, Etapas (procedural), Modelo, Memória, Saída (analista), Contexto e Tools.
Em tela larga o índice fica fixo ao lado; em tela estreita vira uma barra no topo que rola de lado.
Um ponto amarelo marca a seção com alteração não salva; um vermelho, a seção com erro (depois da
primeira tentativa de salvar).

- **O que vai no `PUT`:** só os campos que você mexeu, comparados com o que o formulário mostrou
  ao abrir. Abrir e sair sem editar nunca fica "Alterações não salvas", nem com um serviço antigo
  que não devolve os campos opcionais (`model_params`, `timeout_seconds`, `session_summary`...).
- **Prompt:** uma instrução por linha; linhas em branco e espaços nas pontas não contam. **Tela
  cheia** abre um editor com a numeração das linhas, busca (Ctrl+F, Enter vai para a próxima) e a
  aba **Diferenças** contra a versão salva. Salvar um prompt alterado mostra antes o diff contra as
  instruções salvas e pede a confirmação da versão nova. Uma instrução salva com quebras de linha
  dentro (vinda da CLI ou da API) vira várias instruções se o prompt for editado aqui — o diff da
  revisão mostra isso antes de gravar.
- **Modelo:** o modelo salvo aparece na hora; a lista dos provedores chega depois. Se um provedor não
  responder em 10 s, fica a lista fixa dele, com o motivo na dica — salvar nunca espera a lista.

## Formulário de `dependencies`

O painel **Contexto** do Playground, o campo **Dependencies** da Análise e o **Testar** de uma tool
editam o objeto num formulário gerado dos campos declarados — os `dependency_fields` do agente ou,
numa tool `kind="api"`, os parâmetros de origem "modelo" (argumentos) e "dependência"
(`dependencies`, pelo nome em `dependency`). Cada campo tem o seu controle: texto, número, caixa
para `boolean`, seleção para `enum`, sub-formulário para `object` e lista para `array`; obrigatório
leva `*`, a descrição aparece como ajuda e tipo errado, obrigatório faltando ou valor fora do `enum`
são apontados na hora (quem recusa de fato é o backend, com 422). O botão **JSON** mostra o mesmo
objeto como texto, para colar um inteiro; os dois modos ficam sincronizados. Chaves que não estão
declaradas aparecem em **Outros campos** (chave/valor, valor lido como JSON quando dá) e nunca são
descartadas; agente sem campos declarados fica só com esse editor. No Playground o objeto continua
pré-preenchido com valores fictícios e guardado por agente no navegador.

## Entrada estruturada no Playground

O Playground manda a mesma [entrada estruturada](integracao.md#entrada-estruturada) que uma
integração, para testar um agente com o contexto e o histórico separados em campos:

- **Blocos de contexto.** O painel **Contexto** tem duas abas: **dependencies** (o formulário acima,
  valores tipados para as tools) e **Blocos de contexto** (texto para o modelo ler, enviado em
  `context`). Cada bloco tem `key`, título, conteúdo e a marca `volatile` (o bloco que muda todo
  turno, como o contexto temporal, e não conta como "o contexto mudou" na tela da sessão). Os blocos
  ficam guardados por agente no navegador, como as `dependencies`, e vão em toda mensagem. Antes do
  envio o console confere o que o servidor recusaria com 422: `key` e título preenchidos, `key`
  única (até 64 caracteres), título até 200, no máximo 30 blocos. Um bloco inválido aparece em
  vermelho no próprio campo, o botão **Contexto** fica "inválido" e a mensagem não sai. **Exemplo**
  preenche dois blocos (dados do contato e contexto temporal).
- **Simular atendente.** O botão **Atendente** do campo de mensagem troca o modo: o texto digitado
  vira uma fala do atendente humano, mostrada como bolha de atendente (tracejada, "vai junto da
  próxima mensagem", com × para descartar). Na próxima mensagem do contato essas falas vão em
  `events` (`role: "human_agent"`, com `at` do momento em que foram registradas) e a mensagem
  continua em `message`. "Tentar novamente" reenvia as falas que foram com aquela mensagem.
- **A conversa reaberta** relê a linha do tempo da sessão: a bolha do contato mostra só a fala (o
  `run_input` do AgentOS é o texto montado pelo servidor) e as falas do atendente voltam antes
  dela. Uma conversa cujo primeiro turno foi estruturado é nomeada pela fala, não pelo texto
  montado.
- **Código** mostra a chamada com `context` e `events` quando a tela os usa (as falas pendentes ou,
  sem elas, as que foram com a última mensagem), em cURL, JavaScript, Python e PHP.
- Sem blocos nem falas de atendente o corpo é exatamente o de antes: nada de `events: []` ou
  `context: []`.
- **Agente procedural** recusa `events` e `context` (422): o painel mostra só as `dependencies`,
  com o aviso, e o botão **Atendente** não aparece.

Na tela da sessão em Logs, uma conversa do Playground com blocos e atendente aparece igual a uma
vinda de integração.

## Logs: Panorama, Sessões e Execuções

Os Logs têm três abas, e cada uma é uma rota:

- **Panorama** (`/logs`) responde "como está?": números agregados do período, sem lista de casos.
  Filtra por período, agentes e "Incluir testes" (os runs com `dry_run` ficam de fora por padrão).
- **Sessões** (`/logs/sessions`): as conversas, com busca (sessão, usuário ou `conversation_id`),
  status, período e agentes. Cada uma abre a transcrição.
- **Execuções** (`/logs/runs`): cada run, com os mesmos filtros mais a **fila de revisão**
  (complexidade, tool falhou, efeito colateral, 👎, esconder mensagens curtas, amostra aleatória).
  Cada um abre o trace.

Os filtros do recorte:

- **Período**: os atalhos (24h, 7, 30 e 90 dias, todo o período) ou **Intervalo personalizado**, com
  as datas de início e fim, as duas inclusas (o dia final entra inteiro, no fuso do navegador). No
  Panorama, o "período anterior" das variações é a janela do mesmo tamanho logo antes do início.
- **Agentes**: vários de uma vez, com busca pelo nome ou pelo `agent_type` (que aparece embaixo do
  nome, porque há nomes repetidos). Nada marcado é todos. Um agente removido que veio num link
  continua na lista, marcado como removido.
- **Status** (Sessões e Execuções): vários de uma vez — "erro" e "interrompida" juntos, por exemplo.

O período e os agentes atravessam as abas, e o recorte inteiro fica na URL (`period` ou `from`/`to`,
`agent` repetido, `tests`, `status` separado por vírgula, `q`, `user_id`, `session_id`, `version`,
`sort`, `complexity`, `tool_failed`, `side_effect`, `hide_short`, `feedback`): recarregar ou mandar o
link volta ao mesmo lugar. Os links antigos com `agent=x` e `status=error` continuam valendo. A
amostra aleatória não entra na URL, então recarregar sorteia de novo.

Do Panorama para os casos, cada número leva às execuções que o explicam, no mesmo período, agentes
e testes:

- os **KPIs** são clicáveis: **Execuções** abre a lista do recorte; **Falharam**, as com erro ou
  interrompidas; **Responderam com tool falhando**, as que terminaram em sucesso com alguma tool
  falhando (o mesmo critério do número); **Custo** e **Latência p95** abrem a lista ordenada pelas
  **mais caras** ou **mais lentas** primeiro. A ordenação aparece como filtro fixo ("Ordem"),
  removível; runs sem custo (ou sem latência) ficam de fora dela, e com a amostra aleatória ela pausa;
- a seta de cada agente abre as execuções dele; na tabela de versões (abra o agente), a versão abre
  as execuções daquela versão; a seta de uma tool falhando abre as execuções com tool falhando nos
  agentes em que ela falhou (a lista não recorta pelo nome da tool).

Usuário, sessão, versão e ordenação aparecem como filtros fixos, removíveis; com usuário ou sessão,
a lista mostra também os números deles. Mudar os agentes tira o filtro de versão.

Links antigos: `/logs?user_id=…` vai para as sessões do usuário e `/logs?session_id=…` para as
execuções da sessão.

## A tela da execução

`/runs/{run_id}` abre de qualquer lista (Logs, sessão, aba Execuções do agente). Além dos spans,
ela se adapta ao tipo de run:

- **Analista (`/analyze`)**: a **Entrada** é o `document`, com as quebras de linha preservadas, e a
  **Saída** é a árvore dos campos do `result` (objetos e arrays aninhados, na ordem e com os rótulos
  do `response_schema`; o caminho de cada campo, como `parcelas[0].valor`, aparece ao passar o mouse
  e é o mesmo do `kuro eval`). Um campo com `enum` aparece como selo, em alerta quando o valor está
  fora dos permitidos; o botão **JSON** mostra a saída crua. O título é o agente e a `fase` da metadata, não o documento.
- **Procedural**: **Procedimento neste turno** mostra a etapa em que o turno terminou, o que já foi
  coletado, o que falta, os valores recusados e as ações — o estado daquele turno, não o atual.
  Runs gravados antes desta versão não têm o quadro.
- **Linha do tempo**: quando o run tem uma chave de correlação na metadata (`ciclo_id` por padrão,
  configurável em `CORRELATION_METADATA_KEYS`), aparecem todos os runs com o mesmo valor, de
  qualquer agente, em ordem — triagem → consulta à base → redação, por exemplo —, com o tempo
  desde o primeiro, a fase e o status. Cada linha abre o run.
- **Avaliar esta execução**: 👍 grava o score `feedback`. O 👎 depende do agente:
  - no **analista**, pede o que estava errado e a **saída correta**, que começa preenchida com a
    saída atual (edite só os campos errados). Ela vira a referência do run, que sai em
    `kuro runs export` como caso de eval — o caminho é corrigir as instructions num draft e
    conferir com `kuro eval`, não uma regra global. Fato errado (dado da base) se corrige na base
    de conhecimento;
  - no **conversacional** e no **procedural**, registra o 👎 com comentário e oferece o
    **ensinar**, que mescla o comentário nas regras do agente lendo a conversa da sessão.

O título é a fala pura do contato (`input_message`; num run legado, o texto que chegou), e a
migalha volta para **Logs › Execuções** (e para a sessão, quando o run tem uma). Num agente que
conversa, o cartão **Entrada** mostra o que entrou como texto: os eventos do turno (quem, quando,
documento, a fala nova) e os blocos de contexto, cada um no seu cartão. Run sem entrada estruturada
mostra o texto inteiro com as quebras de linha. Nos spans, uma entrada com campos de texto de várias
linhas sai um bloco por campo, não como JSON escapado.

## A tela da sessão

`/logs/sessions/{id}` lê a linha do tempo da sessão (`GET /observability/sessions/{id}/timeline`,
ver [integracao.md](integracao.md#linha-do-tempo-da-sessão)): a tela não recorta texto, só lê os
campos que a entrada estruturada do `/chat` gravou. Capturas em `docs/telas/sessao-*.png`.

- **Cabeçalho:** o nome do contato (`contact_name`; sem ele, o `user_id`), o agente, o canal, o
  início, a duração, os turnos e o custo da sessão inteira. O `session_id` fica ao lado, copiável.
- **Conversa:** uma bolha por evento, com autor e horário. Contato à direita; agente à esquerda;
  atendente humano à esquerda, em outra cor e com o rótulo; sistema numa linha discreta no meio.
  O que foi enviado em nome do agente (`agent_sent`) igual à resposta anterior dele não aparece de
  novo (espaços e quebras de linha não contam); se saiu diferente, aparece com o selo
  **enviado diferente**. Documento (`kind: document`) vem recolhido com o nome do arquivo e abre
  em markdown.
- **Tools:** entre a fala e a resposta, uma linha por chamada — o nome legível (o verbo no fim do
  nome, como `regente_r6_decidir` → "decidiu"; senão o rótulo cadastrado da tool), os argumentos
  simples em ordem e o começo do resultado: `decidiu: transferir → Secretaria`. Selos para
  **falhou** (o critério de `tool_failures`), **não encontrado** (404, neutro) e **efeito
  colateral**. Clicar abre os argumentos e o resultado inteiros (lido do trace do run).
- **Contexto:** em tela larga, um painel fixo ao lado; em tela estreita, o botão **Contexto** abre
  uma gaveta. Um cartão por bloco, com o `title`. O painel acompanha o turno em foco (o que está no
  meio da tela, ou o que foi clicado); turno sem blocos mostra o último turno anterior que tinha.
  Bloco que mudou desde o turno anterior fica destacado (**mudou**, ou **novo**) e tem
  **ver diferença**, linha a linha contra o `previous_content`. Bloco `volatile` nunca conta como
  mudança.
- **Entrada não estruturada:** run que chegou só com `message` (o legado, ou antes da entrada
  estruturada) aparece como um cartão recolhido com o texto original e o aviso de que a integração
  ainda manda tudo em `message`. Ele continua com as tools e a resposta.
- **Analista:** os runs de analista (ex.: `grupo-18`, com triagem e redação) aparecem como cartões
  recolhidos com o agente, o status e o link para a execução; abertos, mostram a entrada e a saída.
- **Embaixo, em abas:** as execuções da sessão, o que o agente lembra (resumo da sessão e memórias
  do contato) e os gráficos. O "feedback sobre a conversa inteira" fica no fim da conversa, no
  conversacional e no procedural.
- Mais de 100 turnos: **Carregar turnos mais novos** pede a página seguinte. Com
  `TRACE_STORE_BACKEND=langfuse` a conversa não existe (501) e a tela mostra só as abas.

Na lista de **Sessões**, cada linha mostra o nome do contato (quando há), a prévia da última fala e o
canal; na de **Execuções**, a coluna da mensagem é a fala pura (`input_message`), com o texto antigo
quando o run não é estruturado.

## O gráfico do panorama (Logs)

A série no tempo tem três medidas, nas abas acima do gráfico:

- **Volume** responde duas perguntas, em dois gráficos alinhados pelo mesmo período:
  - **Execuções por resultado**: barras empilhadas com as falhas **na base**, junto ao eixo
    (erro ou interrompida, depois tool falhando, sucesso no topo). Num dia de 640 execuções e 42
    erros, os erros ficam onde o olho os acha, e não como um fio no topo da barra;
  - **Taxa de falha**: das execuções de cada período, quantas falharam e quantas responderam com
    tool falhando, em %, com uma linha fina na média do período. Ela não depende do volume: um dia
    calmo com 10% de erro aparece tão alto quanto um dia cheio com 10%. São dois gráficos, e não
    um com dois eixos, de propósito.
- **Custo**, em barras.
- **Latência p95**, em linha.

Passar o mouse (ou focar pelo teclado) num período mostra o detalhe dele, e nos dois gráficos do
volume marca o mesmo período nos dois. Um período com **menos de 5 execuções** sai com o ponto
vazado, e o detalhe avisa que é um valor só, não uma tendência: 1 falha em 4 execuções dá 25% e
não diz nada. Esses períodos também não definem a escala da taxa. Se um deles passar do teto, fica
na borda de cima como um triângulo vazado, e o valor real está no detalhe e na tabela. O botão
**Tabela** mostra os mesmos números, com as taxas de cada período.

As cores de status são as mesmas nos dois temas e foram conferidas contra daltonismo
(`frontend/src/app/globals.css`, `--viz-*`). A linha de "tool falhando" usa um âmbar mais escuro
que o das barras, porque uma linha fina precisa de mais contraste no fundo claro.

Atalhos: `Ctrl/⌘+K` (paleta de comandos) e `Ctrl/⌘+B` (recolher a sidebar).
Tema claro, escuro ou do sistema.

O navegador só fala com o Next.js. Os Route Handlers em `frontend/src/app/api/**`
fazem proxy para o `agent-service` usando `AGENT_SERVICE_URL`, que só existe no
servidor: sem CORS e sem expor o backend. O console não tem login próprio: o `user_id` do
console fica no `localStorage` do navegador.

### Login (basic_auth no profile `tls`)

Como o servidor do console chama a API com a chave admin, quem abre o console tem acesso total.
Na máquina local isso não muda nada: o console fica em `127.0.0.1:3000`. Publicado pelo profile
`tls` (`KURO_CONSOLE_DOMAIN`), o Caddy pede usuário e senha antes de qualquer página ou rota
`/api/**` do console, e se recusa a subir sem eles:

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'uma-senha-forte'
# no .env, o hash entre aspas simples (o $ do bcrypt não pode ser interpolado):
#   CONSOLE_BASIC_AUTH_USER=admin
#   CONSOLE_BASIC_AUTH_HASH='$2a$14$...'
docker compose --profile ui --profile tls up -d
```

Sem as duas variáveis, o Caddy não publica nada e diz o que falta em
`docker compose --profile tls logs caddy`. A API (`/chat`, `/analyze`, `/mcp`, `/health`) não
passa por esse login: ela continua com a chave dela. Detalhes, inclusive o cuidado com o `$` do
hash, em [Login do console](operacao.md#login-do-console-basic_auth).
