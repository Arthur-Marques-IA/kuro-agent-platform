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

describe("displayValue", () => {
  it("mostra nulo e booleano de forma legível", () => {
    assert.equal(displayValue(null), "—");
    assert.equal(displayValue(true), "sim");
    assert.equal(displayValue(3), "3");
  });
});
