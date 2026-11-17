import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { CSV_BOM, csvCell, toCsv } from "./csv.ts";

describe("csvCell", () => {
  it("deixa texto simples como está", () => {
    assert.equal(csvCell("ok"), "ok");
    assert.equal(csvCell("ação"), "ação");
  });

  it("põe entre aspas e dobra as aspas internas", () => {
    assert.equal(csvCell('ele disse "oi"'), '"ele disse ""oi"""');
    assert.equal(csvCell("a,b"), '"a,b"');
    assert.equal(csvCell("a;b"), '"a;b"');
    assert.equal(csvCell("linha 1\nlinha 2"), '"linha 1\nlinha 2"');
    assert.equal(csvCell("a\r\nb"), '"a\r\nb"');
  });

  it("neutraliza texto que a planilha leria como fórmula", () => {
    assert.equal(csvCell("=1+1"), "'=1+1");
    assert.equal(csvCell("+5511999"), "'+5511999");
    assert.equal(csvCell("-2"), "'-2");
    assert.equal(csvCell("@SUM(A1)"), "'@SUM(A1)");
    assert.equal(csvCell("\tx"), "'\tx");
    // Prefixo e aspas juntos: a vírgula ainda obriga as aspas.
    assert.equal(csvCell('=HYPERLINK("http://x","clique")'), `"'=HYPERLINK(""http://x"",""clique"")"`);
  });

  it("mantém números crus, inclusive negativos, e esvazia nulos", () => {
    assert.equal(csvCell(-3), "-3");
    assert.equal(csvCell(0.0123), "0.0123");
    assert.equal(csvCell(Number.NaN), "");
    assert.equal(csvCell(null), "");
    assert.equal(csvCell(undefined), "");
    assert.equal(csvCell(true), "true");
  });
});

describe("toCsv", () => {
  it("começa com BOM e separa as linhas com CRLF", () => {
    const csv = toCsv(
      [
        { header: "id", value: (r: { id: string; n: number | null }) => r.id },
        { header: "n", value: (r) => r.n },
      ],
      [
        { id: "a", n: 1 },
        { id: "b,c", n: null },
      ]
    );
    assert.ok(csv.startsWith(CSV_BOM));
    assert.equal(csv.slice(1), 'id,n\r\na,1\r\n"b,c",\r\n');
  });

  it("sem linhas, só o cabeçalho", () => {
    assert.equal(toCsv([{ header: "id", value: (r: { id: string }) => r.id }], []), `${CSV_BOM}id\r\n`);
  });
});
