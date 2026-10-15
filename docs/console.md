# Console web

| Página | O que tem |
|---|---|
| **Playground** `/chat` | conversa em streaming com qualquer agente conversacional; URL por conversa, histórico reidratado; edição de `dependencies`; anexos; 👍/👎 — e o 👎 pergunta o que mudar e ensina o agente |
| **Análise** `/analyze` | roda um agente analista: documento (ou anexo) entra, o objeto do `response_schema` sai |
| **Agentes** `/agents` | configuração (só os campos alterados vão no `PUT`), tipo do agente, parâmetros de geração e tempo limite, editor da saída estruturada com valores permitidos (`enum`); **Versões** (configuração inteira e prompt com diff) e **Promover**; **Aprendizado** (as regras vindas de feedback, editáveis, com histórico e rollback); execuções, com a concordância do shadow nos analistas; conversas e a aba Integração com o contrato vindo do backend |
| **Tools** `/tools` | lista agrupada por integração (prefixo do nome ou host da API; as de sistema recolhidas no fim), filtros em chip (tipo, efeito colateral, não classificada, desligada, sem agente), por linha a 1ª frase da descrição, método e host e os agentes que usam, e um detalhe com descrição inteira, URL e parâmetros; ligar/desligar pelo menu; criação e edição dos três tipos, com formulário próprio por tipo, campos de dentro de parâmetros `object`/`array`, botão **Testar** (com `dependencies`) e origem de cada parâmetro |
| **Conhecimento** `/knowledge` | documentos com status, escolha do embedder na criação, texto, arquivo ou site em qualquer coleção (sites com progresso, páginas e ressincronização), teste de busca semântica com score e origem |
| **Modelos** `/models` | credenciais por provedor, teste de chave |
| **Logs** `/logs` | o **Panorama** do período (KPIs contra o período anterior, a série no tempo, tools falhando, custo e versões por agente) e, para investigar, as listas de **Sessões** `/logs/sessions` e **Execuções** `/logs/runs`; cada sessão e cada run abrem em detalhe ([abaixo](#logs-panorama-sessões-e-execuções)) |
| **Trace** `/runs/[id]` | cascata de spans (agente → modelo → tools), versão da configuração, tokens, custo, avaliações, metadata de quem chamou e a referência do shadow |

**O Kuro no canto** (telas a partir de 1280 px, ao lado da sidebar): o corvo do [`kuro dash`](tui.md) consulta as execuções a cada 5 s (com backoff em erro; para com a aba oculta ou minimizado) e reage como lá — bate as asas com execução nova (testes do Playground contam), grasna de olho vermelho com erro, interrupção ou tool falhando, fica de cabeça para baixo com a API fora do ar e dorme com a observabilidade desligada; o balão diz o que chegou e o clique abre o run ou os Logs.

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
servidor: sem CORS e sem expor o backend. Não há login no MVP: o `user_id` do
console fica no `localStorage` do navegador.
