/**
 * A organização da página Tools. Os casos vêm dos dados reais que motivaram o redesenho: ~14 tools
 * `regente_*` de uma integração e três de sistema (calculator, cat_fact, hackernews).
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  EMPTY_FILTERS,
  SYSTEM_GROUP,
  apiEndpoint,
  firstSentence,
  groupTools,
  isUnclassified,
  matchesFilters,
  toolUsageIndex,
  urlHost,
} from "./tools-grouping.ts";
import type { ToolSummary } from "./types";

function tool(name: string, patch: Partial<ToolSummary> = {}): ToolSummary {
  return {
    tool_name: name,
    kind: "api",
    label: name,
    description: null,
    config: { method: "post", url: "http://host.docker.internal:8000/api/v1/x.php", parameters: [] },
    enabled: true,
    is_seed: false,
    side_effect: false,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...patch,
  };
}

describe("groupTools", () => {
  it("agrupa pelo prefixo e deixa as de sistema por último", () => {
    const groups = groupTools([
      tool("calculator", { kind: "builtin", is_seed: true, config: { builtin_id: "calculator" } }),
      tool("regente_anotar"),
      tool("regente_resolver"),
      tool("cat_fact", { is_seed: true, config: { method: "GET", url: "https://catfact.ninja/fact" } }),
    ]);
    assert.deepEqual(
      groups.map((g) => [g.key, g.title, g.tools.length]),
      [
        ["prefix:regente", "Regente", 2],
        [SYSTEM_GROUP, "Sistema e exemplos", 2],
      ]
    );
    assert.deepEqual(groups[0].hosts, ["host.docker.internal:8000"]);
  });

  it("prefixo sozinho não vira grupo; host repetido vira", () => {
    const groups = groupTools([
      tool("cep", { config: { url: "https://viacep.com.br/ws/{cep}/json" } }),
      tool("cep_lote", { config: { url: "https://viacep.com.br/lote" } }),
      tool("clima", { kind: "python", config: {} }),
    ]);
    assert.deepEqual(
      groups.map((g) => [g.title, g.tools.map((t) => t.tool_name)]),
      [
        ["viacep.com.br", ["cep", "cep_lote"]],
        ["Outras", ["clima"]],
      ]
    );
  });

  it("sem nenhum grupo de integração, o resto se chama 'Suas tools'", () => {
    assert.equal(groupTools([tool("a", { kind: "python", config: {} })])[0].title, "Suas tools");
  });
});

describe("toolUsageIndex", () => {
  it("junta tools e etapas action, separando os drafts", () => {
    const usage = toolUsageIndex([
      { agent_type: "r8", tools: ["regente_anotar"], stages: [] },
      { agent_type: "r8-draft", tools: ["regente_anotar"] },
      { agent_type: "abertura", tools: [], stages: [{ id: "s", type: "action", goal: "", tool: "regente_anotar" }] },
    ]);
    assert.deepEqual(usage.get("regente_anotar"), { agents: ["abertura", "r8"], drafts: ["r8-draft"] });
  });
});

describe("firstSentence", () => {
  it("não corta dentro de colchetes, parênteses ou aspas", () => {
    const text =
      'Normalmente NÃO precisa chamar — vem pronta (linha "[SISTEMA — ALUNO ...]"). Só chame se o CPF mudar.';
    assert.deepEqual(firstSentence(text), {
      head: 'Normalmente NÃO precisa chamar — vem pronta (linha "[SISTEMA — ALUNO ...]").',
      rest: true,
    });
  });

  it("frase única ou vazia", () => {
    assert.deepEqual(firstSentence("Devolve um fato curto."), { head: "Devolve um fato curto.", rest: false });
    assert.deepEqual(firstSentence(null), { head: "", rest: false });
  });
});

describe("endpoint", () => {
  it("método em maiúsculas e host mesmo com {param} no host", () => {
    assert.deepEqual(apiEndpoint(tool("x")), {
      method: "POST",
      host: "host.docker.internal:8000",
      url: "http://host.docker.internal:8000/api/v1/x.php",
    });
    assert.equal(urlHost("https://{tenant}.exemplo.com/v1"), "{tenant}.exemplo.com");
    assert.equal(apiEndpoint(tool("c", { kind: "builtin" })), null);
  });
});

describe("filtros", () => {
  const usage = toolUsageIndex([{ agent_type: "r8-draft", tools: ["b"] }]);

  it("não classificada segue o critério dos Logs (só ligadas)", () => {
    assert.equal(isUnclassified(tool("a", { side_effect: null })), true);
    assert.equal(isUnclassified(tool("a", { side_effect: null, enabled: false })), false);
  });

  it("draft não conta como uso; a busca acha pelo agente e pela URL", () => {
    assert.equal(matchesFilters(tool("b"), { ...EMPTY_FILTERS, unused: true }, usage), true);
    assert.equal(matchesFilters(tool("calc", { is_seed: true }), { ...EMPTY_FILTERS, unused: true }, usage), false);
    assert.equal(matchesFilters(tool("b"), { ...EMPTY_FILTERS, query: "r8-draft" }, usage), true);
    assert.equal(matchesFilters(tool("b"), { ...EMPTY_FILTERS, query: "x.php" }, usage), true);
    assert.equal(matchesFilters(tool("b"), { ...EMPTY_FILTERS, kind: "python" }, usage), false);
    assert.equal(matchesFilters(tool("b", { side_effect: true }), { ...EMPTY_FILTERS, sideEffect: true }, usage), true);
  });
});
