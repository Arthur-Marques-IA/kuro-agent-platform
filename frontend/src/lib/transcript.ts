/**
 * Separa o contexto que o sistema chamador manda junto da fala do contato — o mesmo
 * corte de `observability/transcript.py` (mantenha os marcadores iguais). Sem
 * marcador, a mensagem inteira é a fala.
 */
const FALA_MARKERS = ["[FALA DO CONTATO]", "ÚLTIMA MENSAGEM DO ALUNO:", "ÚLTIMA MENSAGEM DO CONTATO:"];

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
  return { contexto: text.slice(0, best).trim(), fala: text.slice(best + marker.length).trim() };
}

export type SessionMemory = {
  resumo_da_sessao: { texto: string; atualizado_em?: string | null } | null;
  memorias_do_contato: { texto: string; topicos: string[]; atualizado_em?: string | null }[];
};
