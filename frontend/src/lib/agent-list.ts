/**
 * A organização da lista de agentes (`components/agents/agents-index.tsx`): o par produção/draft
 * num cartão só, o filtro por tipo e a ordenação por uso recente.
 * Sem imports de `@/`: roda direto no test runner do Node (`npm test`).
 */
import type { AgentDefinition, AgentKind } from "./types";

export const DRAFT_SUFFIX = "-draft";

export const KIND_LABELS: Record<AgentKind, string> = {
  conversational: "Conversacional",
  analysis: "Analista",
  procedural: "Procedural",
};

/** Um cartão da lista: o agente e, se existir, o `<agent_type>-draft` dele. */
export interface AgentEntry {
  agent: AgentDefinition;
  draft: AgentDefinition | null;
}

/** O `agent_type` de produção de um draft (`r8-draft` → `r8`); `null` se não for draft. */
export function draftOf(agentType: string): string | null {
  return agentType.endsWith(DRAFT_SUFFIX) && agentType.length > DRAFT_SUFFIX.length
    ? agentType.slice(0, -DRAFT_SUFFIX.length)
    : null;
}

/**
 * Junta cada `<agent_type>-draft` ao agente de produção. Um draft sem o agente de produção
 * continua sozinho no próprio cartão.
 */
export function pairAgents(agents: AgentDefinition[]): AgentEntry[] {
  const byType = new Map(agents.map((a) => [a.agent_type, a]));
  const entries: AgentEntry[] = [];
  for (const agent of agents) {
    const prod = draftOf(agent.agent_type);
    if (prod && byType.has(prod)) continue;
    entries.push({ agent, draft: byType.get(`${agent.agent_type}${DRAFT_SUFFIX}`) ?? null });
  }
  return entries;
}

/**
 * Os campos que o promote copia, agrupados com o nome que aparece no cartão. Cada grupo
 * lista os campos de verdade, para o diálogo de comparação mostrar o valor de cada lado.
 */
const COMPARED: { label: string; fields: Record<string, (a: AgentDefinition) => unknown> }[] = [
  { label: "prompt", fields: { instructions: (a) => a.instructions } },
  {
    label: "modelo",
    fields: {
      model_provider: (a) => a.model_provider,
      model_id: (a) => a.model_id,
      model_credential_id: (a) => a.model_credential_id,
      model_params: (a) => a.model_params ?? null,
      timeout_seconds: (a) => a.timeout_seconds ?? null,
    },
  },
  { label: "tools", fields: { tools: (a) => [...a.tools].sort() } },
  {
    label: "memória",
    fields: {
      memory_backend: (a) => a.memory_backend,
      num_history_runs: (a) => a.num_history_runs,
      history_tool_calls: (a) => a.history_tool_calls ?? null,
      session_summary: (a) => a.session_summary ?? false,
    },
  },
  { label: "base", fields: { knowledge_collection: (a) => a.knowledge_collection } },
  { label: "dependências", fields: { dependency_fields: (a) => a.dependency_fields } },
  { label: "schema", fields: { response_schema: (a) => a.response_schema } },
  { label: "etapas", fields: { stages: (a) => a.stages ?? [] } },
  { label: "formato", fields: { plain_text: (a) => a.plain_text ?? false } },
  { label: "tipo", fields: { kind: (a) => a.kind } },
];

/** Um campo que difere entre produção e draft, com o valor de cada lado. */
export interface FieldChange {
  group: string;
  field: string;
  prod: unknown;
  draft: unknown;
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Campo a campo, o que o draft mudou em relação à produção. */
export function draftChanges(prod: AgentDefinition, draft: AgentDefinition): FieldChange[] {
  return COMPARED.flatMap(({ label, fields }) =>
    Object.entries(fields)
      .filter(([, pick]) => !same(pick(prod), pick(draft)))
      .map(([field, pick]) => ({ group: label, field, prod: pick(prod), draft: pick(draft) }))
  );
}

/** No que o draft difere da produção, na linguagem do cartão ("prompt", "modelo"...). */
export function draftDifferences(prod: AgentDefinition, draft: AgentDefinition): string[] {
  return [...new Set(draftChanges(prod, draft).map((c) => c.group))];
}

export type AgentSort = "recent" | "name" | "updated";

export const SORT_LABELS: Record<AgentSort, string> = {
  recent: "Uso recente",
  name: "Nome",
  updated: "Editados por último",
};

function time(value: string | null | undefined): number {
  const t = value ? new Date(value).getTime() : NaN;
  return Number.isFinite(t) ? t : -Infinity;
}

/** O uso mais recente do cartão: o do agente ou o do draft, o que for mais novo. */
export function entryLastUsed(entry: AgentEntry, lastUsed: Map<string, string>): string | null {
  const own = lastUsed.get(entry.agent.agent_type) ?? null;
  const draft = entry.draft ? (lastUsed.get(entry.draft.agent_type) ?? null) : null;
  if (!own) return draft;
  if (!draft) return own;
  return time(draft) > time(own) ? draft : own;
}

/**
 * Ordena os cartões. Em "uso recente", quem nunca rodou vai para o fim, pelo mais editado;
 * empate sempre cai no nome, para a ordem não pular entre recarregamentos.
 */
export function sortEntries(entries: AgentEntry[], sort: AgentSort, lastUsed: Map<string, string>): AgentEntry[] {
  const byName = (a: AgentEntry, b: AgentEntry) => a.agent.name.localeCompare(b.agent.name, "pt-BR");
  const updated = (e: AgentEntry) => Math.max(time(e.agent.updated_at), e.draft ? time(e.draft.updated_at) : -Infinity);
  const compare: Record<AgentSort, (a: AgentEntry, b: AgentEntry) => number> = {
    name: byName,
    updated: (a, b) => updated(b) - updated(a) || byName(a, b),
    recent: (a, b) =>
      time(entryLastUsed(b, lastUsed)) - time(entryLastUsed(a, lastUsed)) || updated(b) - updated(a) || byName(a, b),
  };
  return [...entries].sort((a, b) => {
    const diff = compare[sort](a, b);
    return Number.isNaN(diff) ? 0 : diff;
  });
}

/** Junta as datas de uso de várias fontes (execuções, conversas), ficando com a mais nova. */
export function mergeLastUsed(...sources: [string, string | null | undefined][][]): Map<string, string> {
  const out = new Map<string, string>();
  for (const source of sources) {
    for (const [agentType, at] of source) {
      if (!at) continue;
      const current = out.get(agentType);
      if (!current || time(at) > time(current)) out.set(agentType, at);
    }
  }
  return out;
}
