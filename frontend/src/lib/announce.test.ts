/** O anunciador para leitores de tela e o texto do fim de uma resposta em streaming. `npm test`. */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { type Announcement, announce, chatCompletionAnnouncement, preview, subscribeAnnouncements } from "./announce.ts";

describe("announce", () => {
  it("entrega a quem está inscrito, com id novo a cada vez, e para ao cancelar", () => {
    const heard: Announcement[] = [];
    const stop = subscribeAnnouncements((a) => heard.push(a));
    announce("Tool salva");
    announce("Tool salva");
    announce("  Falhou\n  ao salvar ", { assertive: true });
    announce("   ");
    stop();
    announce("depois de cancelar");
    assert.deepEqual(
      heard.map((a) => [a.text, a.assertive]),
      [
        ["Tool salva", false],
        ["Tool salva", false],
        ["Falhou ao salvar", true],
      ]
    );
    assert.notEqual(heard[0].id, heard[1].id);
  });
});

describe("chatCompletionAnnouncement", () => {
  it("lê a resposta uma vez, com o nome do agente e sem marcação de markdown", () => {
    const out = chatCompletionAnnouncement({ agentName: "Suporte", content: "**Pronto**: seu `wifi` voltou." });
    assert.deepEqual(out, { text: "Suporte respondeu: Pronto : seu wifi voltou.", assertive: false });
  });

  it("corta resposta longa no fim de uma palavra", () => {
    const long = "palavra ".repeat(100);
    const { text } = chatCompletionAnnouncement({ content: long });
    assert.ok(text.startsWith("O agente respondeu: palavra"));
    assert.ok(text.endsWith("palavra…"));
    assert.ok(text.length < 320);
  });

  it("erro interrompe; interrupção e resposta vazia são educados", () => {
    assert.deepEqual(chatCompletionAnnouncement({ agentName: "R6", content: "", error: "HTTP 500" }), {
      text: "R6 não respondeu: HTTP 500",
      assertive: true,
    });
    assert.equal(chatCompletionAnnouncement({ content: "meio", stopped: true }).text, "Resposta interrompida.");
    assert.equal(chatCompletionAnnouncement({ content: "  " }).text, "O agente terminou, sem texto na resposta.");
  });

  it("erro longo do provedor (JSON) é lido só no começo", () => {
    const error = JSON.stringify({ error: { message: "API key not valid. ".repeat(40) } });
    const { text, assertive } = chatCompletionAnnouncement({ content: "", error });
    assert.equal(assertive, true);
    assert.ok(text.length < 200, String(text.length));
    assert.ok(text.endsWith("…"));
  });

  it("preview não mexe em texto curto", () => {
    assert.equal(preview("oi"), "oi");
  });
});
