/**
 * A conversa da tela da sessão: `agent_sent` repetido, a linha legível de cada tool, a
 * diferença de um bloco de contexto e quem aparece no cabeçalho. `npm test`.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  agentSentStatus,
  argsSummary,
  blockDiff,
  contextTurnFor,
  defaultContextTurn,
  previousOutputs,
  sessionTitle,
  speechOutsideEvents,
  stepBadges,
  stepLine,
  toolLabel,
  visibleEvents,
} from "./timeline.ts";
import type { TimelineEvent, TimelineStep, TimelineTurn } from "./types";

function turn(overrides: Partial<TimelineTurn> = {}): TimelineTurn {
  return {
    run_id: `r-${Math.random().toString(36).slice(2, 8)}`,
    at: "2026-11-11T12:00:00Z",
    agent_type: "r6",
    status: "success",
    dry_run: false,
    structured: true,
    events: [],
    message: "oi",
    context: [],
    steps: [],
    sources: [],
    output: "resposta",
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    ...overrides,
  };
}

const sent = (content: string): TimelineEvent => ({ role: "agent_sent", content, kind: "text" });

describe("agent_sent", () => {
  it("igual à resposta anterior (ignorando espaços) some; diferente ganha selo; sem anterior aparece sem selo", () => {
    assert.equal(agentSentStatus("Olá,  Maria\n", "Olá, Maria"), "duplicate");
    assert.equal(agentSentStatus("Olá, Maria!", "Olá, Maria"), "different");
    assert.equal(agentSentStatus("Olá", null), "unknown");
    assert.equal(agentSentStatus("Olá", "   "), "unknown");
  });

  it("compara com o turno conversacional anterior, pulando o analista", () => {
    const turns = [
      turn({ output: "Recebi seus dados" }),
      turn({ agent_type: "triagem", output: '{"fase": "x"}' }),
      turn({ events: [sent("Recebi seus dados"), sent("Recebi seus dados, Maria"), { role: "contact", content: "ok", speech: true }] }),
    ];
    const previous = previousOutputs(turns, (t) => t.agent_type === "triagem");
    assert.deepEqual(previous, [null, "Recebi seus dados", "Recebi seus dados"]);
    const shown = visibleEvents(turns[2], previous[2]);
    assert.deepEqual(
      shown.map((s) => [s.event.role, s.sent ?? null]),
      [
        ["agent_sent", "different"],
        ["contact", null],
      ]
    );
    assert.equal(shown[0].index, 1);
  });

  it("a marca echo do servidor esconde o repetido mesmo sem a resposta anterior na página", () => {
    const eco = { ...sent("Recebi seus dados"), echo: true };
    const shown = visibleEvents(turn({ events: [eco, { role: "contact", content: "ok", speech: true }] }), null);
    assert.deepEqual(
      shown.map((s) => s.event.role),
      ["contact"]
    );
  });

  it("a fala vira bolha própria só quando nenhum evento é a fala", () => {
    assert.equal(speechOutsideEvents(turn({ message: "quero parcelar" })), "quero parcelar");
    assert.equal(speechOutsideEvents(turn({ events: [{ role: "contact", content: "x", speech: true }] })), null);
    assert.equal(speechOutsideEvents(turn({ structured: false, message: null, raw_input: "tudo" })), null);
  });
});

describe("tools", () => {
  const step = (overrides: Partial<TimelineStep>): TimelineStep => ({ tool: "x", ok: true, side_effect: false, ...overrides });

  it("nome legível: especial, verbo no fim do nome, label cadastrado, nome humanizado", () => {
    assert.equal(toolLabel("search_knowledge_base"), "buscou na base");
    assert.equal(toolLabel("regente_r6_decidir", { regente_r6_decidir: "Decisão do R6" }), "decidiu");
    assert.equal(toolLabel("ficha_aluno", { ficha_aluno: "Ficha do aluno" }), "Ficha do aluno");
    assert.equal(toolLabel("ficha_aluno"), "Ficha aluno");
  });

  it("resumo dos argumentos: valores simples em ordem, no máximo três, objetos ficam de fora", () => {
    assert.equal(argsSummary({ acao: "transferir", departamento: "Secretaria" }), "transferir → Secretaria");
    assert.equal(argsSummary({ filtros: { a: 1 }, n: 3, ativo: true, a: "x", b: "y" }), "3 → true → x");
    assert.equal(argsSummary({ texto: "a".repeat(100) }).length, 40);
    assert.equal(argsSummary(null), "");
    assert.equal(argsSummary([1, 2]), "");
    assert.equal(
      stepLine(step({ tool: "regente_r6_decidir", args: { acao: "transferir", departamento: "Secretaria" } })),
      "decidiu: transferir → Secretaria"
    );
    assert.equal(stepLine(step({ tool: "ficha", args: {} })), "Ficha");
  });

  it("selos: falha, 404 neutro, efeito colateral", () => {
    assert.deepEqual(stepBadges(step({ ok: false, failure: "unavailable" })), ["failed"]);
    assert.deepEqual(stepBadges(step({ ok: true, failure: "not_found" })), ["not_found"]);
    assert.deepEqual(stepBadges(step({ side_effect: true })), ["side_effect"]);
    assert.deepEqual(stepBadges(step({})), []);
  });
});

describe("contexto", () => {
  it("diferença por linha contra o turno anterior; bloco novo é todo adicionado", () => {
    assert.deepEqual(blockDiff("Nome: Maria\nSEM parcela vencida", "Nome: Maria\n1 parcela vencida"), [
      { type: "same", text: "Nome: Maria" },
      { type: "removed", text: "SEM parcela vencida" },
      { type: "added", text: "1 parcela vencida" },
    ]);
    assert.deepEqual(blockDiff(null, "pediu 2ª via"), [{ type: "added", text: "pediu 2ª via" }]);
  });

  it("o painel começa no último turno com contexto", () => {
    const a = turn({ context: [{ key: "k", content: "x", hash: "h", changed: false }] });
    const b = turn({ structured: false, context: [] });
    assert.equal(defaultContextTurn([a, b]), a.run_id);
    assert.equal(defaultContextTurn([b]), null);
  });

  it("o painel acompanha o turno em foco; turno sem blocos mostra o último anterior que tinha", () => {
    const block = { key: "k", content: "x", hash: "h", changed: false };
    const a = turn({ context: [block] });
    const b = turn({ context: [] });
    const c = turn({ context: [block] });
    assert.equal(contextTurnFor([a, b, c], b.run_id)?.i, 0);
    assert.equal(contextTurnFor([a, b, c], c.run_id)?.i, 2);
    assert.equal(contextTurnFor([a, b, c], null)?.i, 2);
    assert.equal(contextTurnFor([b], null), null);
  });
});

describe("cabeçalho", () => {
  it("nome do contato, senão user_id, senão a sessão", () => {
    assert.equal(sessionTitle({ contact_name: "Maria", user_id: "aluno-1", session_id: "s" }), "Maria");
    assert.equal(sessionTitle({ contact_name: null, user_id: "aluno-1", session_id: "s" }), "aluno-1");
    assert.equal(sessionTitle({ session_id: "s" }), "s");
  });
});
