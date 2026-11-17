import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { checksFromRows, checksProblem, EMPTY_CHECK_ROW, parseValue, rowFromCheck, validField } from "./agent-checks.ts";

const row = (patch: object) => ({ ...EMPTY_CHECK_ROW, name: "curta", ...patch });

describe("campos e valores", () => {
  it("aceita os campos documentados e recusa o resto", () => {
    for (const f of ["output", "tools.gravar.calls", "tools.x.args.cpf", "result.acao", "dependencies.a.b", "metadata.canal"]) {
      assert.equal(validField(f), true, f);
    }
    for (const f of ["saida", "tools.x", "result.", "tools.x.args."]) assert.equal(validField(f), false, f);
  });

  it("o valor digitado vira JSON quando dá", () => {
    assert.equal(parseValue("5"), 5);
    assert.equal(parseValue("true"), true);
    assert.deepEqual(parseValue('["a"]'), ["a"]);
    assert.equal(parseValue("texto solto"), "texto solto");
  });
});

describe("linhas e API", () => {
  it("ida e volta preserva a checagem, com condição", () => {
    const check = {
      name: "uma vez",
      field: "tools.gravar.calls",
      op: "lte" as const,
      value: 1,
      when: { field: "status", op: "eq" as const, value: "success" },
      severity: "warn" as const,
    };
    assert.deepEqual(checksFromRows([rowFromCheck(check)]), [check]);
  });

  it("exists não leva valor e a linha vazia some", () => {
    const rows = [row({ field: "result.acao", op: "exists" }), { ...EMPTY_CHECK_ROW, name: "", field: "" }];
    assert.deepEqual(checksFromRows(rows), [{ name: "curta", field: "result.acao", op: "exists", severity: "fail" }]);
  });
});

describe("checksProblem", () => {
  it("aponta o primeiro problema", () => {
    assert.equal(checksProblem([row({ value: "5" })]), null);
    assert.match(checksProblem([row({ name: "" , field: "output"})]) ?? "", /dê um nome/);
    assert.match(checksProblem([row({ field: "saida", value: "1" })]) ?? "", /campo desconhecido/);
    assert.match(checksProblem([row({ value: "" })]) ?? "", /informe o valor/);
    assert.match(checksProblem([row({ value: "abc" })]) ?? "", /pede um número/);
    assert.match(checksProblem([row({ op: "in", value: "a" })]) ?? "", /lista JSON/);
    assert.match(checksProblem([row({ value: "1" }), row({ value: "2" })]) ?? "", /repetido/);
  });
});
