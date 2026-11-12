/**
 * Camadas sobrepostas do console (diálogos, sheets, menus, popovers): quem abre por cima
 * dispensa o que é passageiro por baixo, para nunca sobrar um menu aberto atrás da busca
 * global. A regra:
 *
 * - `menu` (menu suspenso, popover de filtro) é passageiro: qualquer camada que abre depois
 *   o fecha — outro menu, um diálogo, a paleta do Ctrl+K;
 * - `modal` (diálogo, sheet, a paleta) nunca é fechado por outra camada: um confirm aberto
 *   de dentro de um sheet empilha, e o Ctrl+K por cima de um formulário não joga fora o
 *   que foi digitado.
 *
 * O `Esc` continua com cada camada (o foco está nela, e ela para a propagação): um `Esc`,
 * uma camada. Módulo puro, sem React, para rodar no `npm test`.
 */

export type LayerKind = "modal" | "menu";

export interface Layer {
  id: number;
  kind: LayerKind;
}

/** As camadas abertas que a que está entrando fecha (nunca ela mesma). */
export function layersToDismiss(open: readonly Layer[], incoming: Layer): Layer[] {
  return open.filter((layer) => layer.id !== incoming.id && layer.kind === "menu");
}

export interface LayerRegistry {
  /**
   * Registra uma camada que acabou de abrir e fecha as que ela dispensa (chamando o
   * `dismiss` delas). Devolve a função que a remove do registro ao fechar.
   */
  open(kind: LayerKind, dismiss: () => void): () => void;
  /** As camadas abertas, da mais antiga para a mais nova. */
  layers(): Layer[];
}

export function createLayerRegistry(): LayerRegistry {
  let nextId = 1;
  const open = new Map<number, { layer: Layer; dismiss: () => void }>();

  return {
    open(kind, dismiss) {
      const layer: Layer = { id: nextId++, kind };
      const victims = layersToDismiss(
        [...open.values()].map((entry) => entry.layer),
        layer
      );
      open.set(layer.id, { layer, dismiss });
      for (const victim of victims) {
        const entry = open.get(victim.id);
        open.delete(victim.id);
        entry?.dismiss();
      }
      return () => {
        open.delete(layer.id);
      };
    },
    layers() {
      return [...open.values()].map((entry) => entry.layer);
    },
  };
}

/** O registro único do console. */
export const overlayLayers = createLayerRegistry();
