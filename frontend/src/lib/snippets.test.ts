/**
 * Os exemplos de integração da aba Integração. `npm test` (`node --experimental-strip-types --test`).
 *
 * O que já quebrou de verdade e não pode voltar: o stream que só lia `message` (um 503 que chega
 * como evento `error` sumia calado) e a falta do exemplo em PHP, que é a linguagem do integrador.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { buildSnippet, SNIPPET_LANGUAGES, type SnippetOptions } from "./snippets.ts";

const BASE: SnippetOptions = {
  baseUrl: "https://kuro.exemplo.com",
  endpoint: "/chat",
  payload: { agent_type: "suporte", user_id: "u1", session_id: "s1", message: "O preço é $10, 'ok'?" },
  requiresAuth: true,
};

describe("PHP", () => {
  it("está entre as linguagens", () => {
    assert.ok(SNIPPET_LANGUAGES.some((l) => l.value === "php"));
  });

  it("manda o JSON literal num nowdoc, com a chave runtime do ambiente", () => {
    const code = buildSnippet("php", BASE);
    assert.match(code, /<<<'JSON'\n\{/);
    assert.match(code, /\nJSON;\n/);
    assert.ok(code.includes(`"message": "O preço é $10, 'ok'?"`), "o corpo não pode ser escapado nem interpolado");
    assert.ok(code.includes(`getenv("KURO_RUNTIME_API_KEY")`));
    assert.ok(code.includes(`curl_init("https://kuro.exemplo.com/chat")`));
    assert.match(code, /\$status !== 200/);
  });

  it("sem auth, sem header Authorization", () => {
    assert.ok(!buildSnippet("php", { ...BASE, requiresAuth: false }).includes("Authorization"));
  });

  it("no stream, junta os pedaços, trata error e devolve o tamanho lido", () => {
    const code = buildSnippet("php", { ...BASE, endpoint: "/chat/stream" });
    assert.ok(code.includes("CURLOPT_WRITEFUNCTION"));
    assert.ok(code.includes(`str_replace("\\r\\n", "\\n", $buffer . $chunk)`));
    assert.ok(code.includes(`$event === "error"`));
    assert.ok(code.includes("return strlen($chunk);"));
  });

  it("no analyze, lê result", () => {
    const code = buildSnippet("php", { ...BASE, endpoint: "/analyze", payload: { agent_type: "x", document: "t" } });
    assert.ok(code.includes(`$data["result"]`));
  });
});

describe("stream", () => {
  for (const language of ["javascript", "python"] as const) {
    it(`${language} trata os eventos run, state, sources e error`, () => {
      const code = buildSnippet(language, { ...BASE, endpoint: "/chat/stream" });
      for (const event of ["run", "message", "state", "sources", "error"]) {
        assert.ok(code.includes(`event === "${event}"`) || code.includes(`event == "${event}"`), event);
      }
    });
  }

  it("cURL usa -N para não bufferizar", () => {
    assert.ok(buildSnippet("curl", { ...BASE, endpoint: "/chat/stream" }).startsWith("curl -N -X POST"));
  });
});
