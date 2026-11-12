/** Regra de compatibilidade console × serviço. Roda com `npm test`. */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { compareVersions, parseVersion, serviceCompat } from "./service-compat.ts";

describe("parseVersion", () => {
  it("lê os números e ignora sufixos", () => {
    assert.deepEqual(parseVersion("0.2.0"), [0, 2, 0]);
    assert.deepEqual(parseVersion("0.3.0.dev0"), [0, 3, 0]);
    assert.deepEqual(parseVersion("1.4.2+abc123"), [1, 4, 2]);
    assert.deepEqual(parseVersion("v0.2"), [0, 2]);
  });

  it("recusa o que não é versão", () => {
    assert.equal(parseVersion("desconhecida"), null);
    assert.equal(parseVersion(""), null);
    assert.equal(parseVersion(null), null);
    assert.equal(parseVersion(undefined), null);
  });
});

describe("compareVersions", () => {
  it("compara número a número, não como texto", () => {
    assert.ok(compareVersions([0, 10, 0], [0, 9, 0]) > 0);
    assert.ok(compareVersions([0, 2, 0], [0, 2, 1]) < 0);
    assert.equal(compareVersions([0, 2], [0, 2, 0]), 0);
  });
});

describe("serviceCompat", () => {
  const MIN = "0.2.0";

  it("aceita a versão mínima e as mais novas", () => {
    assert.deepEqual(serviceCompat(true, "0.2.0", MIN), { ok: true });
    assert.deepEqual(serviceCompat(true, "0.2.1", MIN), { ok: true });
    assert.deepEqual(serviceCompat(true, "0.10.0", MIN), { ok: true });
    assert.deepEqual(serviceCompat(true, "0.3.0.dev0", MIN), { ok: true });
  });

  it("avisa quando o serviço é mais antigo", () => {
    const out = serviceCompat(true, "0.1.9", MIN);
    assert.equal(out.ok, false);
    assert.equal(!out.ok && out.reason, "too_old");
    assert.match(!out.ok ? out.message : "", /0\.1\.9.*0\.2\.0/);
  });

  it("avisa sem versão ou com versão ilegível", () => {
    for (const version of [undefined, null, "", "desconhecida"]) {
      const out = serviceCompat(true, version, MIN);
      assert.equal(!out.ok && out.reason, "unknown", String(version));
    }
  });

  it("avisa com o serviço fora do ar", () => {
    const out = serviceCompat(false, "0.2.0", MIN);
    assert.equal(!out.ok && out.reason, "unreachable");
  });

  it("usa a constante do console por padrão", () => {
    assert.deepEqual(serviceCompat(true, "99.0.0"), { ok: true });
    assert.equal(serviceCompat(true, "0.0.1").ok, false);
  });
});
