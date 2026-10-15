/**
 * A conta do gráfico de tendência dos Logs. Roda com o test runner do próprio Node, sem
 * dependência nova: `npm test` (`node --experimental-strip-types --test`).
 *
 * Os casos vêm dos dados que motivaram o redesenho: um dia com 640 execuções achatando os outros,
 * e um dia com 4 execuções e 50% de tool falhando puxando a escala da taxa para cima.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { FEW_RUNS, STACK_KEYS, bucketRate, niceMax, periodFailureRate, rateScale } from "./trend.ts";
import type { OverviewBucket, OverviewTotals } from "./types";

function bucket(runs: number, errors = 0, tool = 0): OverviewBucket {
  return {
    start: "2026-10-07T00:00:00-03:00",
    runs,
    success: runs - errors - tool,
    tool_failure_runs: tool,
    errors,
    cost_usd: 0,
    total_tokens: 0,
    p95_latency_ms: runs ? 1000 : null,
  };
}

function totals(runs: number, errors: number, interrupted: number): OverviewTotals {
  return {
    runs,
    success: runs - errors - interrupted,
    errors,
    interrupted,
    tool_failure_runs: 0,
    tool_calls: 0,
    tool_failures: 0,
    sessions: 0,
    total_tokens: 0,
    cost_usd: 0,
    p50_latency_ms: null,
    p95_latency_ms: null,
    feedback_up: 0,
    feedback_down: 0,
  };
}

describe("bucketRate", () => {
  it("é a fração das execuções do período", () => {
    assert.equal(bucketRate(bucket(640, 42, 1), "failed"), 42 / 640);
    assert.equal(bucketRate(bucket(640, 42, 1), "tool"), 1 / 640);
  });

  it("período sem execução não tem taxa (a linha quebra, não cai a zero)", () => {
    assert.equal(bucketRate(bucket(0), "failed"), null);
    assert.equal(bucketRate(bucket(0), "tool"), null);
  });
});

describe("periodFailureRate", () => {
  it("soma erro e interrompida", () => {
    assert.equal(periodFailureRate(totals(100, 4, 3)), 0.07);
  });

  it("sem execução, sem média", () => {
    assert.equal(periodFailureRate(totals(0, 0, 0)), null);
  });
});

describe("rateScale", () => {
  it("amostra pequena não define a escala e fica marcada como acima dela", () => {
    // 25/09: 45 execuções, 27% falharam. 28/09: 4 execuções, 50% com tool falhando.
    const buckets = [bucket(45, 12), bucket(4, 1, 2), bucket(640, 42, 1)];
    const { max, clipped } = rateScale(buckets, 0.072);
    assert.equal(max, 0.3); // o teto vem do 25/09, não dos 50% de 4 execuções
    assert.equal(clipped, true);
  });

  it("sem nenhum período confiável, usa todos", () => {
    const { max, clipped } = rateScale([bucket(2, 1)], 0.5);
    assert.equal(max, 0.5);
    assert.equal(clipped, false);
  });

  it("taxa baixa não vira um gráfico de ruído: o eixo vai até pelo menos 5%", () => {
    assert.equal(rateScale([bucket(1000, 1)], 0.001).max, 0.05);
  });

  it("sem falha nenhuma, 0–5% (e não 0–100%)", () => {
    assert.deepEqual(rateScale([bucket(50), bucket(0)], 0), { max: 0.05, clipped: false });
  });

  it("nunca passa de 100%", () => {
    assert.equal(rateScale([bucket(10, 10)], 1).max, 1);
  });

  it("o limite de amostra pequena é FEW_RUNS", () => {
    assert.equal(FEW_RUNS, 5);
    // Com exatamente FEW_RUNS execuções o período já conta para a escala.
    assert.equal(rateScale([bucket(FEW_RUNS, 2), bucket(100, 5)], 0.06).max, 0.4);
  });
});

describe("niceMax", () => {
  it("arredonda para um teto legível perto do dado", () => {
    assert.equal(niceMax(640), 800);
    assert.equal(niceMax(0.267), 0.3);
    assert.equal(niceMax(0.072), 0.08);
  });
});

describe("STACK_KEYS", () => {
  it("as falhas vêm primeiro, junto ao eixo", () => {
    assert.deepEqual(STACK_KEYS, ["errors", "tool_failure_runs", "success"]);
  });
});
