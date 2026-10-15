/**
 * Formulário de dependencies ↔ JSON. `npm test` (node --test, sem dependência nova).
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  coerceText,
  extraKeys,
  fieldsFromToolParams,
  formatLoose,
  parseLoose,
  parseObjectText,
  setKey,
  stringifyObject,
  validateObject,
  type FormField,
} from "./dependencies-form.ts";
import type { ApiToolParam } from "./types";

const FIELDS: FormField[] = [
  { name: "cpf", type: "string", required: true },
  { name: "idade", type: "integer" },
  { name: "plano", type: "string", enum: ["basico", "premium"] },
];

describe("ida e volta pelo texto", () => {
  it("editar um campo não perde os extras nem muda a ordem", () => {
    const text = JSON.stringify({ extra: { a: 1 }, cpf: "1", outro: [1, 2] }, null, 2);
    const obj = parseObjectText(text).value!;
    const novo = stringifyObject(setKey(obj, "cpf", "2"));
    assert.deepEqual(Object.keys(JSON.parse(novo)), ["extra", "cpf", "outro"]);
    assert.deepEqual(JSON.parse(novo).extra, { a: 1 });
    assert.deepEqual(extraKeys(FIELDS, obj), ["extra", "outro"]);
  });

  it("sem edição o texto do preset continua igual", () => {
    const preset = JSON.stringify({ cpf: "000", idade: 0 }, null, 2);
    const obj = parseObjectText(preset).value!;
    assert.equal(stringifyObject(setKey(obj, "cpf", "000")), preset);
  });

  it("objeto vazio vira texto vazio e texto vazio vira objeto vazio", () => {
    assert.equal(stringifyObject({}), "");
    assert.deepEqual(parseObjectText("  ").value, {});
  });

  it("JSON inválido ou que não é objeto é erro", () => {
    assert.match(parseObjectText("{").error!, /JSON inválido/);
    assert.match(parseObjectText("[1]").error!, /objeto/);
  });

  it("campo novo entra no fim; undefined remove", () => {
    assert.deepEqual(Object.keys(setKey({ a: 1 }, "b", 2)), ["a", "b"]);
    assert.deepEqual(setKey({ a: 1, b: 2 }, "a", undefined), { b: 2 });
  });
});

describe("coerceText", () => {
  it("vazio remove a chave", () => {
    assert.equal(coerceText("string", ""), undefined);
    assert.equal(coerceText("integer", ""), undefined);
  });

  it("número parcial vira número quando já vale um", () => {
    assert.equal(coerceText("number", "1."), 1);
    assert.equal(coerceText("number", "1.05"), 1.05);
    assert.equal(coerceText("number", "-"), "-");
    assert.equal(coerceText("integer", "12"), 12);
    assert.equal(coerceText("integer", "1.5"), "1.5");
  });

  it("texto continua texto, mesmo parecendo número", () => {
    assert.equal(coerceText("string", "42"), "42");
  });
});

describe("outros campos (valor solto)", () => {
  it("formatLoose e parseLoose são inversos", () => {
    for (const v of ["texto", "42", "true", 42, true, null, { a: [1] }, [1, "b"]]) {
      assert.deepEqual(parseLoose(formatLoose(v)), v);
    }
  });
});

describe("validateObject", () => {
  it("aponta obrigatório faltando, tipo errado e enum", () => {
    assert.deepEqual(validateObject(FIELDS, { idade: "1.5", plano: "ouro" }), {
      cpf: "obrigatório",
      idade: "deve ser um número inteiro",
      plano: "fora dos valores permitidos (basico, premium)",
    });
  });

  it("opcional ausente não é erro", () => {
    assert.deepEqual(validateObject(FIELDS, { cpf: "1" }), {});
  });

  it("entra em object e array de object", () => {
    const fields: FormField[] = [
      { name: "cliente", type: "object", fields: [{ name: "nome", type: "string", required: true }] },
      {
        name: "parcelas",
        type: "array",
        items: { type: "object", fields: [{ name: "valor", type: "number", required: true }] },
      },
      { name: "ids", type: "array", items: { type: "integer" } },
    ];
    assert.deepEqual(validateObject(fields, { cliente: {}, parcelas: [{ valor: 1 }, {}], ids: [1, "x"] }), {
      "cliente.nome": "obrigatório",
      "parcelas[1].valor": "obrigatório",
      "ids[1]": "deve ser um número inteiro",
    });
  });
});

describe("fieldsFromToolParams", () => {
  it("separa argumentos e dependencies pelo nome do campo em `dependency`", () => {
    const params: ApiToolParam[] = [
      { name: "q", type: "string", location: "query", required: true },
      { name: "document", type: "string", location: "path", source: "dependency", dependency: "cpf" },
      { name: "doc2", type: "string", location: "query", source: "dependency", dependency: "cpf" },
      { name: "teste", type: "boolean", location: "header", source: "dependency", dependency: "dry_run" },
      { name: "versao", type: "string", location: "header", source: "const", value: "2" },
    ];
    const { args, deps } = fieldsFromToolParams(params);
    assert.deepEqual(args.map((f) => f.name), ["q"]);
    assert.deepEqual(deps.map((f) => f.name), ["cpf"]);
  });
});
