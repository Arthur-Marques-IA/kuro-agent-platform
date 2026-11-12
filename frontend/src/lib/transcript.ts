/**
 * O que o agente lembra de uma sessão (`GET /observability/sessions/{id}/transcript`): o
 * resumo da sessão e as memórias do contato. A conversa em si vem da linha do tempo
 * (`lib/timeline.ts`), já separada em campos — nada aqui recorta texto.
 */
export type SessionMemory = {
  resumo_da_sessao: { texto: string; atualizado_em?: string | null } | null;
  memorias_do_contato: { texto: string; topicos: string[]; atualizado_em?: string | null }[];
};
