import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { detectId, filterEntries, idLookup, matchScore, normalize } from "./command-search.ts";

const UUID = "3f2b8c1e-9a4d-4e7f-b2c6-0d1e2f3a4b5c";

describe("detectId", () => {
  it("UUID é ambíguo: execução e sessão, nessa ordem", () => {
    assert.deepEqual(detectId(UUID), { id: UUID, targets: ["run", "session"] });
  });

  it("aceita espaços em volta e maiúsculas no UUID (vira minúsculo, como o serviço grava)", () => {
    assert.deepEqual(detectId(`  ${UUID.toUpperCase()}\n`), { id: UUID, targets: ["run", "session"] });
  });

  it("sessões com prefixo do serviço são só sessão", () => {
    const analyze = `analyze-${"a1".repeat(16)}`;
    assert.deepEqual(detectId(analyze), { id: analyze, targets: ["session"] });
    assert.deepEqual(detectId("cli-0123456789ab"), { id: "cli-0123456789ab", targets: ["session"] });
    assert.deepEqual(detectId("mcp-abcdef012345"), { id: "mcp-abcdef012345", targets: ["session"] });
  });

  it("o resto não é id", () => {
    for (const text of [
      "",
      "suporte",
      "conversar com suporte",
      UUID.slice(0, -1),
      UUID.replaceAll("-", ""),
      `${UUID} x`,
      "cli-xyz",
      "analyze-",
      "outro-0123456789ab",
    ]) {
      assert.equal(detectId(text), null, text);
    }
  });
});

describe("matchScore", () => {
  const tool = { group: "Tools", label: "Consulta de CEP", hint: "cep", keywords: "api" };

  it("nome igual ou começando igual ganha de conter", () => {
    assert.ok(matchScore(tool, "cep") > matchScore(tool, "api"));
    assert.ok(matchScore(tool, "consulta") > matchScore(tool, "de c"));
    assert.ok(matchScore(tool, "de c") > 0);
  });

  it("não casa devolve 0; busca vazia casa tudo", () => {
    assert.equal(matchScore(tool, "fatura"), 0);
    assert.equal(matchScore(tool, ""), 1);
  });

  it("ignora acento (a busca chega normalizada)", () => {
    const col = { group: "Bases de conhecimento", label: "Manuais de manutenção" };
    assert.ok(matchScore(col, normalize("manutencao")) > 0);
    assert.ok(matchScore(col, normalize("MANUTENÇÃO")) > 0);
  });
});

describe("filterEntries", () => {
  const items = [
    { group: "Navegar", label: "Tools" },
    { group: "Agentes", label: "Conversar com Suporte", hint: "suporte" },
    { group: "Tools", label: "Abrir chamado", hint: "chamado-suporte" },
    { group: "Tools", label: "Suporte técnico", hint: "suporte" },
    { group: "Tools", label: "Calculadora", hint: "calculator" },
    { group: "Bases de conhecimento", label: "Manuais", hint: "manuais", keywords: "suporte" },
    { group: "Conversas", label: "c1" },
    { group: "Conversas", label: "c2" },
    { group: "Conversas", label: "c3" },
  ];
  const options = {
    searchOnlyGroups: ["Tools", "Bases de conhecimento"],
    limitPerGroup: { Tools: 2 },
    emptyQueryLimit: { Conversas: 2 },
  };

  it("sem busca, tools e collections ficam de fora e as conversas são cortadas", () => {
    assert.deepEqual(
      filterEntries(items, "", options).map((i) => i.label),
      ["Tools", "Conversar com Suporte", "c1", "c2"]
    );
  });

  it("com busca, ordena dentro do grupo e mantém os grupos contíguos na ordem de entrada", () => {
    const result = filterEntries(items, "suporte", options);
    assert.deepEqual(
      result.map((i) => `${i.group}:${i.label}`),
      [
        "Agentes:Conversar com Suporte",
        "Tools:Suporte técnico",
        "Tools:Abrir chamado",
        "Bases de conhecimento:Manuais",
      ]
    );
  });

  it("respeita o limite por grupo", () => {
    const result = filterEntries(items, "c", options);
    assert.deepEqual(
      result.filter((i) => i.group === "Tools").map((i) => i.label),
      ["Calculadora", "Abrir chamado"]
    );
  });

  it("empate mantém a ordem de entrada", () => {
    const result = filterEntries(items, "c", { limitPerGroup: {} }).filter((i) => i.group === "Conversas");
    assert.deepEqual(result.map((i) => i.label), ["c1", "c2", "c3"]);
  });
});

describe("idLookup", () => {
  it("só igualdade exata conta (a busca do serviço casa trechos do session_id)", () => {
    const runs = [
      { run_id: "r-1", session_id: `${UUID}-filha` },
      { run_id: UUID, session_id: "s-9" },
    ];
    assert.deepEqual(idLookup(UUID, runs), { run: true, session: false });
    assert.deepEqual(idLookup("s-9", runs), { run: false, session: true });
    assert.deepEqual(idLookup("x", []), { run: false, session: false });
  });
});
