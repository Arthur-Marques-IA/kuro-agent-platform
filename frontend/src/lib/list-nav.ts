/**
 * Atalhos de lista do console, como no `kuro dash`: `j`/`k` movem a seleção,
 * `Enter` abre a linha selecionada e `Esc` volta da tela de detalhe para a lista.
 *
 * Aqui só a decisão (pura, testada em `list-nav.test.ts`); o hook que escuta o teclado e
 * mexe no foco está em `use-list-nav.ts`.
 */

export type ListAction = "next" | "prev" | "open" | "back";

export interface KeyInfo {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  /** IME compondo um caractere (`event.isComposing`). */
  isComposing?: boolean;
  /** Outro handler já tratou a tecla (`event.defaultPrevented`). */
  defaultPrevented?: boolean;
}

export interface KeyContext {
  /** O foco está num campo de texto (input, textarea, select ou contenteditable). */
  typing: boolean;
  /** Há um sobreposto aberto: diálogo, gaveta, menu, paleta de comandos. */
  overlayOpen: boolean;
}

// As setas, Home e End ficam com o navegador (rolar a página): só letras que não rolam nada.
const KEYS: Record<string, ListAction> = {
  j: "next",
  k: "prev",
  Enter: "open",
  Escape: "back",
};

/**
 * A ação de uma tecla, ou `null` quando ela não é nossa: digitando, com sobreposto aberto,
 * com Ctrl/⌘/Alt (atalhos do navegador e do console) ou com Shift (`J` não é `j`).
 */
export function listActionFor(event: KeyInfo, context: KeyContext): ListAction | null {
  if (event.defaultPrevented || event.isComposing) return null;
  if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return null;
  if (context.typing || context.overlayOpen) return null;
  return KEYS[event.key] ?? null;
}

/**
 * O próximo índice selecionado. Sem seleção (`-1`), `j` vai para o primeiro e `k` para o
 * último. Nas pontas, para (não dá a volta): na TUI também é assim.
 */
export function moveSelection(current: number, count: number, action: ListAction): number {
  if (count <= 0) return -1;
  const last = count - 1;
  if (action === "next") return current < 0 ? 0 : Math.min(current + 1, last);
  if (action === "prev") return current < 0 ? last : Math.max(current - 1, 0);
  return Math.min(current, last);
}

/** Tags e papéis em que a tecla é texto, não atalho. */
export function isTypingElement(element: { tagName?: string; isContentEditable?: boolean; getAttribute?(name: string): string | null } | null): boolean {
  if (!element) return false;
  if (element.isContentEditable) return true;
  const tag = element.tagName?.toLowerCase();
  if (tag === "textarea" || tag === "select") return true;
  if (tag === "input") {
    const type = (element.getAttribute?.("type") ?? "text").toLowerCase();
    // Checkbox, rádio e botões não recebem texto: lá o atalho continua valendo.
    return !["checkbox", "radio", "button", "submit", "reset", "range", "color", "file"].includes(type);
  }
  const role = element.getAttribute?.("role");
  return role === "textbox" || role === "combobox" || role === "searchbox";
}

/**
 * Seletor dos sobrepostos abertos que tomam o teclado. Defensivo: além do nosso `Dialog`
 * (`role="dialog"`) e do `DropdownMenu` (`role="menu"`), cobre `<dialog open>` nativo,
 * `aria-modal` e listbox de autocompletar.
 */
export const OVERLAY_SELECTOR = '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"], [aria-modal="true"], dialog[open]';
