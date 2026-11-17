/** A conta da composição do prompt. `npm test` (test runner do Node). */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { cacheFraction, formatPercent, promptRows } from "./prompt-composition.ts";
import type { PromptComposition } from "./types";

const composition: PromptComposition = {
  chars: 1000,
  parts: { instructions: 600, history: 300, turn: 99, other: 1, memories: 0 },
  tokens_estimate: 250,
  tokens_estimate_method: "chars/4",
  messages: 4,
  attachments: 0,
};

describe("promptRows", () => {
  it("ordena pela leitura, descarta o que é zero e calcula o percentual", () => {
    const rows = promptRows(composition);
    assert.deepEqual(
      rows.map((r) => r.part),
      ["instructions", "history", "turn", "other"]
    );
    assert.equal(rows[0].percent, 60);
    assert.equal(rows[1].chars, 300);
  });

  it("sem total declarado, soma as partes", () => {
    const rows = promptRows({ ...composition, chars: 0, parts: { instructions: 1, turn: 1 } });
    assert.deepEqual(
      rows.map((r) => r.percent),
      [50, 50]
    );
  });
});

describe("formatPercent e cacheFraction", () => {
  it("mostra <1% para o minúsculo", () => {
    assert.equal(formatPercent(0.1), "<1%");
    assert.equal(formatPercent(59.6), "60%");
    assert.equal(formatPercent(0), "0%");
  });

  it("a fração do cache fica entre 0 e 1 e é nula sem dado", () => {
    assert.equal(cacheFraction(32509, 38578)?.toFixed(2), "0.84");
    assert.equal(cacheFraction(50, 10), 1);
    assert.equal(cacheFraction(0, 10), null);
    assert.equal(cacheFraction(undefined, undefined), null);
  });
});
