# Console web

| Página | O que tem |
|---|---|
| **Playground** `/chat` | conversa em streaming com qualquer agente conversacional; URL por conversa, histórico reidratado; edição de `dependencies`; anexos; 👍/👎 — e o 👎 pergunta o que mudar e ensina o agente |
| **Análise** `/analyze` | roda um agente analista: documento (ou anexo) entra, o objeto do `response_schema` sai |
| **Agentes** `/agents` | configuração (só os campos alterados vão no `PUT`), tipo do agente, parâmetros de geração e tempo limite, editor da saída estruturada com valores permitidos (`enum`); **Versões** (configuração inteira e prompt com diff) e **Promover**; **Aprendizado** (as regras vindas de feedback, editáveis, com histórico e rollback); execuções, com a concordância do shadow nos analistas; conversas e a aba Integração com o contrato vindo do backend |
| **Tools** `/tools` | criação e edição dos três tipos, com formulário próprio por tipo, campos de dentro de parâmetros `object`/`array`, botão **Testar** (com `dependencies`) e origem de cada parâmetro |
| **Conhecimento** `/knowledge` | documentos com status, escolha do embedder na criação, texto, arquivo ou site em qualquer coleção (sites com progresso, páginas e ressincronização), teste de busca semântica com score e origem |
| **Modelos** `/models` | credenciais por provedor, teste de chave |
| **Logs** `/logs` | sessões e execuções de todos os agentes; o panorama do período (KPIs contra o período anterior, a série no tempo, tools falhando, custo e versões por agente); cada sessão e cada run abrem em detalhe |
| **Trace** `/runs/[id]` | cascata de spans (agente → modelo → tools), versão da configuração, tokens, custo, avaliações, metadata de quem chamou e a referência do shadow |

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
