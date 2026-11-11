/** O `response_schema` como tabela na tela de Análise: `npm test`. */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { schemaRows } from "./schema-rows.ts";
import type { SchemaField } from "./types";

describe("schemaRows", () => {
  it("achata objetos e itens de array, um nível mais fundo, com o caminho", () => {
    const schema: SchemaField[] = [
      { name: "acao", type: "string", required: true, description: "o que fazer", enum: ["pagar", "negar"] },
      { name: "cliente", type: "object", label: "Cliente", fields: [{ name: "nome", type: "string", required: true }] },
      {
        name: "parcelas",
        type: "array",
        items: {
          type: "object",
          fields: [
            { name: "numero", type: "integer" },
            { name: "status", type: "string", enum: ["aberta", "paga"] },
          ],
        },
      },
      { name: "tags", type: "array", items: { type: "string", enum: ["a", "b"] } },
      { name: "nota", type: "number", enum: [] },
    ];
    const rows = schemaRows(schema);
    assert.deepEqual(
      rows.map((r) => [r.path, r.depth, r.type, r.required]),
      [
        ["acao", 0, "string", true],
        ["cliente", 0, "object", false],
        ["cliente.nome", 1, "string", true],
        ["parcelas", 0, "array de object", false],
        ["parcelas[].numero", 1, "integer", false],
        ["parcelas[].status", 1, "string", false],
        ["tags", 0, "array de string", false],
        ["nota", 0, "number", false],
      ]
    );
    assert.deepEqual(rows[0].enum, ["pagar", "negar"]);
    assert.equal(rows[0].description, "o que fazer");
    assert.equal(rows[1].label, "Cliente");
    assert.deepEqual(rows[5].enum, ["aberta", "paga"]);
    // O enum de um array de escalares é o de cada item.
    assert.deepEqual(rows[6].enum, ["a", "b"]);
    // Enum vazio é o mesmo que sem enum.
    assert.equal(rows[7].enum, undefined);
  });

  it("aceita schema vazio ou ausente", () => {
    assert.deepEqual(schemaRows([]), []);
    assert.deepEqual(schemaRows(null), []);
  });
});
