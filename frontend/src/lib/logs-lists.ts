/**
 * Lógica pura das listas dos Logs (Execuções e Sessões), sem React: as colunas do CSV exportado, a
 * paginação até o teto da exportação, a mescla da atualização automática e a sondagem barata que
 * descobre se o vazio é só porque os testes estão de fora. Testado por `logs-lists.test.ts`.
 */

import type { CsvColumn } from "./csv.ts";
import type { LogSessionSummary, RunSummary } from "./types";

// -- Atualização automática --------------------------------------------------------

/** Segundos entre atualizações; 0 = desligada (o padrão). */
export const AUTO_REFRESH_OPTIONS = [0, 10, 30, 60] as const;
export type AutoRefreshSeconds = (typeof AUTO_REFRESH_OPTIONS)[number];
export const AUTO_REFRESH_STORAGE_KEY = "kuro:logs:auto-refresh";

/** O valor guardado no navegador; qualquer coisa fora das opções vale "desligada". */
export function parseAutoRefresh(raw: string | null | undefined): AutoRefreshSeconds {
  const n = Number(raw);
  return (AUTO_REFRESH_OPTIONS as readonly number[]).includes(n) ? (n as AutoRefreshSeconds) : 0;
}

/**
 * A primeira página buscada de novo, mesclada ao que já está na tela: o que mudou (status, custo,
 * feedback) troca no lugar, e o que é novo entra no topo só quando a lista é a das mais recentes
 * (`prepend`) — numa lista ordenada por custo ou latência, o lugar dele não é o topo. O que já foi
 * carregado com "Carregar mais" fica: a lista não encolhe nem pula.
 */
export function mergeFirstPage<T>(current: T[], fresh: T[], key: (item: T) => string, prepend: boolean): T[] {
  const byKey = new Map(fresh.map((item) => [key(item), item]));
  const known = new Set(current.map(key));
  const updated = current.map((item) => byKey.get(key(item)) ?? item);
  if (!prepend) return updated;
  const added = fresh.filter((item) => !known.has(key(item)));
  return added.length ? [...added, ...updated] : updated;
}

// -- "Incluir testes" no estado vazio ------------------------------------------------

/**
 * Os parâmetros da sondagem: o mesmo recorte, com os testes e uma linha só. Como a lista sem testes
 * veio vazia, qualquer execução que voltar aqui é um teste. Amostra e ordenação não mudam a resposta.
 */
export function testsProbeParams(params: Record<string, string | string[]>): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {};
  for (const [key, value] of Object.entries(params)) {
    if (["sample", "_seed", "sort", "cursor", "limit", "include_dry_run"].includes(key)) continue;
    out[key] = value;
  }
  out.include_dry_run = "true";
  out.limit = "1";
  return out;
}

// -- Exportação -----------------------------------------------------------------------

/** Teto de linhas do CSV de execuções (paginado no servidor, 100 por página). */
export const RUNS_EXPORT_LIMIT = 5000;
export const RUNS_EXPORT_PAGE = 100;
/** `/observability/sessions` não pagina: uma chamada traz até 200 sessões, as de atividade mais recente. */
export const SESSIONS_EXPORT_LIMIT = 200;

export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
}

/**
 * Segue o `next_cursor` até acabar ou chegar ao teto. Uma página pode vir vazia e ainda ter cursor
 * (traces de fora do serviço), por isso também há um limite de páginas. `truncated` diz que sobrou.
 */
export async function collectPages<T>(
  fetchPage: (cursor: string | null) => Promise<Page<T>>,
  { max, pageSize, onProgress }: { max: number; pageSize: number; onProgress?: (count: number) => void }
): Promise<{ items: T[]; truncated: boolean }> {
  const items: T[] = [];
  const maxPages = Math.ceil(max / pageSize) * 2;
  let cursor: string | null = null;
  for (let page = 0; page < maxPages; page++) {
    const result: Page<T> = await fetchPage(cursor);
    items.push(...result.items);
    onProgress?.(Math.min(items.length, max));
    cursor = result.next_cursor ?? null;
    if (items.length >= max) return { items: items.slice(0, max), truncated: items.length > max || cursor != null };
    if (!cursor) return { items, truncated: false };
  }
  return { items, truncated: cursor != null };
}

export const RUN_CSV_COLUMNS: CsvColumn<RunSummary>[] = [
  { header: "started_at", value: (r) => r.started_at },
  { header: "run_id", value: (r) => r.run_id },
  { header: "agent_type", value: (r) => r.agent_type },
  { header: "agent_name", value: (r) => r.agent_name },
  { header: "status", value: (r) => r.status },
  { header: "status_message", value: (r) => r.status_message },
  // A fala pura (entrada estruturada); em run legado, o texto inteiro que chegou — o mesmo da tela.
  { header: "message", value: (r) => r.input_message || r.message },
  { header: "output", value: (r) => r.output },
  { header: "user_id", value: (r) => r.user_id },
  { header: "session_id", value: (r) => r.session_id },
  { header: "endpoint", value: (r) => r.endpoint },
  { header: "agent_version", value: (r) => r.agent_version },
  { header: "prompt_version", value: (r) => r.prompt_version },
  { header: "model", value: (r) => r.model },
  { header: "complexity", value: (r) => r.complexity },
  { header: "tool_calls", value: (r) => r.tool_calls },
  { header: "tool_failures", value: (r) => r.tool_failures },
  { header: "latency_ms", value: (r) => r.latency_ms },
  { header: "input_tokens", value: (r) => r.input_tokens },
  { header: "output_tokens", value: (r) => r.output_tokens },
  { header: "total_tokens", value: (r) => r.total_tokens },
  { header: "cost_usd", value: (r) => r.cost_usd },
  { header: "feedback_up", value: (r) => r.feedback_up },
  { header: "feedback_down", value: (r) => r.feedback_down },
  { header: "trace_id", value: (r) => r.trace_id },
];

export const SESSION_CSV_COLUMNS: CsvColumn<LogSessionSummary>[] = [
  { header: "last_activity", value: (s) => s.last_activity },
  { header: "started_at", value: (s) => s.started_at },
  { header: "session_id", value: (s) => s.session_id },
  { header: "contact_name", value: (s) => s.contact_name },
  { header: "channel", value: (s) => s.channel },
  { header: "user_id", value: (s) => s.user_id },
  { header: "agent_types", value: (s) => s.agent_types.join(" ") },
  { header: "run_count", value: (s) => s.run_count },
  { header: "error_count", value: (s) => s.error_count },
  { header: "total_tokens", value: (s) => s.total_tokens },
  { header: "cost_usd", value: (s) => s.cost_usd },
  { header: "feedback_up", value: (s) => s.feedback_up },
  { header: "feedback_down", value: (s) => s.feedback_down },
  { header: "last_message", value: (s) => s.last_message },
];

/** `kuro-execucoes-2026-11-11-1432.csv`: o nome diz o que é e quando saiu. */
export function exportFileName(kind: "execucoes" | "sessoes", now: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
  return `kuro-${kind}-${stamp}.csv`;
}
