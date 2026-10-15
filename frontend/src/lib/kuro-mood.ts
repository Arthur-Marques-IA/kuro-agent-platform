/**
 * O humor do Kuro no canto do console (`components/brand/kuro-mascot.tsx`), separado do desenho
 * para ser testado sozinho (`kuro-mood.test.ts`, `npm test`).
 *
 * É a mesma regra do corvo do `kuro dash` (`crow_mood`, `feed_ok` e `feed_failed` em
 * src/agent_service/tui/app.py), alimentada pela consulta repetida de `/observability/runs`:
 * - a primeira consulta só registra o que já existe — abrir o console não reage a erro antigo;
 * - chegou execução com erro, interrompida ou com tool falhando: alerta (`error`) por 8 s;
 * - chegaram execuções sem problema: bate as asas (`working`) por 2 s;
 * - erro da API (401, 500...): alerta só ao entrar no erro — uma chave errada que dá 401 a cada
 *   consulta é configuração, e não pode deixar o corvo em alerta para sempre;
 * - o serviço não responde (o BFF diz que o agent-service está indisponível, ou o fetch nem
 *   chega): `offline`. Um 502 do próprio serviço (o Langfuse não respondeu) é erro da API, não queda;
 * - observabilidade desligada, ou aba oculta: `paused` (dormindo, sem consultar).
 *
 * Sem `Date.now()` aqui dentro: quem chama passa o `now`, e os testes ficam determinísticos.
 */
import type { RunSummary } from "./types";

export type KuroMood = "idle" | "working" | "error" | "paused" | "offline";

/** Quanto tempo o corvo fica em alerta depois de um erro (ALERT_SECONDS da TUI). */
export const ALERT_MS = 8000;
/** E batendo as asas depois de chegarem execuções (BUSY_SECONDS da TUI). */
export const BUSY_MS = 2000;
/** Quanto tempo o balão fica aberto depois de uma reação — mais que as asas, para dar tempo de ler. */
export const BALLOON_MS = 6000;
/** Intervalo da consulta com tudo bem; em erro dobra a cada falha, até `MAX_POLL_MS`. */
export const POLL_MS = 5000;
export const MAX_POLL_MS = 60_000;
/** Quantos run_id lembrar para saber o que é novo (a consulta traz bem menos que isso). */
export const SEEN_LIMIT = 200;

/** O que aconteceu por último — o texto do balão e para onde o clique leva. */
export interface KuroEvent {
  kind: "run" | "alert";
  text: string;
  href: string;
  at: number;
}

export interface KuroFeed {
  /** run_id já vistos, do mais novo para o mais antigo; `null` antes da primeira consulta. */
  seen: string[] | null;
  offline: boolean;
  /** A mensagem do erro da API em vigor (não offline); `null` = a última consulta deu certo. */
  apiError: string | null;
  /** A rota de execuções não existe neste serviço (501/404): fica quieto, como sem observabilidade. */
  unsupported: boolean;
  alertUntil: number;
  busyUntil: number;
  /** Falhas seguidas, para o backoff. */
  failures: number;
  last: KuroEvent | null;
}

export function initialFeed(backendReachable: boolean): KuroFeed {
  return {
    seen: null,
    offline: !backendReachable,
    apiError: null,
    unsupported: false,
    alertUntil: 0,
    busyUntil: 0,
    failures: 0,
    last: null,
  };
}

/** Execução que merece alerta: não terminou bem ou alguma tool falhou (`_bad` da TUI). */
export function isBadRun(run: Pick<RunSummary, "status" | "tool_failures">): boolean {
  return run.status !== "success" || (run.tool_failures ?? 0) > 0;
}

/** Teste do Playground (`dry_run`). */
export function isTestRun(run: Pick<RunSummary, "metadata">): boolean {
  return String(run.metadata?.dry_run ?? "").toLowerCase() === "true";
}

function runLabel(run: RunSummary): string {
  return `${run.agent_type}${isTestRun(run) ? " (teste)" : ""}`;
}

function problem(run: RunSummary): string {
  if (run.status === "error") return `erro em ${runLabel(run)}`;
  if (run.status === "interrupted") return `${runLabel(run)} interrompida`;
  const n = run.tool_failures ?? 0;
  return `${n === 1 ? "tool falhou" : `${n} tools falharam`} em ${runLabel(run)}`;
}

function runHref(run: RunSummary): string {
  return `/runs/${encodeURIComponent(run.run_id)}`;
}

/** O evento de um lote de execuções novas (mais novas primeiro, como a API devolve). */
export function describeFresh(fresh: RunSummary[], at: number): KuroEvent | null {
  if (fresh.length === 0) return null;
  const bad = fresh.filter(isBadRun);
  if (bad.length > 0) {
    const text = bad.length === 1 ? problem(bad[0]) : `${bad.length} execuções com problema · ${problem(bad[0])}`;
    return { kind: "alert", text, href: bad.length === 1 ? runHref(bad[0]) : "/logs/runs", at };
  }
  if (fresh.length === 1) {
    return { kind: "run", text: `nova execução de ${runLabel(fresh[0])}`, href: runHref(fresh[0]), at };
  }
  const text = `${fresh.length} execuções novas · a última de ${runLabel(fresh[0])}`;
  return { kind: "run", text, href: "/logs/runs", at };
}

/** A consulta respondeu. `runs` vem da API, mais novas primeiro. */
export function feedRuns(feed: KuroFeed, runs: RunSummary[], now: number): KuroFeed {
  const known = new Set(feed.seen ?? []);
  const fresh = feed.seen === null ? [] : runs.filter((r) => !known.has(r.run_id));
  const ids = runs.map((r) => r.run_id);
  const seen = [...ids, ...(feed.seen ?? []).filter((id) => !ids.includes(id))].slice(0, SEEN_LIMIT);
  const event = describeFresh(fresh, now);
  return {
    ...feed,
    seen,
    offline: false,
    apiError: null,
    unsupported: false,
    failures: 0,
    alertUntil: event?.kind === "alert" ? now + ALERT_MS : feed.alertUntil,
    busyUntil: event?.kind === "run" ? now + BUSY_MS : feed.busyUntil,
    last: event ?? feed.last,
  };
}

/** O `detail` do 502 que o BFF (`lib/api.ts`) devolve quando não alcança o agent-service. */
export const BFF_UNAVAILABLE = "agent-service indisponível no momento.";

/** A falha é o serviço fora do alcance, e não um erro que ele respondeu. */
export function isUnreachable(status: number, message: string): boolean {
  return status === 0 || (status === 502 && message === BFF_UNAVAILABLE);
}

/**
 * A consulta falhou. `status` é o HTTP da resposta, ou `0` quando o fetch nem chegou (o próprio
 * console caiu). Fora do ar: `isUnreachable`. 501/404 (rota que este serviço não tem) e 503 (trace
 * store do Langfuse sem configuração) deixam o corvo quieto; o resto é erro da API.
 */
export function feedFailure(feed: KuroFeed, status: number, message: string, now: number): KuroFeed {
  const failures = feed.failures + 1;
  if (isUnreachable(status, message)) {
    return { ...feed, offline: true, failures };
  }
  if (status === 501 || status === 404 || status === 503) {
    return { ...feed, offline: false, apiError: null, unsupported: true, failures };
  }
  const entering = message !== feed.apiError;
  return {
    ...feed,
    offline: false,
    apiError: message,
    failures,
    alertUntil: entering ? now + ALERT_MS : feed.alertUntil,
    last: entering ? { kind: "alert", text: `erro na API: ${message}`, href: "/logs/runs", at: now } : feed.last,
  };
}

/** Espera até a próxima consulta: `POLL_MS` com tudo bem, dobrando a cada falha seguida. */
export function nextPollDelay(feed: KuroFeed): number {
  return Math.min(POLL_MS * 2 ** Math.min(feed.failures, 8), MAX_POLL_MS);
}

export interface MoodContext {
  /** Observabilidade ligada no serviço (`/observability/config`). Desligada: dorme e não consulta. */
  observability: boolean;
  /** Aba oculta: a consulta para, e o corvo dorme (o "pausado" da TUI). */
  hidden: boolean;
}

/** O humor agora (`crow_mood` da TUI). */
export function moodOf(feed: KuroFeed, now: number, ctx: MoodContext): KuroMood {
  if (feed.offline) return "offline";
  if (!ctx.observability || feed.unsupported || ctx.hidden) return "paused";
  if (now < feed.alertUntil) return "error";
  if (now < feed.busyUntil) return "working";
  return "idle";
}

/** A frase de estado do tooltip — o que o corvo está "dizendo" agora. */
export function statusLine(feed: KuroFeed, mood: KuroMood, ctx: MoodContext): string {
  if (mood === "offline") return "API fora do ar — tentando de novo";
  if (!ctx.observability || feed.unsupported) return "observabilidade desligada — nada para acompanhar";
  if (ctx.hidden) return "pausado enquanto a aba está oculta";
  if (feed.apiError) return `erro na API: ${feed.apiError}`;
  if (feed.seen === null) return "conectando…";
  return "acompanhando as execuções";
}

/** O balão só abre logo depois de uma reação (execução nova, alerta). */
export function balloonEvent(feed: KuroFeed, now: number): KuroEvent | null {
  return feed.last && now - feed.last.at < BALLOON_MS ? feed.last : null;
}
