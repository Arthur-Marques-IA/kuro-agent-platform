"use client";

import { type RefObject, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { OVERLAY_SELECTOR, isTypingElement, listActionFor, moveSelection } from "@/lib/list-nav";

/** Atributo do contêiner da lista e de cada item (o link que abre a linha). */
export const LIST_NAV_ATTR = "data-list-nav";
export const LIST_NAV_ITEM_ATTR = "data-list-nav-item";

function visible(element: Element): boolean {
  return element.getClientRects().length > 0;
}

function overlayOpen(): boolean {
  return Array.from(document.querySelectorAll(OVERLAY_SELECTOR)).some(visible);
}

function keyContext() {
  return { typing: isTypingElement(document.activeElement as HTMLElement | null), overlayOpen: overlayOpen() };
}

/**
 * A lista que responde ao teclado: a que tem o foco; senão, a primeira visível da página.
 * Com duas listas na tela (raro), só uma anda.
 */
function activeList(): HTMLElement | null {
  const lists = Array.from(document.querySelectorAll<HTMLElement>(`[${LIST_NAV_ATTR}]`)).filter(visible);
  return lists.find((list) => list.contains(document.activeElement)) ?? lists[0] ?? null;
}

/**
 * `j`/`k` movem a seleção pelos itens (`data-list-nav-item`) visíveis dentro de `ref` — no
 * celular os cartões, no desktop as linhas da tabela — e `Enter` abre o selecionado. A
 * seleção é o foco: o item ganha o anel de foco e o leitor de tela lê o rótulo dele.
 */
export function useListKeyboardNav(ref: RefObject<HTMLElement | null>) {
  const selected = useRef(-1);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const action = listActionFor(event, keyContext());
      if (action !== "next" && action !== "prev" && action !== "open") return;
      const container = ref.current;
      if (!container || activeList() !== container) return;
      const items = Array.from(container.querySelectorAll<HTMLElement>(`[${LIST_NAV_ITEM_ATTR}]`)).filter(visible);
      if (items.length === 0) return;

      const focused = items.findIndex((item) => item === document.activeElement || item.contains(document.activeElement));
      if (focused >= 0) selected.current = focused;

      if (action === "open") {
        // Com o foco no link, o Enter do próprio navegador abre (e respeita Ctrl+Enter etc.).
        // Só age quando o foco saiu de tudo (body): abre a linha que estava selecionada.
        const idle = !document.activeElement || document.activeElement === document.body;
        if (!idle || selected.current < 0 || selected.current >= items.length) return;
        event.preventDefault();
        items[selected.current].click();
        return;
      }

      event.preventDefault();
      const next = moveSelection(focused >= 0 ? focused : selected.current, items.length, action);
      selected.current = next;
      const target = items[next];
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: "nearest" });
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [ref]);
}

interface NavigationLike {
  currentEntry?: { index: number } | null;
  entries?: () => { url: string | null }[];
}

/** A entrada anterior do histórico é deste console? (API Navigation, quando o navegador tem.) */
function canGoBackInApp(): boolean {
  const nav = (window as unknown as { navigation?: NavigationLike }).navigation;
  const index = nav?.currentEntry?.index;
  if (!nav?.entries || index == null || index < 1) return false;
  const previous = nav.entries()[index - 1]?.url;
  try {
    return !!previous && new URL(previous).origin === window.location.origin;
  } catch {
    return false;
  }
}

/**
 * `Esc` numa tela de detalhe volta para a lista, como no `kuro dash`: pelo histórico quando
 * se chegou por ele (a lista volta com os filtros e a rolagem), senão para `fallbackHref`.
 * Não age digitando, com diálogo/gaveta/menu aberto (o `Esc` é deles) nem com modificador.
 */
export function useEscapeBack(fallbackHref: string) {
  const router = useRouter();
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (listActionFor(event, keyContext()) !== "back") return;
      event.preventDefault();
      if (canGoBackInApp()) router.back();
      else router.push(fallbackHref);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [router, fallbackHref]);
}
