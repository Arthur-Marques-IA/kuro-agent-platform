/** Os atalhos de lista (`j`/`k`, `Enter`, `Esc`) e quando eles ficam quietos. `npm test`. */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { isTypingElement, listActionFor, moveSelection } from "./list-nav.ts";

const free = { typing: false, overlayOpen: false };

function el(tagName: string, attrs: Record<string, string> = {}, isContentEditable = false) {
  return { tagName, isContentEditable, getAttribute: (name: string) => attrs[name] ?? null };
}

describe("listActionFor", () => {
  it("mapeia j, k, Enter e Esc", () => {
    assert.equal(listActionFor({ key: "j" }, free), "next");
    assert.equal(listActionFor({ key: "k" }, free), "prev");
    assert.equal(listActionFor({ key: "Enter" }, free), "open");
    assert.equal(listActionFor({ key: "Escape" }, free), "back");
  });

  it("deixa as setas e as outras letras para o navegador", () => {
    for (const key of ["ArrowDown", "ArrowUp", "Home", "End", "x", " "]) {
      assert.equal(listActionFor({ key }, free), null, key);
    }
  });

  it("não age digitando nem com sobreposto aberto", () => {
    assert.equal(listActionFor({ key: "j" }, { typing: true, overlayOpen: false }), null);
    assert.equal(listActionFor({ key: "Escape" }, { typing: false, overlayOpen: true }), null);
  });

  it("não age com modificador, IME compondo ou tecla já tratada", () => {
    assert.equal(listActionFor({ key: "j", ctrlKey: true }, free), null);
    assert.equal(listActionFor({ key: "k", metaKey: true }, free), null);
    assert.equal(listActionFor({ key: "Enter", altKey: true }, free), null);
    assert.equal(listActionFor({ key: "J", shiftKey: true }, free), null);
    assert.equal(listActionFor({ key: "j", isComposing: true }, free), null);
    assert.equal(listActionFor({ key: "Escape", defaultPrevented: true }, free), null);
  });
});

describe("moveSelection", () => {
  it("sem seleção, j vai ao primeiro e k ao último", () => {
    assert.equal(moveSelection(-1, 5, "next"), 0);
    assert.equal(moveSelection(-1, 5, "prev"), 4);
  });

  it("anda um por vez e para nas pontas", () => {
    assert.equal(moveSelection(1, 5, "next"), 2);
    assert.equal(moveSelection(4, 5, "next"), 4);
    assert.equal(moveSelection(1, 5, "prev"), 0);
    assert.equal(moveSelection(0, 5, "prev"), 0);
  });

  it("lista vazia não tem seleção; lista que encolheu prende no fim", () => {
    assert.equal(moveSelection(2, 0, "next"), -1);
    assert.equal(moveSelection(9, 3, "open"), 2);
  });
});

describe("isTypingElement", () => {
  it("campos de texto contam como digitação", () => {
    assert.equal(isTypingElement(el("INPUT")), true);
    assert.equal(isTypingElement(el("input", { type: "search" })), true);
    assert.equal(isTypingElement(el("TEXTAREA")), true);
    assert.equal(isTypingElement(el("SELECT")), true);
    assert.equal(isTypingElement(el("DIV", {}, true)), true);
    assert.equal(isTypingElement(el("DIV", { role: "combobox" })), true);
  });

  it("links, botões, checkbox e o corpo da página não", () => {
    assert.equal(isTypingElement(el("A")), false);
    assert.equal(isTypingElement(el("BUTTON")), false);
    assert.equal(isTypingElement(el("input", { type: "checkbox" })), false);
    assert.equal(isTypingElement(el("BODY")), false);
    assert.equal(isTypingElement(null), false);
  });
});
