/**
 * Iniciais do avatar e a data curta do cartão de agente. `npm test`.
 * Os nomes vêm da lista real que motivou a mudança: "R6 — Agente…" e "R8 — …" viravam "R—".
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { formatRelativeShort, initials } from "./format.ts";

describe("initials", () => {
  it("ignora separadores sem letra nem número", () => {
    assert.equal(initials("R6 — Agente de cobrança"), "R6");
    assert.equal(initials("R8 — Simulação de Vendas (chat)"), "R8");
    assert.equal(initials("Demo · shadow"), "DS");
  });

  it("usa as duas primeiras palavras", () => {
    assert.equal(initials("Auditor de Atendimentos Humanos"), "AD");
    assert.equal(initials("suporte técnico"), "ST");
  });

  it("palavra única dá as duas primeiras letras", () => {
    assert.equal(initials("Suporte"), "SU");
  });

  it("desempata pelo slug quando o nome não dá iniciais", () => {
    assert.equal(initials("—", "r6-cobranca"), "R6");
    assert.equal(initials("🤖", "extrator-contrato"), "EC");
    assert.equal(initials("X", "extrator"), "EX");
    assert.equal(initials("—"), "?");
    assert.equal(initials(""), "?");
  });

  it("aceita o slug como nome", () => {
    assert.equal(initials("extrator-contrato"), "EC");
    assert.equal(initials("r8_draft"), "R8");
  });
});

describe("formatRelativeShort", () => {
  const now = new Date("2026-11-11T12:00:00Z").getTime();
  const ago = (seconds: number) => new Date(now - seconds * 1000).toISOString();

  it("encurta sem virar data por extenso", () => {
    assert.equal(formatRelativeShort(ago(10), now), "agora");
    assert.equal(formatRelativeShort(ago(5 * 60), now), "há 5 min");
    assert.equal(formatRelativeShort(ago(3 * 3600), now), "há 3 h");
    assert.equal(formatRelativeShort(ago(86400), now), "ontem");
    assert.equal(formatRelativeShort(ago(3 * 86400), now), "há 3 dias");
    assert.equal(formatRelativeShort(ago(15 * 86400), now), "há 2 sem.");
    assert.equal(formatRelativeShort(ago(35 * 86400), now), "há 1 mês");
    assert.equal(formatRelativeShort(ago(120 * 86400), now), "há 4 meses");
    assert.equal(formatRelativeShort(ago(800 * 86400), now), "há 2 anos");
  });

  it("data inválida não quebra", () => {
    assert.equal(formatRelativeShort("não é data", now), "—");
  });
});
