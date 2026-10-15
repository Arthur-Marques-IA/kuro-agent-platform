/**
 * O humor do Kuro no canto do console: a mesma regra do corvo do `kuro dash`
 * (src/agent_service/tui/app.py). Roda com `npm test` (`node --experimental-strip-types --test`).
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ALERT_MS,
  BFF_UNAVAILABLE,
  BALLOON_MS,
  BUSY_MS,
  MAX_POLL_MS,
  POLL_MS,
  SEEN_LIMIT,
  balloonEvent,
  feedFailure,
  feedRuns,
  initialFeed,
  moodOf,
  nextPollDelay,
  statusLine,
} from "./kuro-mood.ts";
import type { RunSummary } from "./types";

const ON = { observability: true, hidden: false };

function run(id: string, extra: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: id,
    trace_id: id,
    agent_type: "r8-v2",
    started_at: "2026-10-14T12:00:00Z",
    status: "success",
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    feedback_up: 0,
    feedback_down: 0,
    ...extra,
  };
}

/** Feed já com a primeira consulta feita (a que só registra o que existe). */
function seeded(runs: RunSummary[] = [run("a")], now = 0) {
  return feedRuns(initialFeed(true), runs, now);
}

describe("primeira consulta", () => {
  it("só registra o que já existe: erro antigo não alarma quem abre o console", () => {
    const feed = seeded([run("a", { status: "error" }), run("b")], 1000);
    assert.equal(moodOf(feed, 1000, ON), "idle");
    assert.equal(feed.last, null);
    assert.deepEqual(feed.seen, ["a", "b"]);
  });

  it("começa offline quando o layout não alcançou a API", () => {
    assert.equal(moodOf(initialFeed(false), 0, ON), "offline");
    assert.equal(moodOf(initialFeed(true), 0, ON), "idle");
  });
});

describe("execuções novas", () => {
  it("bate as asas por BUSY_MS e o balão aponta o run", () => {
    const feed = feedRuns(seeded(), [run("b"), run("a")], 10_000);
    assert.equal(moodOf(feed, 10_000, ON), "working");
    assert.equal(moodOf(feed, 10_000 + BUSY_MS, ON), "idle");
    assert.equal(feed.last?.text, "nova execução de r8-v2");
    assert.equal(feed.last?.href, "/runs/b");
  });

  it("marca teste do Playground e agrupa várias, levando aos Logs", () => {
    const feed = feedRuns(seeded(), [run("c", { metadata: { dry_run: "true" } }), run("b"), run("a")], 0);
    assert.equal(feed.last?.text, "2 execuções novas · a última de r8-v2 (teste)");
    assert.equal(feed.last?.href, "/logs/runs");
  });

  it("erro, interrupção ou tool falhando alarmam por ALERT_MS, e erro ganha de sucesso no mesmo lote", () => {
    const feed = feedRuns(seeded(), [run("c"), run("b", { status: "error" }), run("a")], 5000);
    assert.equal(moodOf(feed, 5000, ON), "error");
    assert.equal(moodOf(feed, 5000 + ALERT_MS - 1, ON), "error");
    assert.equal(moodOf(feed, 5000 + ALERT_MS, ON), "idle");
    assert.equal(feed.last?.text, "erro em r8-v2");
    assert.equal(feed.last?.href, "/runs/b");

    const tool = feedRuns(seeded(), [run("b", { tool_failures: 2 }), run("a")], 0);
    assert.equal(moodOf(tool, 0, ON), "error");
    assert.equal(tool.last?.text, "2 tools falharam em r8-v2");

    const stopped = feedRuns(seeded(), [run("b", { status: "interrupted" }), run("a")], 0);
    assert.equal(stopped.last?.text, "r8-v2 interrompida");
  });

  it("nada novo não reage nem apaga o último evento", () => {
    const first = feedRuns(seeded(), [run("b"), run("a")], 0);
    const again = feedRuns(first, [run("b"), run("a")], 3000);
    assert.equal(again.last, first.last);
    assert.equal(again.busyUntil, first.busyUntil);
  });

  it("lembra no máximo SEEN_LIMIT ids", () => {
    let feed = seeded([]);
    for (let i = 0; i < SEEN_LIMIT + 50; i++) feed = feedRuns(feed, [run(`r${i}`)], i);
    assert.equal(feed.seen?.length, SEEN_LIMIT);
    assert.equal(feed.seen?.[0], `r${SEEN_LIMIT + 49}`);
  });
});

describe("falhas da consulta", () => {
  it("BFF sem alcançar o serviço ou fetch sem resposta: offline, e volta quando a consulta responde", () => {
    assert.equal(moodOf(feedFailure(seeded(), 0, "x", 0), 0, ON), "offline");
    assert.equal(moodOf(feedFailure(seeded(), 502, BFF_UNAVAILABLE, 0), 0, ON), "offline");
    const back = feedRuns(feedFailure(seeded(), 0, "x", 0), [run("a")], 1000);
    assert.equal(moodOf(back, 1000, ON), "idle");
    assert.equal(back.failures, 0);
  });

  it("erro da API alarma só ao entrar no erro: 401 repetido não deixa em alerta", () => {
    const first = feedFailure(seeded(), 401, "chave inválida", 0);
    assert.equal(moodOf(first, 0, ON), "error");
    const repeated = feedFailure(first, 401, "chave inválida", ALERT_MS + 1000);
    assert.equal(moodOf(repeated, ALERT_MS + 1000, ON), "idle");
    assert.equal(statusLine(repeated, "idle", ON), "erro na API: chave inválida");
    const other = feedFailure(repeated, 500, "falhou", ALERT_MS + 2000);
    assert.equal(moodOf(other, ALERT_MS + 2000, ON), "error");
    assert.equal(feedRuns(other, [run("a")], 0).apiError, null);
  });

  it("502 do próprio serviço (Langfuse fora) é erro da API, não queda", () => {
    const feed = feedFailure(seeded(), 502, "Langfuse não respondeu", 0);
    assert.equal(moodOf(feed, 0, ON), "error");
    assert.equal(feed.offline, false);
  });

  it("rota de execuções ausente (501) ou trace store sem configuração (503) fica quieta", () => {
    assert.equal(moodOf(feedFailure(seeded(), 501, "não suportado", 0), 0, ON), "paused");
    assert.equal(moodOf(feedFailure(seeded(), 503, "Langfuse não configurado", 0), 0, ON), "paused");
  });

  it("backoff dobra a cada falha seguida, até MAX_POLL_MS", () => {
    let feed = seeded();
    assert.equal(nextPollDelay(feed), POLL_MS);
    feed = feedFailure(feed, 0, "x", 0);
    assert.equal(nextPollDelay(feed), POLL_MS * 2);
    feed = feedFailure(feed, 0, "x", 0);
    assert.equal(nextPollDelay(feed), POLL_MS * 4);
    for (let i = 0; i < 20; i++) feed = feedFailure(feed, 0, "x", 0);
    assert.equal(nextPollDelay(feed), MAX_POLL_MS);
  });
});

describe("contexto", () => {
  it("observabilidade desligada ou aba oculta: dorme; offline vale mais", () => {
    const feed = feedRuns(seeded(), [run("b", { status: "error" }), run("a")], 0);
    assert.equal(moodOf(feed, 0, { observability: false, hidden: false }), "paused");
    assert.equal(moodOf(feed, 0, { observability: true, hidden: true }), "paused");
    assert.equal(moodOf(feedFailure(feed, 0, "x", 0), 0, { observability: false, hidden: true }), "offline");
  });

  it("o balão só abre logo depois da reação", () => {
    const feed = feedRuns(seeded(), [run("b"), run("a")], 1000);
    assert.equal(balloonEvent(feed, 1000)?.text, "nova execução de r8-v2");
    assert.equal(balloonEvent(feed, 1000 + BALLOON_MS), null);
    assert.equal(balloonEvent(seeded(), 0), null);
  });
});
