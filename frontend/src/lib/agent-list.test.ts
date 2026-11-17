/** O par produção/draft e a ordenação da lista de agentes. `npm test`. */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { draftChanges, draftDifferences, draftOf, mergeLastUsed, pairAgents, sortEntries } from "./agent-list.ts";
import type { AgentDefinition } from "./types";

function agent(agent_type: string, extra: Partial<AgentDefinition> = {}): AgentDefinition {
  return {
    agent_type,
    name: agent_type,
    kind: "conversational",
    response_schema: [],
    instructions: ["Responda."],
    tools: [],
    model_provider: "google",
    model_id: "gemini",
    model_credential_id: null,
    knowledge_collection: null,
    dependency_fields: [],
    memory_backend: "none",
    num_history_runs: 3,
    is_seed: false,
    prompt_version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...extra,
  };
}

describe("pairAgents", () => {
  it("junta o draft ao agente de produção num cartão só", () => {
    const entries = pairAgents([agent("demo-shadow"), agent("demo-shadow-draft"), agent("suporte")]);
    assert.deepEqual(
      entries.map((e) => [e.agent.agent_type, e.draft?.agent_type ?? null]),
      [
        ["demo-shadow", "demo-shadow-draft"],
        ["suporte", null],
      ]
    );
  });

  it("draft sem produção fica sozinho", () => {
    const entries = pairAgents([agent("r8-draft")]);
    assert.equal(entries.length, 1);
    assert.equal(entries[0].agent.agent_type, "r8-draft");
    assert.equal(entries[0].draft, null);
  });

  it("'-draft' sozinho não é draft de ninguém", () => {
    assert.equal(draftOf("-draft"), null);
    assert.equal(draftOf("r8-draft"), "r8");
    assert.equal(draftOf("r8"), null);
  });
});

describe("draftDifferences", () => {
  it("aponta só o que mudou", () => {
    const prod = agent("r8", { tools: ["a", "b"] });
    assert.deepEqual(draftDifferences(prod, agent("r8-draft", { tools: ["b", "a"] })), []);
    assert.deepEqual(
      draftDifferences(prod, agent("r8-draft", { tools: ["a", "b"], instructions: ["Outro."], model_id: "x" })),
      ["prompt", "modelo"]
    );
  });

  it("timeout e texto puro também contam, com o valor de cada lado", () => {
    const changes = draftChanges(agent("r8"), agent("r8-draft", { timeout_seconds: 30, plain_text: true }));
    assert.deepEqual(changes, [
      { group: "modelo", field: "timeout_seconds", prod: null, draft: 30 },
      { group: "formato", field: "plain_text", prod: false, draft: true },
    ]);
  });
});

describe("sortEntries", () => {
  const a = agent("alfa", { name: "Alfa", updated_at: "2026-03-01T00:00:00Z" });
  const b = agent("beta", { name: "Beta", updated_at: "2026-02-01T00:00:00Z" });
  const c = agent("gama", { name: "Gama", updated_at: "2026-04-01T00:00:00Z" });
  const cDraft = agent("gama-draft", { name: "Gama", updated_at: "2026-01-01T00:00:00Z" });
  const entries = pairAgents([a, b, c, cDraft]);
  const names = (list: typeof entries) => list.map((e) => e.agent.agent_type);

  it("uso recente: o draft conta para o cartão; quem nunca rodou vai pelo mais editado", () => {
    const lastUsed = mergeLastUsed(
      [["beta", "2026-11-01T00:00:00Z"], ["gama-draft", "2026-11-10T00:00:00Z"]],
      [["beta", "2026-10-01T00:00:00Z"], ["alfa", null]]
    );
    assert.equal(lastUsed.get("beta"), "2026-11-01T00:00:00Z");
    assert.deepEqual(names(sortEntries(entries, "recent", lastUsed)), ["gama", "beta", "alfa"]);
  });

  it("nome e edição", () => {
    assert.deepEqual(names(sortEntries(entries, "name", new Map())), ["alfa", "beta", "gama"]);
    assert.deepEqual(names(sortEntries(entries, "updated", new Map())), ["gama", "alfa", "beta"]);
  });
});
