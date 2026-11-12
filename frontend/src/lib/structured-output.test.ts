/** A árvore da saída de um analista na tela do run: `npm test`. */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { displayValue, outputTree, parseStructured } from "./structured-output.ts";
import type { SchemaField } from "./types";

const SCHEMA: SchemaField[] = [
  { name: "acao", type: "string", label: "Ação" },
  { name: "cliente", type: "object", fields: [{ name: "nome", type: "string", label: "Nome" }] },
  {
    name: "parcelas",
    type: "array",
    items: { type: "object", fields: [{ name: "valor", type: "number", label: "Valor" }] },
  },
];

describe("parseStructured", () => {
  it("só aceita objeto ou array JSON", () => {
    assert.deepEqual(parseStructured(' {"a": 1} '), { a: 1 });
    assert.deepEqual(parseStructured("[1]"), [1]);
    assert.equal(parseStructured("texto"), null);
    assert.equal(parseStructured("{quebrado"), null);
    assert.equal(parseStructured(null), null);
  });
});

describe("outputTree", () => {
  it("segue a ordem e os rótulos do schema, com caminhos do eval", () => {
    const tree = outputTree({ extra: 1, parcelas: [{ valor: 10 }], cliente: { nome: "Ana" }, acao: "x" }, SCHEMA);
    assert.deepEqual(
      tree.map((n) => [n.key, n.label, n.kind]),
      [
        ["acao", "Ação", "value"],
        ["cliente", "cliente", "object"],
        ["parcelas", "parcelas", "array"],
        ["extra", "extra", "value"],
      ]
    );
    assert.equal(tree[1].children?.[0].path, "cliente.nome");
    assert.equal(tree[1].children?.[0].label, "Nome");
    const item = tree[2].children?.[0];
    assert.equal(item?.path, "parcelas[0]");
    assert.equal(item?.children?.[0].path, "parcelas[0].valor");
    assert.equal(item?.children?.[0].label, "Valor");
  });

  it("funciona sem schema (agente apagado ou mudado)", () => {
    const tree = outputTree({ b: null, a: [1, 2] });
    assert.deepEqual(tree.map((n) => n.key), ["b", "a"]);
    assert.equal(tree[1].children?.length, 2);
  });
});

describe("outputTree com enum", () => {
  const schema: SchemaField[] = [
    { name: "acao", type: "string", enum: ["pagar", "negar"] },
    { name: "nivel", type: "integer", enum: [1, 2, 3] },
    { name: "livre", type: "string" },
    { name: "tags", type: "array", items: { type: "string", enum: ["a", "b"] } },
    { name: "cliente", type: "object", fields: [{ name: "perfil", type: "string", enum: ["pf", "pj"] }] },
    {
      name: "parcelas",
      type: "array",
      items: { type: "object", fields: [{ name: "status", type: "string", enum: ["aberta", "paga"] }] },
    },
  ];

  it("marca as folhas com valores permitidos e se o valor está entre eles", () => {
    const tree = outputTree(
      {
        acao: "pagar",
        nivel: 2,
        livre: "x",
        tags: ["a", "z"],
        cliente: { perfil: "pj" },
        parcelas: [{ status: "paga" }],
      },
      schema
    );
    const [acao, nivel, livre, tags, cliente, parcelas] = tree;
    assert.deepEqual(acao.enum, ["pagar", "negar"]);
    assert.equal(acao.enumMatch, true);
    assert.equal(nivel.enumMatch, true);
    assert.equal(livre.enum, undefined);
    assert.equal(livre.enumMatch, undefined);
    // O enum do item de um array de escalares vale para cada elemento.
    assert.deepEqual(tags.children?.map((n) => [n.path, n.label, n.enumMatch]), [
      ["tags[0]", "[0]", true],
      ["tags[1]", "[1]", false],
    ]);
    assert.equal(cliente.children?.[0].enumMatch, true);
    assert.deepEqual(parcelas.children?.[0].children?.[0].enum, ["aberta", "paga"]);
    assert.equal(parcelas.children?.[0].children?.[0].enumMatch, true);
  });

  it("nulo não está entre os permitidos", () => {
    const [acao] = outputTree({ acao: null }, schema);
    assert.equal(acao.enumMatch, false);
  });
});

describe("displayValue", () => {
  it("mostra nulo e booleano de forma legível", () => {
    assert.equal(displayValue(null), "—");
    assert.equal(displayValue(true), "sim");
    assert.equal(displayValue(3), "3");
  });
});
