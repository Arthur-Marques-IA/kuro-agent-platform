/**
 * O corpo do `/chat` (e do `/chat/stream`) que o Playground manda, com a entrada
 * estruturada: blocos de `context` e eventos `human_agent` em `events`. Separado do
 * desenho para ser testado sozinho (`chat-request.test.ts`, `npm test`).
 *
 * As regras dos blocos espelham as do servidor (`agents/structured_input.py`): o que o
 * servidor recusaria com 422 é apontado aqui, campo a campo, antes do envio.
 */
import type { Attachment, ChatMessage, TimelineTurn } from "./types";

export const MAX_CONTEXT_BLOCKS = 30;
export const MAX_KEY_CHARS = 64;
export const MAX_TITLE_CHARS = 200;

/** Um bloco como a pessoa edita no painel (pode estar incompleto). */
export interface ContextBlockDraft {
  key: string;
  title: string;
  content: string;
  volatile: boolean;
}

/** Um bloco como vai no corpo da requisição. */
export interface ContextBlockPayload {
  key: string;
  title: string;
  content: string;
  volatile?: boolean;
}

/** Um evento da entrada estruturada como vai no corpo. O Playground só gera `human_agent`. */
export interface ChatEventPayload {
  role: "contact" | "human_agent" | "agent_sent" | "system";
  content: string;
  at?: string;
}

export interface BlockErrors {
  key?: string;
  title?: string;
}

export interface BlocksValidation {
  /** Os blocos que vão no corpo: aparados, sem as linhas totalmente em branco. */
  blocks: ContextBlockPayload[];
  /** Um item por bloco do rascunho (mesmo índice); `null` quando o bloco está certo. */
  errors: (BlockErrors | null)[];
  /** O primeiro problema, para o aviso do envio; `null` quando dá para enviar. */
  error: string | null;
}

export function emptyBlock(): ContextBlockDraft {
  return { key: "", title: "", content: "", volatile: false };
}

function isBlank(block: ContextBlockDraft): boolean {
  return !block.key.trim() && !block.title.trim() && !block.content.trim();
}

/** Lê os blocos guardados no `localStorage`; texto inválido vira lista vazia. */
export function parseContextBlocks(text: string | null | undefined): ContextBlockDraft[] {
  if (!text) return [];
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return [];
  }
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null)
    .map((item) => ({
      key: typeof item.key === "string" ? item.key : "",
      title: typeof item.title === "string" ? item.title : "",
      content: typeof item.content === "string" ? item.content : "",
      volatile: item.volatile === true,
    }));
}

export function serializeContextBlocks(blocks: ContextBlockDraft[]): string {
  return JSON.stringify(blocks);
}

/**
 * Confere os blocos com as regras do servidor: `key` e `title` não vazios (depois de
 * aparar), `key` única e até 64 caracteres, `title` até 200, no máximo 30 blocos.
 * `content` vazio é aceito. Uma linha toda em branco é ignorada, não é erro.
 */
export function validateContextBlocks(drafts: ContextBlockDraft[]): BlocksValidation {
  const blocks: ContextBlockPayload[] = [];
  const errors: (BlockErrors | null)[] = [];
  const seen = new Map<string, number>();
  let first: string | null = null;

  drafts.forEach((draft, index) => {
    if (isBlank(draft)) {
      errors.push(null);
      return;
    }
    const key = draft.key.trim();
    const title = draft.title.trim();
    const problems: BlockErrors = {};
    if (!key) problems.key = "Informe a key.";
    else if (key.length > MAX_KEY_CHARS) problems.key = `Até ${MAX_KEY_CHARS} caracteres.`;
    else if (seen.has(key)) problems.key = `Key repetida (bloco ${(seen.get(key) ?? 0) + 1}).`;
    if (!title) problems.title = "Informe o título.";
    else if (title.length > MAX_TITLE_CHARS) problems.title = `Até ${MAX_TITLE_CHARS} caracteres.`;
    if (key && !seen.has(key)) seen.set(key, index);

    if (problems.key || problems.title) {
      errors.push(problems);
      first ??= `Bloco ${index + 1}: ${problems.key ?? problems.title}`;
      return;
    }
    errors.push(null);
    blocks.push({ key, title, content: draft.content, ...(draft.volatile ? { volatile: true } : {}) });
  });

  if (!first && blocks.length > MAX_CONTEXT_BLOCKS) {
    first = `No máximo ${MAX_CONTEXT_BLOCKS} blocos (há ${blocks.length}).`;
  }
  return { blocks, errors, error: first };
}

function sameBlock(a: ContextBlockPayload, b: ContextBlockPayload): boolean {
  return a.key === b.key && a.title === b.title && a.content === b.content && Boolean(a.volatile) === Boolean(b.volatile);
}

/** Mesma lista de blocos, na mesma ordem (a ordem é a do prompt). */
export function sameContext(a: ContextBlockPayload[], b: ContextBlockPayload[]): boolean {
  return a.length === b.length && a.every((block, i) => sameBlock(block, b[i]));
}

/**
 * O `context` da próxima mensagem. O servidor guarda o contexto da sessão: `context`
 * ausente herda o último enviado, uma lista substitui o contexto inteiro e `[]` limpa.
 * Então o Playground só manda quando os blocos mudaram desde o último envio desta
 * conversa (`lastSent`; `null` = nunca mandou):
 *
 * - nunca mandou: os blocos, se houver; sem blocos, nada (a sessão segue sem contexto);
 * - igual ao último envio: nada (`undefined`), o servidor já tem;
 * - diferente: a lista inteira — inclusive `[]`, quando a pessoa apagou todos os blocos.
 */
export function contextToSend(
  current: ContextBlockPayload[],
  lastSent: ContextBlockPayload[] | null
): ContextBlockPayload[] | undefined {
  if (lastSent === null) return current.length ? current : undefined;
  return sameContext(current, lastSent) ? undefined : current;
}

/** O último `context` enviado numa conversa, como guardado no `localStorage`; `null` = nunca. */
export function parseSentContext(text: string | null | undefined): ContextBlockPayload[] | null {
  if (!text) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (!Array.isArray(raw)) return null;
  return raw
    .filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null)
    .map((item) => ({
      key: String(item.key ?? ""),
      title: String(item.title ?? ""),
      content: String(item.content ?? ""),
      ...(item.volatile === true ? { volatile: true } : {}),
    }));
}

/** Um evento de atendente humano, com o horário em que foi registrado. */
export function humanAgentEvent(content: string, at: Date = new Date()): ChatEventPayload {
  return { role: "human_agent", content: content.trim(), at: at.toISOString() };
}

export interface ChatBodyOptions {
  agentType: string;
  userId: string;
  sessionId: string;
  message: string;
  dependencies?: Record<string, unknown> | null;
  attachments?: Attachment[];
  dryRun?: boolean;
  /** O que `contextToSend` decidiu: `undefined` fica de fora (herda), `[]` limpa. */
  context?: ContextBlockPayload[];
  events?: ChatEventPayload[];
}

/**
 * O corpo do `/chat/stream`. Campo vazio fica de fora (`undefined` some no
 * `JSON.stringify`): uma conversa sem blocos nem eventos manda exatamente o corpo de
 * antes, sem `events: []`. `context` vai como veio: `[]` é "limpe o contexto da sessão".
 */
export function buildChatBody(opts: ChatBodyOptions): Record<string, unknown> {
  return {
    agent_type: opts.agentType,
    user_id: opts.userId,
    session_id: opts.sessionId,
    message: opts.message,
    dependencies: opts.dependencies ?? undefined,
    attachments: opts.attachments?.length ? opts.attachments : undefined,
    dry_run: opts.dryRun || undefined,
    context: opts.context,
    events: opts.events?.length ? opts.events : undefined,
  };
}

/**
 * Conversa reaberta: o histórico vem dos runs do AgentOS, cujo `run_input` de um turno
 * estruturado é a mensagem montada pelo servidor (`# Conversa desde … # Mensagem nova do
 * contato`; nos runs da 0.3.0, com `# Contexto` na frente).
 * A linha do tempo traz os mesmos turnos já separados: aqui a bolha do contato volta a
 * ser só a fala, e cada evento `human_agent` vira a bolha do atendente antes dela.
 * Run legado (ou ausente da linha do tempo) fica como veio.
 */
export function applyTimeline(messages: ChatMessage[], turns: TimelineTurn[]): ChatMessage[] {
  const byRun = new Map(turns.filter((turn) => turn.structured).map((turn) => [turn.run_id, turn]));
  if (byRun.size === 0) return messages;
  return messages.flatMap((message) => {
    if (message.role !== "user" || !message.id.endsWith(":user")) return [message];
    const turn = byRun.get(message.id.slice(0, -":user".length));
    if (!turn) return [message];
    const speech = turn.events.find((event) => event.speech);
    const before = turn.events.filter((event) => !event.speech && event.role === "human_agent");
    const humans: ChatMessage[] = before.map((event, index) => ({
      id: `${turn.run_id}:human:${index}`,
      role: "human_agent",
      content: event.content ?? "",
      createdAt: event.at ? new Date(event.at).getTime() : message.createdAt,
    }));
    return [...humans, { ...message, content: turn.message ?? speech?.content ?? message.content }];
  });
}

/**
 * Nome da conversa quando o 1º turno foi estruturado. Sem nome, o AgentOS usa a primeira
 * entrada, que aí é o texto montado (`# Mensagem nova do contato …`): o Playground nomeia pela fala.
 */
export function sessionNameFromSpeech(text: string, max = 80): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max - 1).trimEnd()}…` : clean;
}

/** Os eventos `human_agent` mostrados logo antes de `index` (o que foi junto daquela fala). */
export function eventsBefore(messages: ChatMessage[], index: number): ChatEventPayload[] {
  const events: ChatEventPayload[] = [];
  for (let i = index - 1; i >= 0 && messages[i].role === "human_agent"; i--) {
    const m = messages[i];
    events.unshift({
      role: "human_agent",
      content: m.content,
      ...(m.createdAt ? { at: new Date(m.createdAt).toISOString() } : {}),
    });
  }
  return events;
}
