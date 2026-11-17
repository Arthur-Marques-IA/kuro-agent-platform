/**
 * A conta da tela "Composição do prompt": quanto do que foi enviado ao modelo veio de cada parte.
 * Os números do servidor são caracteres exatos; aqui só ordenamos, filtramos o que é zero e
 * calculamos a fração de cada parte e do cache.
 */
import type { PromptComposition, PromptPart } from "./types";

/** Ordem de leitura: do que o agente é (instruções) até o que o contato disse agora. */
export const PROMPT_PART_ORDER: PromptPart[] = [
  "instructions",
  "session_context",
  "memories",
  "session_summary",
  "history",
  "history_tools",
  "dependencies",
  "turn",
  "other",
];

export const PROMPT_PART_LABELS: Record<PromptPart, string> = {
  instructions: "Instruções do agente",
  session_context: "Contexto da sessão",
  memories: "Memórias",
  session_summary: "Resumo da sessão",
  history: "Histórico da conversa",
  history_tools: "Tools do histórico",
  dependencies: "Dependencies",
  turn: "Mensagem do turno",
  other: "Outras",
};

/** Classes literais (o Tailwind só gera o que enxerga no código). As cores se repetem com
 *  opacidades diferentes, mas a legenda traz nome e percentual: a cor nunca é o único sinal. */
export const PROMPT_PART_BARS: Record<PromptPart, string> = {
  instructions: "bg-primary",
  session_context: "bg-success",
  memories: "bg-warning",
  session_summary: "bg-warning/50",
  history: "bg-primary/50",
  history_tools: "bg-success/50",
  dependencies: "bg-muted-foreground",
  turn: "bg-foreground",
  other: "bg-muted-foreground/50",
};

export interface PromptRow {
  part: PromptPart;
  label: string;
  chars: number;
  /** 0–100 */
  percent: number;
}

/** As partes com conteúdo, em ordem de leitura. */
export function promptRows(composition: PromptComposition): PromptRow[] {
  const total = composition.chars || Object.values(composition.parts).reduce((sum, n) => sum + (n ?? 0), 0);
  return PROMPT_PART_ORDER.flatMap((part) => {
    const chars = composition.parts[part] ?? 0;
    return chars > 0 ? [{ part, label: PROMPT_PART_LABELS[part], chars, percent: total ? (chars / total) * 100 : 0 }] : [];
  });
}

/** "42%", "<1%" para o que existe mas é minúsculo. */
export function formatPercent(percent: number): string {
  if (percent > 0 && percent < 1) return "<1%";
  return `${Math.round(percent)}%`;
}

/** Fração do prompt lida do cache (0–1), ou `null` sem tokens de entrada. No Claude a entrada
 *  não inclui o cache, então o valor pode passar de 1 e é limitado a 1. */
export function cacheFraction(cacheReadTokens: number | undefined, inputTokens: number | undefined): number | null {
  if (!inputTokens || !cacheReadTokens) return null;
  return Math.min(cacheReadTokens / inputTokens, 1);
}
