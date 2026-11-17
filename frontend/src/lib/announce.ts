/**
 * Anunciador único para leitores de tela: `announce("Tool salva")` escreve na região viva
 * montada uma vez no layout raiz (`<LiveAnnouncer />`, em `components/ui/announcer.tsx`).
 * Fora do React de propósito: um hook, um handler de `fetch` ou um `finally` chamam igual.
 *
 * Toasts já são anunciados pela região deles — não chame `announce` junto de um `toast`,
 * senão o leitor fala duas vezes.
 */

export interface AnnounceOptions {
  /** Interrompe o que o leitor está falando (erro). O padrão é educado: espera a vez. */
  assertive?: boolean;
}

export interface Announcement {
  text: string;
  assertive: boolean;
  /** Muda a cada anúncio: o mesmo texto duas vezes seguidas também é lido. */
  id: number;
}

type Listener = (announcement: Announcement) => void;

const listeners = new Set<Listener>();
let nextId = 0;

export function announce(text: string, { assertive = false }: AnnounceOptions = {}): void {
  const clean = text.replace(/\s+/g, " ").trim();
  if (!clean) return;
  const announcement = { text: clean, assertive, id: ++nextId };
  listeners.forEach((listener) => listener(announcement));
}

/** Usado pela região viva; devolve a função que cancela a inscrição. */
export function subscribeAnnouncements(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Teto do trecho da resposta lido em voz: o resto o usuário lê navegando pela conversa. */
export const PREVIEW_CHARS = 280;

/** Corta no fim de palavra e põe reticências. */
export function preview(text: string, max = PREVIEW_CHARS): string {
  const clean = text.replace(/[#*_`>|~]+/g, " ").replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  const cut = clean.slice(0, max);
  const space = cut.lastIndexOf(" ");
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).trimEnd()}…`;
}

/**
 * O que dizer quando uma resposta em streaming termina (uma vez, não a cada pedaço).
 * Erro vai em modo assertivo; resposta e interrupção, educado.
 */
export function chatCompletionAnnouncement(result: {
  agentName?: string;
  content: string;
  error?: string | null;
  stopped?: boolean;
}): { text: string; assertive: boolean } {
  const who = result.agentName?.trim() || "O agente";
  // O erro do provedor pode vir como um JSON enorme: lido só o começo (o resto está na tela).
  if (result.error) return { text: `${who} não respondeu: ${preview(result.error, 160)}`, assertive: true };
  if (result.stopped) {
    return { text: result.content.trim() ? "Resposta interrompida." : "Resposta interrompida, sem texto.", assertive: false };
  }
  const body = preview(result.content);
  return { text: body ? `${who} respondeu: ${body}` : `${who} terminou, sem texto na resposta.`, assertive: false };
}
