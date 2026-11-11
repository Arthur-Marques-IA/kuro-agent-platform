/**
 * A lógica da conversa na tela da sessão (`components/observability/session-detail-view.tsx`),
 * separada do desenho para ser testada sozinha (`timeline.test.ts`, `npm test`).
 *
 * Tudo aqui lê os campos da linha do tempo (`GET /observability/sessions/{id}/timeline`):
 * nada recorta texto nem procura marcadores na entrada.
 */
import { type DiffLine, diffLines } from "./diff.ts";
import type { TimelineEvent, TimelineStep, TimelineTurn } from "./types";

export const ROLE_LABELS: Record<string, string> = {
  contact: "Contato",
  human_agent: "Atendente humano",
  agent_sent: "Enviado em nome do agente",
  system: "Sistema",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

/** Espaços e quebras colapsados — o que o canal reformata não conta como diferença. */
export function normalizeText(text: string | null | undefined): string {
  return (text ?? "").split(/\s+/).filter(Boolean).join(" ");
}

/**
 * Um `agent_sent` é o que a instituição de fato enviou em nome do agente.
 * - `duplicate`: igual à resposta anterior do agente — não se desenha de novo;
 * - `different`: saiu diferente do que o agente escreveu — aparece com selo;
 * - `unknown`: não há resposta anterior do agente na página para comparar — aparece, sem selo.
 */
export type AgentSentStatus = "duplicate" | "different" | "unknown";

export function agentSentStatus(content: string | null | undefined, previousOutput: string | null | undefined): AgentSentStatus {
  if (previousOutput == null || normalizeText(previousOutput) === "") return "unknown";
  return normalizeText(content) === normalizeText(previousOutput) ? "duplicate" : "different";
}

/**
 * A resposta anterior do agente para cada turno: o `output` do último turno conversacional
 * antes dele (`skip` tira, por exemplo, os runs de analista, que não falam com o contato).
 */
export function previousOutputs(turns: TimelineTurn[], skip: (turn: TimelineTurn) => boolean = () => false): (string | null)[] {
  let last: string | null = null;
  return turns.map((turn) => {
    const before = last;
    if (!skip(turn)) last = turn.output ?? null;
    return before;
  });
}

export interface ShownEvent {
  event: TimelineEvent;
  index: number;
  /** Só em `agent_sent`. */
  sent?: AgentSentStatus;
}

/** Os eventos do turno a desenhar, em ordem — sem os `agent_sent` repetidos. */
export function visibleEvents(turn: TimelineTurn, previousOutput: string | null): ShownEvent[] {
  const shown: ShownEvent[] = [];
  turn.events.forEach((event, index) => {
    if (event.role !== "agent_sent") {
      shown.push({ event, index });
      return;
    }
    const sent = agentSentStatus(event.content, previousOutput);
    if (sent !== "duplicate") shown.push({ event, index, sent });
  });
  return shown;
}

/** A fala nova vira uma bolha própria só quando nenhum evento já é ela (`speech: true`). */
export function speechOutsideEvents(turn: TimelineTurn): string | null {
  if (!turn.structured) return null;
  if (turn.events.some((e) => e.speech)) return null;
  return turn.message?.trim() ? turn.message : null;
}

// -- tools ------------------------------------------------------------------------

/** Último pedaço do nome da tool → o verbo no passado, para ler "decidiu: transferir". */
const VERBS: Record<string, string> = {
  decidir: "decidiu",
  consultar: "consultou",
  buscar: "buscou",
  enviar: "enviou",
  registrar: "registrou",
  transferir: "transferiu",
  criar: "criou",
  atualizar: "atualizou",
  cancelar: "cancelou",
  agendar: "agendou",
  gerar: "gerou",
  calcular: "calculou",
  verificar: "verificou",
  abrir: "abriu",
  emitir: "emitiu",
  encerrar: "encerrou",
};

const SPECIAL: Record<string, string> = {
  search_knowledge_base: "buscou na base",
  update_user_memory: "atualizou a memória",
};

function humanize(name: string): string {
  const words = name.split(/[_\-\s.]+/).filter(Boolean);
  if (words.length === 0) return name;
  const text = words.join(" ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * O nome legível de uma tool, nesta ordem: nomes especiais (busca na base, memória); o
 * último pedaço do nome quando é um verbo conhecido (`regente_r6_decidir` → "decidiu"); o
 * `label` cadastrado da tool; o nome com espaços no lugar de `_`/`-`.
 */
export function toolLabel(name: string, labels: Record<string, string> = {}): string {
  if (SPECIAL[name]) return SPECIAL[name];
  const last = name.split(/[_\-.]+/).filter(Boolean).pop()?.toLowerCase();
  if (last && VERBS[last]) return VERBS[last];
  if (labels[name]?.trim()) return labels[name].trim();
  return humanize(name);
}

const ARG_MAX = 40;
const SUMMARY_ARGS = 3;

export function shorten(text: string, max: number): string {
  const flat = normalizeText(text);
  return flat.length <= max ? flat : flat.slice(0, max - 1) + "…";
}

/**
 * Os argumentos principais em uma linha: os primeiros valores simples (texto, número,
 * booleano), na ordem em que vieram, separados por " → ". Objetos e listas ficam para o
 * detalhe expandido.
 */
export function argsSummary(args: unknown, max = SUMMARY_ARGS): string {
  if (args == null) return "";
  if (typeof args === "string") return shorten(args, ARG_MAX * 2);
  if (typeof args !== "object" || Array.isArray(args)) return "";
  const values: string[] = [];
  for (const value of Object.values(args as Record<string, unknown>)) {
    if (values.length >= max) break;
    if (typeof value === "string" && value.trim()) values.push(shorten(value, ARG_MAX));
    else if (typeof value === "number" || typeof value === "boolean") values.push(String(value));
  }
  return values.join(" → ");
}

/** A linha da tool na conversa: `decidiu: transferir → Secretaria`. */
export function stepLine(step: TimelineStep, labels: Record<string, string> = {}): string {
  const label = toolLabel(step.tool, labels);
  const summary = argsSummary(step.args);
  return summary ? `${label}: ${summary}` : label;
}

export type StepTone = "failed" | "not_found" | "side_effect" | "ok";

/** Selos da linha: falha (o critério de `tool_failures`), "não encontrado" (404, neutro) e efeito colateral. */
export function stepBadges(step: TimelineStep): StepTone[] {
  const tones: StepTone[] = [];
  if (!step.ok) tones.push("failed");
  else if (step.failure === "not_found") tones.push("not_found");
  if (step.side_effect) tones.push("side_effect");
  return tones;
}

// -- contexto ---------------------------------------------------------------------

/** Diferença linha a linha de um bloco contra o turno anterior. Bloco novo: tudo entra como adicionado. */
export function blockDiff(previous: string | null | undefined, current: string): DiffLine[] {
  const before = previous == null ? [] : previous.split("\n");
  return diffLines(before, current.split("\n"));
}

/** O turno cujo contexto o painel mostra quando nada está em foco: o último que tem blocos. */
export function defaultContextTurn(turns: TimelineTurn[]): string | null {
  for (let i = turns.length - 1; i >= 0; i--) {
    if (turns[i].context.length > 0) return turns[i].run_id;
  }
  return null;
}

/**
 * O turno do painel de contexto: o turno em foco, se tem blocos (ou é não estruturado, e
 * o painel diz isso); senão o último turno com blocos antes dele; sem foco, o último com
 * blocos da sessão. `i` é a posição, para o "turno N".
 */
export function contextTurnFor(turns: TimelineTurn[], focusId: string | null): { turn: TimelineTurn; i: number } | null {
  const index = turns.findIndex((t) => t.run_id === focusId);
  for (let i = index; i >= 0; i--) {
    if (turns[i].context.length > 0 || !turns[i].structured) return { turn: turns[i], i };
  }
  const fallback = defaultContextTurn(turns);
  const i = turns.findIndex((t) => t.run_id === fallback);
  return i >= 0 ? { turn: turns[i], i } : null;
}

/** Quem aparece no cabeçalho: o nome do contato, senão o `user_id`, senão a sessão. */
export function sessionTitle(timeline: { contact_name?: string | null; user_id?: string | null; session_id: string }): string {
  return timeline.contact_name || timeline.user_id || timeline.session_id;
}
