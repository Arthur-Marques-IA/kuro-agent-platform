/**
 * Separa o contexto que o sistema chamador manda junto da fala do contato — o mesmo
 * corte de `observability/transcript.py` (mantenha os marcadores iguais). Sem
 * marcador, a mensagem inteira é a fala.
 */
const FALA_MARKERS = ["[FALA DO CONTATO]", "ÚLTIMA MENSAGEM DO ALUNO:", "ÚLTIMA MENSAGEM DO CONTATO:"];

const HISTORY_MARKER = "HISTÓRICO DA CONVERSA";
const TEMPORAL_PREFIX = "CONTEXTO TEMPORAL:";

/** Tira o histórico repetido e a linha temporal (muda todo turno) do contexto — igual a `clean_context`. */
function cleanContext(contexto: string): string {
  const pos = contexto.indexOf(HISTORY_MARKER);
  const semHistorico = pos >= 0 ? contexto.slice(0, pos) : contexto;
  return semHistorico
    .split("\n")
    .filter((line) => !line.startsWith(TEMPORAL_PREFIX))
    .join("\n")
    .trim();
}

export function splitInput(text: string): { contexto: string; fala: string } {
  let best = -1;
  let marker = "";
  for (const m of FALA_MARKERS) {
    const pos = text.lastIndexOf(m);
    if (pos > best) {
      best = pos;
      marker = m;
    }
  }
  if (best < 0) return { contexto: "", fala: text.trim() };
  return { contexto: cleanContext(text.slice(0, best)), fala: text.slice(best + marker.length).trim() };
}

export type SessionMemory = {
  resumo_da_sessao: { texto: string; atualizado_em?: string | null } | null;
  memorias_do_contato: { texto: string; topicos: string[]; atualizado_em?: string | null }[];
};
