import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  EMPTY_LOGS_QUERY,
  apiSearchParams,
  isoDay,
  legacyLogsRedirect,
  logsHref,
  logsQueryString,
  logsRange,
  parseLogsQuery,
  searchParamsRecord,
  sharedLogsQuery,
} from "./logs-query.ts";

const roundTrip = (query: Parameters<typeof logsQueryString>[0]) =>
  parseLogsQuery(searchParamsRecord(new URLSearchParams(logsQueryString(query))));

describe("parseLogsQuery", () => {
  it("sem parâmetros é o recorte padrão (7 dias, sem testes)", () => {
    assert.deepEqual(parseLogsQuery({}), EMPTY_LOGS_QUERY);
  });

  it("ignora valores fora do domínio em vez de mandá-los à API", () => {
    const query = parseLogsQuery({ period: "2y", status: "talvez", version: "0", complexity: "0,2,9,2", sort: "preco" });
    assert.equal(query.period, "7d");
    assert.deepEqual(query.statuses, []);
    assert.equal(query.sort, "");
    assert.equal(query.version, null);
    assert.deepEqual(query.complexity, [2]);
  });

  it("agente e status repetidos viram lista; o formato antigo (um valor só) continua valendo", () => {
    assert.deepEqual(parseLogsQuery({ agent: ["r8", "suporte", "r8"] }).agents, ["r8", "suporte"]);
    assert.deepEqual(parseLogsQuery({ agent: "r8" }).agents, ["r8"]);
    assert.deepEqual(parseLogsQuery({ status: "error" }).statuses, ["error"]);
    assert.deepEqual(parseLogsQuery({ status: ["interrupted", "error,talvez"] }).statuses, ["error", "interrupted"]);
  });

  it("intervalo livre: descarta data que não existe e desinverte as pontas", () => {
    assert.deepEqual(
      [parseLogsQuery({ from: "2026-02-30", to: "2026-13-01" }).from, parseLogsQuery({ from: "ontem" }).from],
      ["", ""]
    );
    const invertido = parseLogsQuery({ from: "2026-10-10", to: "2026-10-01" });
    assert.deepEqual([invertido.from, invertido.to], ["2026-10-01", "2026-10-10"]);
  });

  it("o ano pela metade, que o campo de data manda a cada tecla, não vira recorte", () => {
    assert.deepEqual(["0002-10-01", "0202-10-01", "2026-10-01"].map(isoDay), ["", "", "2026-10-01"]);
  });
});

describe("logsQueryString", () => {
  it("só escreve o que difere do padrão", () => {
    assert.equal(logsQueryString(EMPTY_LOGS_QUERY), "");
    assert.equal(logsQueryString({ period: "30d", agents: ["r8"], toolFailed: true }), "period=30d&agent=r8&tool_failed=1");
    assert.equal(logsQueryString({ agents: ["r8", "suporte"] }), "agent=r8&agent=suporte");
  });

  it("com intervalo livre, o atalho do período não vai para a URL", () => {
    assert.equal(logsQueryString({ period: "30d", from: "2026-10-01", to: "2026-10-07" }), "from=2026-10-01&to=2026-10-07");
  });

  it("ida e volta pela URL preserva o recorte", () => {
    const query = {
      ...EMPTY_LOGS_QUERY,
      period: "90d" as const,
      agents: ["r8", "suporte"],
      tests: true,
      statuses: ["error" as const, "interrupted" as const],
      search: "98231",
      version: 7,
      complexity: [1, 3],
      sideEffect: true,
      hideShort: true,
      feedbackDown: true,
      sort: "cost" as const,
    };
    assert.deepEqual(roundTrip(query), query);
    const range = { ...EMPTY_LOGS_QUERY, from: "2026-09-01", to: "2026-09-30", agents: ["r8"] };
    assert.deepEqual(roundTrip(range), range);
  });
});

describe("legacyLogsRedirect", () => {
  it("o link antigo do usuário vai para as sessões, e o da sessão para as execuções", () => {
    assert.equal(legacyLogsRedirect({ user_id: "u 1" }), "/logs/sessions?user_id=u+1");
    assert.equal(legacyLogsRedirect({ session_id: "s1" }), "/logs/runs?session_id=s1");
  });

  it("o Panorama com filtros comuns fica onde está", () => {
    assert.equal(legacyLogsRedirect({ period: "30d" }), null);
    assert.equal(logsHref("/logs"), "/logs");
  });
});

describe("logsRange", () => {
  const now = Date.UTC(2026, 9, 14, 12);

  it("o atalho conta para trás a partir de agora, sem limite final", () => {
    assert.deepEqual(logsRange({ period: "7d", from: "", to: "" }, now), {
      since: new Date(now - 7 * 86_400_000).toISOString(),
      until: null,
    });
    assert.deepEqual(logsRange({ period: "all", from: "", to: "" }, now), { since: null, until: null });
  });

  it("o intervalo livre inclui o dia final inteiro (até a meia-noite seguinte, no fuso local)", () => {
    const range = logsRange({ period: "7d", from: "2026-10-01", to: "2026-10-07" }, now);
    assert.equal(range.since, new Date(2026, 9, 1).toISOString());
    assert.equal(range.until, new Date(2026, 9, 8).toISOString());
    assert.deepEqual(logsRange({ period: "7d", from: "2026-10-07", to: "2026-10-01" }, now), {
      since: new Date(2026, 9, 1).toISOString(),
      until: new Date(2026, 9, 8).toISOString(),
    });
    assert.deepEqual(logsRange({ period: "7d", from: "", to: "2026-10-07" }, now), {
      since: null,
      until: new Date(2026, 9, 8).toISOString(),
    });
  });
});

describe("sharedLogsQuery e apiSearchParams", () => {
  it("o recorte que atravessa as abas leva os agentes e o intervalo, não a fila de revisão", () => {
    const query = { ...EMPTY_LOGS_QUERY, agents: ["a", "b"], from: "2026-10-01", toolFailed: true, sort: "cost" as const };
    assert.equal(logsHref("/logs", sharedLogsQuery(query)), "/logs?from=2026-10-01&agent=a&agent=b");
  });

  it("lista vira parâmetro repetido, como a API espera", () => {
    assert.equal(apiSearchParams({ agent_type: ["a", "b"], since: "x" }).toString(), "agent_type=a&agent_type=b&since=x");
  });
});
