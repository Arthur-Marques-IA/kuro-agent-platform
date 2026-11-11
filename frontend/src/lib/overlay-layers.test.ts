/**
 * Camadas sobrepostas: quem abre por cima fecha os menus por baixo, e nunca um diálogo.
 * Roda com `npm test` (`node --experimental-strip-types --test`).
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createLayerRegistry, layersToDismiss } from "./overlay-layers.ts";
import { createReservations } from "./mascot-space.ts";

describe("layersToDismiss", () => {
  const menu = { id: 1, kind: "menu" as const };
  const modal = { id: 2, kind: "modal" as const };

  it("um diálogo (a paleta do Ctrl+K) que abre fecha o menu aberto", () => {
    assert.deepEqual(layersToDismiss([menu], { id: 3, kind: "modal" }), [menu]);
  });

  it("um menu que abre dentro de um diálogo não fecha o diálogo", () => {
    assert.deepEqual(layersToDismiss([modal], { id: 3, kind: "menu" }), []);
  });

  it("um menu fecha o outro", () => {
    assert.deepEqual(layersToDismiss([menu], { id: 3, kind: "menu" }), [menu]);
  });

  it("diálogo sobre diálogo empilha (confirm dentro de um sheet)", () => {
    assert.deepEqual(layersToDismiss([modal], { id: 3, kind: "modal" }), []);
  });

  it("nunca dispensa a própria camada que está entrando", () => {
    assert.deepEqual(layersToDismiss([menu, modal], menu), []);
  });
});

describe("createLayerRegistry", () => {
  it("Preferências aberto + Ctrl+K: o menu fecha e a paleta fica sozinha", () => {
    const registry = createLayerRegistry();
    const closed: string[] = [];
    registry.open("menu", () => closed.push("preferências"));
    registry.open("modal", () => closed.push("paleta"));
    assert.deepEqual(closed, ["preferências"]);
    assert.deepEqual(
      registry.layers().map((layer) => layer.kind),
      ["modal"]
    );
  });

  it("sheet + menu + confirm: o menu sai, o sheet fica embaixo do confirm", () => {
    const registry = createLayerRegistry();
    const closed: string[] = [];
    registry.open("modal", () => closed.push("sheet"));
    registry.open("menu", () => closed.push("menu"));
    registry.open("modal", () => closed.push("confirm"));
    assert.deepEqual(closed, ["menu"]);
    assert.equal(registry.layers().length, 2);
  });

  it("liberar uma camada já dispensada não quebra nem remove outra", () => {
    const registry = createLayerRegistry();
    const releaseMenu = registry.open("menu", () => {});
    registry.open("modal", () => {});
    releaseMenu();
    releaseMenu();
    assert.deepEqual(
      registry.layers().map((layer) => layer.kind),
      ["modal"]
    );
  });

  it("fechar a de cima devolve o registro ao estado anterior", () => {
    const registry = createLayerRegistry();
    registry.open("modal", () => {});
    const releaseTop = registry.open("modal", () => {});
    releaseTop();
    assert.equal(registry.layers().length, 1);
  });
});

describe("createReservations (o espaço do Kuro)", () => {
  it("duas telas reservam e uma sai: continua reservado", () => {
    const space = createReservations();
    const a = space.reserve();
    const b = space.reserve();
    a();
    assert.equal(space.isReserved(), true);
    b();
    assert.equal(space.isReserved(), false);
  });

  it("liberar duas vezes conta uma só", () => {
    const space = createReservations();
    const a = space.reserve();
    const b = space.reserve();
    a();
    a();
    assert.equal(space.isReserved(), true);
    b();
    assert.equal(space.isReserved(), false);
  });

  it("avisa só quando muda de livre para reservado e de volta", () => {
    const space = createReservations();
    let calls = 0;
    const unsubscribe = space.subscribe(() => calls++);
    const a = space.reserve();
    const b = space.reserve();
    b();
    a();
    assert.equal(calls, 2);
    unsubscribe();
    space.reserve();
    assert.equal(calls, 2);
  });
});
