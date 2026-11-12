/**
 * Contraste dos tokens de cor do console, lidos do próprio `globals.css`: mudar um token que
 * derruba texto de 11–12 px abaixo de 4,5:1 quebra este teste. Roda com `npm test`.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import { AA_TEXT, blend, contrastRatio, parseHex, relativeLuminance } from "./contrast.ts";

const css = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");

function tokens(selector: ":root" | ".dark"): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  assert.ok(start >= 0, `bloco ${selector} não encontrado`);
  const body = css.slice(start, css.indexOf("}", start));
  const out: Record<string, string> = {};
  for (const match of body.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{3,6})\s*;/g)) out[match[1]] = match[2];
  return out;
}

const light = tokens(":root");
// O escuro só redefine parte: o resto herda do claro.
const dark = { ...light, ...tokens(".dark") };

describe("contrastRatio", () => {
  it("dá 21 para preto e branco e 1 para a mesma cor", () => {
    assert.equal(contrastRatio("#000000", "#ffffff").toFixed(2), "21.00");
    assert.equal(contrastRatio("#777", "#777777"), 1);
  });

  it("é simétrica e confere com valores conhecidos", () => {
    assert.equal(contrastRatio("#767676", "#ffffff").toFixed(2), "4.54");
    assert.equal(contrastRatio("#ffffff", "#767676"), contrastRatio("#767676", "#ffffff"));
  });

  it("lê #rgb e recusa cor inválida", () => {
    assert.deepEqual(parseHex("#0af"), [0, 170, 255]);
    assert.throws(() => parseHex("azul"));
    assert.equal(relativeLuminance("#ffffff"), 1);
  });

  it("mistura com opacidade como o `bg-x/10` do Tailwind", () => {
    assert.deepEqual(blend("#ffffff", "#000000", 0.5), [127.5, 127.5, 127.5]);
  });
});

for (const [name, t] of [["claro", light], ["escuro", dark]] as const) {
  // Página, cartão, barra lateral (surface), fundo de badge/realce (muted, accent) e popover.
  const backgrounds = ["background", "card", "surface", "muted", "accent", "popover"] as const;

  describe(`tema ${name}`, () => {
    it("texto secundário (muted-foreground) passa 4,5:1 em todo fundo", () => {
      for (const bg of backgrounds) {
        const ratio = contrastRatio(t["muted-foreground"], t[bg]);
        assert.ok(ratio >= AA_TEXT, `muted-foreground sobre ${bg}: ${ratio.toFixed(2)}`);
      }
    });

    it("cores de status como texto passam 4,5:1 em página, cartão e barra lateral", () => {
      for (const color of ["primary", "destructive", "success", "warning"]) {
        for (const bg of ["background", "card", "surface"]) {
          const ratio = contrastRatio(t[color], t[bg]);
          assert.ok(ratio >= AA_TEXT, `${color} sobre ${bg}: ${ratio.toFixed(2)}`);
        }
      }
    });

    it("badges de 11 px (texto da cor sobre a própria cor a 10%) passam 4,5:1 no cartão e na barra lateral", () => {
      for (const color of ["destructive", "success", "warning"]) {
        for (const bg of ["background", "card", "surface"]) {
          const ratio = contrastRatio(t[color], blend(t[color], t[bg], 0.1));
          assert.ok(ratio >= AA_TEXT, `badge ${color} sobre ${bg}: ${ratio.toFixed(2)}`);
        }
      }
      const soft = contrastRatio(t.primary, t["primary-soft"]);
      assert.ok(soft >= AA_TEXT, `badge primary sobre primary-soft: ${soft.toFixed(2)}`);
    });

    it("texto dos botões cheios passa 4,5:1", () => {
      for (const [fg, bg] of [
        ["primary-foreground", "primary"],
        ["destructive-foreground", "destructive"],
      ]) {
        const ratio = contrastRatio(t[fg], t[bg]);
        assert.ok(ratio >= AA_TEXT, `${fg} sobre ${bg}: ${ratio.toFixed(2)}`);
      }
    });
  });
}
