import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { toCsv } from "./csv.ts";
import {
  RUN_CSV_COLUMNS,
  SESSION_CSV_COLUMNS,
  collectPages,
  exportFileName,
  mergeFirstPage,
  parseAutoRefresh,
  testsProbeParams,
  type Page,
} from "./logs-lists.ts";
import type { LogSessionSummary, RunSummary } from "./types";

describe("parseAutoRefresh", () => {
  it("aceita só as opções do seletor", () => {
    assert.equal(parseAutoRefresh("10"), 10);
    assert.equal(parseAutoRefresh("30"), 30);
    assert.equal(parseAutoRefresh("60"), 60);
    assert.equal(parseAutoRefresh("0"), 0);
  });

  it("qualquer outra coisa vale desligada", () => {
    assert.equal(parseAutoRefresh(null), 0);
    assert.equal(parseAutoRefresh(undefined), 0);
    assert.equal(parseAutoRefresh(""), 0);
    assert.equal(parseAutoRefresh("5"), 0);
    assert.equal(parseAutoRefresh("abc"), 0);
  });
});

describe("mergeFirstPage", () => {
  type Item = { id: string; v: number };
  const key = (i: Item) => i.id;
  const current: Item[] = [
    { id: "c", v: 1 },
    { id: "b", v: 1 },
    { id: "a", v: 1 },
  ];

  it("põe as novas no topo e troca as que mudaram no lugar", () => {
    const fresh = [
      { id: "d", v: 1 },
      { id: "c", v: 2 },
    ];
    assert.deepEqual(mergeFirstPage(current, fresh, key, true), [
      { id: "d", v: 1 },
      { id: "c", v: 2 },
      { id: "b", v: 1 },
      { id: "a", v: 1 },
    ]);
  });

  it("numa lista ordenada, só atualiza no lugar", () => {
    const fresh = [
      { id: "d", v: 1 },
      { id: "a", v: 9 },
    ];
    assert.deepEqual(mergeFirstPage(current, fresh, key, false), [
      { id: "c", v: 1 },
      { id: "b", v: 1 },
      { id: "a", v: 9 },
    ]);
  });

  it("não encolhe o que veio de 'Carregar mais'", () => {
    assert.equal(mergeFirstPage(current, [{ id: "c", v: 1 }], key, true).length, 3);
  });
});

describe("testsProbeParams", () => {
  it("mantém o recorte, liga os testes e pede uma linha", () => {
    const probe = testsProbeParams({
      agent_type: ["r8", "suporte"],
      since: "2026-11-01T00:00:00.000Z",
      status: ["error"],
      include_dry_run: "false",
      sample: "20",
      _seed: "123",
      sort: "cost",
    });
    assert.deepEqual(probe, {
      agent_type: ["r8", "suporte"],
      since: "2026-11-01T00:00:00.000Z",
      status: ["error"],
      include_dry_run: "true",
      limit: "1",
    });
  });
});

describe("collectPages", () => {
  function pager(pages: Page<number>[]) {
    const seen: (string | null)[] = [];
    return {
      seen,
      fetch: async (cursor: string | null) => {
        seen.push(cursor);
        return pages[cursor ? Number(cursor) : 0];
      },
    };
  }

  it("segue o cursor até acabar", async () => {
    const p = pager([
      { items: [1, 2], next_cursor: "1" },
      { items: [3], next_cursor: null },
    ]);
    const out = await collectPages(p.fetch, { max: 10, pageSize: 2 });
    assert.deepEqual(out, { items: [1, 2, 3], truncated: false });
    assert.deepEqual(p.seen, [null, "1"]);
  });

  it("para no teto e avisa que sobrou", async () => {
    const p = pager([
      { items: [1, 2], next_cursor: "1" },
      { items: [3, 4], next_cursor: "2" },
      { items: [5, 6], next_cursor: null },
    ]);
    const progress: number[] = [];
    const out = await collectPages(p.fetch, { max: 3, pageSize: 2, onProgress: (n) => progress.push(n) });
    assert.deepEqual(out, { items: [1, 2, 3], truncated: true });
    assert.deepEqual(progress, [2, 3]);
  });

  it("chegar exatamente ao teto na última página não é truncar", async () => {
    const p = pager([
      { items: [1, 2], next_cursor: "1" },
      { items: [3, 4], next_cursor: null },
    ]);
    assert.deepEqual(await collectPages(p.fetch, { max: 4, pageSize: 2 }), { items: [1, 2, 3, 4], truncated: false });
  });

  it("páginas vazias com cursor não rodam para sempre", async () => {
    let calls = 0;
    const out = await collectPages(
      async () => {
        calls++;
        return { items: [] as number[], next_cursor: "x" };
      },
      { max: 4, pageSize: 2 }
    );
    assert.equal(calls, 4);
    assert.deepEqual(out, { items: [], truncated: true });
  });
});

describe("colunas do CSV", () => {
  it("execução: a fala pura, com a fórmula neutralizada", () => {
    const run = {
      run_id: "r1",
      trace_id: "t1",
      agent_type: "suporte",
      started_at: "2026-11-11T12:00:00Z",
      status: "success",
      input_message: "=cmd|' /C calc'!A0",
      message: "texto montado",
      input_tokens: 1,
      output_tokens: 2,
      total_tokens: 3,
      cost_usd: 0.5,
      feedback_up: 0,
      feedback_down: null,
    } as RunSummary;
    const [header, row] = toCsv(RUN_CSV_COLUMNS, [run]).slice(1).split("\r\n");
    const cols = header.split(",");
    assert.equal(cols[0], "started_at");
    assert.ok(row.includes("'=cmd|' /C calc'!A0"));
    assert.ok(!row.includes("texto montado"));
    assert.equal(cols.length, RUN_CSV_COLUMNS.length);
  });

  it("sessão: agentes juntos numa célula", () => {
    const session = {
      session_id: "s1",
      agent_types: ["r8", "suporte"],
      run_count: 2,
      started_at: "2026-11-10T00:00:00Z",
      last_activity: "2026-11-11T00:00:00Z",
      total_tokens: 10,
      error_count: 0,
      feedback_up: null,
      feedback_down: null,
      contact_name: "Ana, a cliente",
    } as LogSessionSummary;
    const row = toCsv(SESSION_CSV_COLUMNS, [session]).split("\r\n")[1];
    assert.ok(row.includes(",r8 suporte,"));
    assert.ok(row.includes('"Ana, a cliente"'));
  });
});

describe("exportFileName", () => {
  it("leva o tipo e o horário local", () => {
    assert.equal(exportFileName("execucoes", new Date(2026, 10, 1, 9, 5)), "kuro-execucoes-2026-11-01-0905.csv");
  });
});
