/**
 * Os filtros dos Logs na URL: o Panorama (`/logs`), as Sessões (`/logs/sessions`), as Execuções
 * (`/logs/runs`) e a aba RAG (`/logs/rag`) leem e escrevem a mesma query string. Um link do Panorama para as execuções de
 * um agente, de uma versão ou com tool falhando é só uma URL; recarregar ou compartilhar a página
 * volta ao mesmo recorte. Sem dependência de React: testado por `logs-query.test.ts`.
 */

export const PERIODS = [
  { value: "1d", label: "Últimas 24h", days: 1 },
  { value: "7d", label: "Últimos 7 dias", days: 7 },
  { value: "30d", label: "Últimos 30 dias", days: 30 },
  { value: "90d", label: "Últimos 90 dias", days: 90 },
  { value: "all", label: "Todo o período", days: null },
] as const;

export type Period = (typeof PERIODS)[number]["value"];

export const DEFAULT_PERIOD: Period = "7d";

export const RUN_STATUSES = ["success", "error", "interrupted"] as const;
export type RunStatusValue = (typeof RUN_STATUSES)[number];

/** Ordenações da lista de execuções além da padrão (mais recentes primeiro). */
export const RUN_SORTS = [
  { value: "cost", label: "Mais caras primeiro" },
  { value: "latency", label: "Mais lentas primeiro" },
] as const;
export type RunSortValue = (typeof RUN_SORTS)[number]["value"];

/** Recortes da aba RAG: o que buscou na base, a busca que voltou vazia e o trecho marcado errado. */
export const RAG_FILTERS = [
  { value: "searched", label: "Buscaram na base" },
  { value: "empty", label: "Busca sem trecho" },
  { value: "flagged", label: "Trecho marcado errado" },
] as const;
export type RagFilterValue = (typeof RAG_FILTERS)[number]["value"];

export type LogsPath = "/logs" | "/logs/sessions" | "/logs/runs" | "/logs/rag";

export interface LogsQuery {
  /** Atalho do período. Ignorado quando há intervalo livre (`from`/`to`). */
  period: Period;
  /** Intervalo livre, dias locais `AAAA-MM-DD` com as duas pontas inclusas. Um dos dois pode faltar. */
  from: string;
  to: string;
  /** Vazio = todos os agentes. */
  agents: string[];
  /** Inclui os testes do Playground (dry_run), que ficam fora das métricas por padrão. */
  tests: boolean;
  /** Vazio = todos os status. */
  statuses: RunStatusValue[];
  search: string;
  /** Filtros fixos, vindos de links (inspector do chat, uma versão no Panorama): aparecem como chips removíveis. */
  userId: string;
  sessionId: string;
  version: number | null;
  /** Ordenação das execuções (os KPIs de custo e latência levam para cá). Vazio = mais recentes. */
  sort: RunSortValue | "";
  // A fila de revisão (só na lista de execuções).
  complexity: number[];
  toolFailed: boolean;
  sideEffect: boolean;
  hideShort: boolean;
  feedbackDown: boolean;
  // A aba RAG.
  rag: RagFilterValue;
  /** Só runs de agentes que consultavam esta collection. Vazio = todas. */
  collection: string;
}

export const EMPTY_LOGS_QUERY: LogsQuery = {
  period: DEFAULT_PERIOD,
  from: "",
  to: "",
  agents: [],
  tests: false,
  statuses: [],
  search: "",
  userId: "",
  sessionId: "",
  version: null,
  sort: "",
  complexity: [],
  toolFailed: false,
  sideEffect: false,
  hideShort: false,
  feedbackDown: false,
  rag: "searched",
  collection: "",
};

export type SearchParams = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
}

/** Todos os valores de um parâmetro repetido (`agent=a&agent=b`), sem vazios nem repetidos. */
function all(value: string | string[] | undefined): string[] {
  const values = (Array.isArray(value) ? value : value == null ? [] : [value]).map((v) => v.trim()).filter(Boolean);
  return [...new Set(values)];
}

function flag(value: string | string[] | undefined): boolean {
  return ["1", "true"].includes(first(value));
}

/**
 * `AAAA-MM-DD` de um dia que existe no calendário, entre 2000 e 2100, ou vazio. O campo de data do navegador
 * avisa a cada tecla do ano (`0002-`, `0020-`, `0202-`): esses meios do caminho não podem virar recorte.
 */
export function isoDay(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return "";
  const [year, month, day] = match.slice(1).map(Number);
  if (year < 2000 || year > 2100) return "";
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day ? value : "";
}

/** `URLSearchParams` vira objeto com os parâmetros repetidos como lista (o `Object.fromEntries` fica com o último). */
export function searchParamsRecord(params: URLSearchParams): SearchParams {
  const out: Record<string, string | string[]> = {};
  for (const key of new Set(params.keys())) {
    const values = params.getAll(key);
    out[key] = values.length > 1 ? values : values[0];
  }
  return out;
}

export function parseLogsQuery(params: SearchParams): LogsQuery {
  const period = first(params.period);
  const version = Number(first(params.version));
  const sort = first(params.sort);
  const rag = first(params.rag);
  const complexity = first(params.complexity)
    .split(",")
    .map(Number)
    .filter((level) => [1, 2, 3].includes(level));
  // `status=error,interrupted` e `status=error&status=interrupted` valem igual; um valor só (o formato antigo) também.
  const statuses = all(params.status)
    .flatMap((s) => s.split(","))
    .filter((s): s is RunStatusValue => (RUN_STATUSES as readonly string[]).includes(s));
  let from = isoDay(first(params.from));
  let to = isoDay(first(params.to));
  if (from && to && from > to) [from, to] = [to, from];
  return {
    period: PERIODS.some((p) => p.value === period) ? (period as Period) : DEFAULT_PERIOD,
    from,
    to,
    agents: all(params.agent),
    tests: flag(params.tests),
    statuses: RUN_STATUSES.filter((s) => statuses.includes(s)),
    search: first(params.q),
    userId: first(params.user_id),
    sessionId: first(params.session_id),
    version: Number.isInteger(version) && version >= 1 ? version : null,
    sort: RUN_SORTS.some((s) => s.value === sort) ? (sort as RunSortValue) : "",
    complexity: [...new Set(complexity)].sort(),
    toolFailed: flag(params.tool_failed),
    sideEffect: flag(params.side_effect),
    hideShort: flag(params.hide_short),
    feedbackDown: first(params.feedback) === "down",
    rag: RAG_FILTERS.some((r) => r.value === rag) ? (rag as RagFilterValue) : "searched",
    collection: first(params.collection),
  };
}

/** O intervalo livre está ligado (vale mais que o atalho do período). */
export function hasCustomRange(query: Pick<LogsQuery, "from" | "to">): boolean {
  return Boolean(query.from || query.to);
}

/** Query string (sem o `?`) só com o que difere do padrão — a URL limpa é a sem filtro. */
export function logsQueryString(query: Partial<LogsQuery>): string {
  const q = { ...EMPTY_LOGS_QUERY, ...query };
  const out = new URLSearchParams();
  if (hasCustomRange(q)) {
    if (q.from) out.set("from", q.from);
    if (q.to) out.set("to", q.to);
  } else if (q.period !== DEFAULT_PERIOD) {
    out.set("period", q.period);
  }
  for (const agent of q.agents) out.append("agent", agent);
  if (q.tests) out.set("tests", "1");
  if (q.statuses.length) out.set("status", q.statuses.join(","));
  if (q.search.trim()) out.set("q", q.search.trim());
  if (q.userId) out.set("user_id", q.userId);
  if (q.sessionId) out.set("session_id", q.sessionId);
  if (q.version != null) out.set("version", String(q.version));
  if (q.sort) out.set("sort", q.sort);
  if (q.complexity.length) out.set("complexity", q.complexity.join(","));
  if (q.toolFailed) out.set("tool_failed", "1");
  if (q.sideEffect) out.set("side_effect", "1");
  if (q.hideShort) out.set("hide_short", "1");
  if (q.feedbackDown) out.set("feedback", "down");
  if (q.rag !== "searched") out.set("rag", q.rag);
  if (q.collection) out.set("collection", q.collection);
  return out.toString();
}

export function logsHref(path: LogsPath, query: Partial<LogsQuery> = {}): string {
  const qs = logsQueryString(query);
  return qs ? `${path}?${qs}` : path;
}

/** O que atravessa as abas: o recorte do período e dos agentes vale para o Panorama e para as listas. */
export function sharedLogsQuery(query: LogsQuery): Partial<LogsQuery> {
  return { period: query.period, from: query.from, to: query.to, agents: query.agents, tests: query.tests };
}

/** Meia-noite local do dia `AAAA-MM-DD` mais `offset` dias (somar no calendário atravessa horário de verão). */
function localMidnight(day: string, offset = 0): Date {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date + offset);
}

/**
 * `since`/`until` (ISO) que vão à API. O atalho conta para trás a partir de `now`; o intervalo livre vai
 * da meia-noite local de `from` até a meia-noite seguinte a `to` (o dia final inteiro entra). `null` = sem
 * limite daquele lado. `now` é parâmetro porque `Date.now()` não pode rodar durante a renderização.
 */
export function logsRange(query: Pick<LogsQuery, "period" | "from" | "to">, now: number): { since: string | null; until: string | null } {
  if (hasCustomRange(query)) {
    // Pontas trocadas (digitadas fora de ordem) valem como o intervalo entre elas.
    const [from, to] = query.from && query.to && query.from > query.to ? [query.to, query.from] : [query.from, query.to];
    return {
      since: from ? localMidnight(from).toISOString() : null,
      until: to ? localMidnight(to, 1).toISOString() : null,
    };
  }
  const days = PERIODS.find((p) => p.value === query.period)?.days;
  return { since: days ? new Date(now - days * 86_400_000).toISOString() : null, until: null };
}

/** `AAAA-MM-DD` do dia local de um instante — para preencher o intervalo livre a partir de hoje. */
export function localDay(moment: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${moment.getFullYear()}-${pad(moment.getMonth() + 1)}-${pad(moment.getDate())}`;
}

/** Parâmetros da API com lista virando parâmetro repetido (`agent_type=a&agent_type=b`), como o FastAPI espera. */
export function apiSearchParams(params: Record<string, string | string[]>): URLSearchParams {
  const out = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    for (const item of Array.isArray(value) ? value : [value]) out.append(key, item);
  }
  return out;
}

/**
 * Destino de um link antigo para `/logs`: até aqui a página tinha as listas embaixo do panorama, e
 * o inspector do chat apontava para `/logs?user_id=` (as sessões do usuário) e `/logs?session_id=`
 * (as execuções da sessão). `null` quando o link já é do Panorama.
 */
export function legacyLogsRedirect(params: SearchParams): string | null {
  const query = parseLogsQuery(params);
  if (query.sessionId) return logsHref("/logs/runs", query);
  if (query.userId) return logsHref("/logs/sessions", query);
  return null;
}
