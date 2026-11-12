/** O estado do formulário de agente: abrir sem editar nunca fica "sujo". `npm test`. */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  buildCreate,
  buildUpdate,
  findMatches,
  formSections,
  sectionStatus,
  valuesFrom,
  type ModelContext,
} from "./agent-form.ts";
import type { AgentDefinition } from "./types";

const ctx: ModelContext = {
  defaultModel: { provider: "google", id: "gemini-flash-latest", label: "Gemini Flash" },
  models: [
    { provider: "google", id: "gemini-flash-latest", label: "Gemini Flash" },
    { provider: "google", id: "gemini-2.5-pro", label: "Gemini 2.5 Pro" },
    { provider: "anthropic", id: "claude-sonnet-4-5", label: "Claude Sonnet 4.5" },
  ],
};

/** Resposta completa do serviço atual, com instruções que o editor "uma por linha" não
 * representa como estão: uma com quebra de linha dentro e uma com espaço sobrando. */
function fullAgent(extra: Partial<AgentDefinition> = {}): AgentDefinition {
  return {
    agent_type: "r6",
    name: "Régua 6",
    kind: "conversational",
    response_schema: [],
    stages: [],
    instructions: ["Você é o agente da régua 6.", "Regras:\n- confirme o CPF\n- seja breve", "Responda em português.  "],
    tools: ["ficha", "calculator"],
    model_provider: "google",
    model_id: "gemini-2.5-pro",
    model_credential_id: null,
    knowledge_collection: "manuais",
    dependency_fields: [{ name: "cpf", type: "string", label: "CPF", description: "", required: true, default: null }],
    memory_backend: "none",
    num_history_runs: 5,
    model_params: { temperature: 0.2, reasoning: "low" },
    timeout_seconds: 60,
    session_summary: true,
    plain_text: false,
    is_seed: false,
    prompt_version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...extra,
  };
}

/** Serviço antigo: não devolve os campos opcionais que vieram depois. */
function legacyAgent(): AgentDefinition {
  const agent = fullAgent() as unknown as Record<string, unknown>;
  for (const key of ["stages", "model_params", "timeout_seconds", "session_summary", "plain_text", "response_schema"]) {
    delete agent[key];
  }
  // E alguns que eram nulos/vazios chegavam sem a chave.
  delete agent.model_id;
  delete agent.model_provider;
  delete agent.memory_backend;
  delete agent.num_history_runs;
  delete agent.dependency_fields;
  return agent as unknown as AgentDefinition;
}

function open(agent: AgentDefinition) {
  const initial = valuesFrom(agent, ctx.defaultModel.id);
  return { initial, values: { ...initial } };
}

describe("buildUpdate — abrir sem editar", () => {
  it("resposta completa: nada a salvar", () => {
    const agent = fullAgent();
    const { initial, values } = open(agent);
    assert.deepEqual(buildUpdate(values, initial, agent, ctx), {});
  });

  it("resposta sem os campos opcionais (serviço antigo): nada a salvar", () => {
    const agent = legacyAgent();
    const { initial, values } = open(agent);
    assert.deepEqual(buildUpdate(values, initial, agent, ctx), {});
  });

  it("agente analista e procedural: nada a salvar", () => {
    for (const kind of ["analysis", "procedural"] as const) {
      const agent = fullAgent({
        kind,
        response_schema: kind === "analysis" ? [{ name: "valor", type: "number", required: true }] : [],
        stages: kind === "procedural" ? [{ id: "coleta", type: "collect", goal: "Dados", fields: [] }] : [],
      });
      const { initial, values } = open(agent);
      assert.deepEqual(buildUpdate(values, initial, agent, ctx), {}, kind);
    }
  });

  it("modelo fora da lista conhecida: nada a salvar", () => {
    const agent = fullAgent({ model_provider: "openai", model_id: "gpt-legado" });
    const { initial, values } = open(agent);
    assert.deepEqual(buildUpdate(values, initial, agent, ctx), {});
  });
});

describe("buildUpdate — edição real", () => {
  it("mudar só o nome manda só o nome (sem versão nova do prompt)", () => {
    const agent = fullAgent();
    const { initial, values } = open(agent);
    assert.deepEqual(buildUpdate({ ...values, name: "Régua seis" }, initial, agent, ctx), { name: "Régua seis" });
  });

  it("serviço antigo: mudar o histórico manda só o histórico", () => {
    const agent = legacyAgent();
    const { initial, values } = open(agent);
    assert.deepEqual(buildUpdate({ ...values, numHistoryRuns: 3 }, initial, agent, ctx), { num_history_runs: 3 });
  });

  it("editar o prompt manda as instruções, uma por linha", () => {
    const agent = fullAgent();
    const { initial, values } = open(agent);
    const changes = buildUpdate({ ...values, instructions: `${values.instructions}\nNova regra.` }, initial, agent, ctx);
    assert.deepEqual(Object.keys(changes), ["instructions"]);
    assert.equal(changes.instructions?.at(-1), "Nova regra.");
  });

  it("linha em branco ou espaço a mais no prompt não conta como alteração", () => {
    const agent = fullAgent();
    const { initial, values } = open(agent);
    assert.deepEqual(buildUpdate({ ...values, instructions: `${values.instructions}\n\n   ` }, initial, agent, ctx), {});
  });

  it("desfazer a edição volta a não ter nada a salvar", () => {
    const agent = fullAgent();
    const { initial, values } = open(agent);
    const edited = { ...values, temperature: "0.9" };
    assert.deepEqual(buildUpdate(edited, initial, agent, ctx), { model_params: { temperature: 0.9, reasoning: "low" } });
    assert.deepEqual(buildUpdate({ ...edited, temperature: "0.2" }, initial, agent, ctx), {});
  });

  it("trocar de modelo manda provedor e id, e ajusta os parâmetros ao provedor", () => {
    const agent = fullAgent();
    const { initial, values } = open(agent);
    const changes = buildUpdate({ ...values, modelId: "claude-sonnet-4-5" }, initial, agent, ctx);
    assert.equal(changes.model_provider, "anthropic");
    assert.equal(changes.model_id, "claude-sonnet-4-5");
    // `reasoning` não vale no anthropic: sai dos parâmetros.
    assert.deepEqual(changes.model_params, { temperature: 0.2 });
  });

  it("versão restaurada do histórico: as instruções diferentes contam como alteração", () => {
    const agent = fullAgent();
    const initial = valuesFrom(agent, ctx.defaultModel.id);
    const values = valuesFrom(agent, ctx.defaultModel.id, ["Versão antiga."]);
    assert.deepEqual(buildUpdate(values, initial, agent, ctx), { instructions: ["Versão antiga."] });
  });

  it("analista nunca manda memória nem histórico", () => {
    const agent = fullAgent({ kind: "analysis", response_schema: [{ name: "x", type: "string", required: true }] });
    const { initial, values } = open(agent);
    assert.deepEqual(buildUpdate({ ...values, numHistoryRuns: 1, memoryBackend: "auto" }, initial, agent, ctx), {});
  });
});

describe("buildCreate", () => {
  it("analista omite memória e histórico; modelo desconhecido cai no padrão", () => {
    const values = { ...valuesFrom(undefined, ctx.defaultModel.id), name: " Extrator ", agentType: "extrator", kind: "analysis" as const, instructions: "Extraia.\n" };
    const body = buildCreate({ ...values, modelId: "sumiu" }, ctx);
    assert.equal(body.name, "Extrator");
    assert.deepEqual(body.instructions, ["Extraia."]);
    assert.equal(body.model_id, ctx.defaultModel.id);
    assert.equal("memory_backend" in body, false);
    assert.equal("num_history_runs" in body, false);
  });
});

describe("sectionStatus e formSections", () => {
  it("liga cada campo alterado e cada erro à seção dele", () => {
    const status = sectionStatus(
      { name: "x", model_params: {}, tools: [] },
      { name: null, agentType: null, instructions: "Escreva", numHistoryRuns: null, modelParams: null, responseSchema: null, stages: null }
    );
    assert.deepEqual([...status.changed].sort(), ["identidade", "modelo", "tools"]);
    assert.deepEqual([...status.invalid], ["prompt"]);
  });

  it("Etapas só em procedural, Saída só em analista", () => {
    assert.ok(formSections("procedural").some((s) => s.id === "etapas"));
    assert.ok(!formSections("conversational").some((s) => s.id === "etapas" || s.id === "saida"));
    assert.ok(formSections("analysis").some((s) => s.id === "saida"));
  });
});

describe("findMatches", () => {
  it("acha sem diferenciar maiúsculas e diz a linha", () => {
    const matches = findMatches("Confirme o CPF.\nNunca peça o cpf duas vezes.", "cpf");
    assert.deepEqual(
      matches.map((m) => [m.start, m.line]),
      [
        [11, 0],
        [29, 1],
      ]
    );
  });

  it("busca vazia não acha nada", () => {
    assert.deepEqual(findMatches("abc", ""), []);
  });
});
