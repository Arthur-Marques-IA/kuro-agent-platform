/**
 * A organização da página Tools: em que grupo cada tool cai, quem a usa e os filtros em chip.
 * Sem imports de `@/`: roda direto no test runner do Node (`npm test`).
 */
import type { AgentDefinition, ToolKind, ToolSummary } from "./types";

/** Quem usa uma tool — em `tools` ou numa etapa `action` de um procedural. */
export interface ToolUsage {
  /** Agentes "de verdade" (sem os `*-draft`). */
  agents: string[];
  /** Drafts (`*-draft`): cópias de teste, não contam como uso em produção. */
  drafts: string[];
}

export const NO_USAGE: ToolUsage = { agents: [], drafts: [] };

export function isDraftAgent(agentType: string): boolean {
  return agentType.endsWith("-draft");
}

export function toolUsageIndex(agents: Pick<AgentDefinition, "agent_type" | "tools" | "stages">[]): Map<string, ToolUsage> {
  const index = new Map<string, ToolUsage>();
  for (const agent of agents) {
    const names = new Set<string>(agent.tools ?? []);
    for (const stage of agent.stages ?? []) if (stage.tool) names.add(stage.tool);
    for (const name of names) {
      const usage = index.get(name) ?? { agents: [], drafts: [] };
      (isDraftAgent(agent.agent_type) ? usage.drafts : usage.agents).push(agent.agent_type);
      index.set(name, usage);
    }
  }
  for (const usage of index.values()) {
    usage.agents.sort();
    usage.drafts.sort();
  }
  return index;
}

/** Método e destino de uma tool `api`; `null` para os outros tipos. */
export function apiEndpoint(tool: Pick<ToolSummary, "kind" | "config">): { method: string; host: string; url: string } | null {
  if (tool.kind !== "api") return null;
  const url = typeof tool.config.url === "string" ? tool.config.url : "";
  const method = typeof tool.config.method === "string" ? tool.config.method.toUpperCase() : "GET";
  return { method, host: urlHost(url), url };
}

/** O host de uma URL — tolera `{param}` no meio (um parâmetro `location="path"` pode compor o host). */
export function urlHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    const match = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]+)/i.exec(url.trim());
    return match ? match[1] : url.trim();
  }
}

/**
 * Até a 1ª frase da descrição (que é escrita para o modelo). Só corta num ponto seguido de espaço e
 * letra maiúscula, fora de colchetes/parênteses/aspas, para não partir "[SISTEMA — ...]" ao meio.
 */
export function firstSentence(text: string | null | undefined): { head: string; rest: boolean } {
  const value = (text ?? "").trim();
  if (!value) return { head: "", rest: false };
  let depth = 0;
  let quoted = false;
  for (let i = 0; i < value.length; i++) {
    const ch = value[i];
    if (ch === '"') quoted = !quoted;
    else if (ch === "[" || ch === "(") depth++;
    else if ((ch === "]" || ch === ")") && depth > 0) depth--;
    else if ((ch === "." || ch === "!" || ch === "?") && depth === 0 && !quoted) {
      const after = value.slice(i + 1);
      if (/^\s+[A-ZÀ-Ý]/.test(after)) return { head: value.slice(0, i + 1), rest: true };
    }
  }
  return { head: value, rest: false };
}

// -- Grupos -------------------------------------------------------------

export const SYSTEM_GROUP = "__system__";
export const OTHER_GROUP = "__other__";

export interface ToolGroup {
  key: string;
  title: string;
  /** Hosts das tools `api` do grupo, para dizer de onde elas vêm. */
  hosts: string[];
  tools: ToolSummary[];
}

function prefixOf(toolName: string): string | null {
  const idx = toolName.indexOf("_");
  return idx > 0 ? toolName.slice(0, idx) : null;
}

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/**
 * Agrupa por integração: o prefixo do `tool_name` (antes do 1º `_`) quando duas ou mais tools o
 * dividem; senão o host da API quando duas ou mais o dividem; senão "Outras". As tools de sistema
 * (`is_seed`) vão para um grupo próprio, sempre por último.
 */
export function groupTools(tools: ToolSummary[]): ToolGroup[] {
  const own = tools.filter((t) => !t.is_seed);
  const prefixCount = new Map<string, number>();
  for (const t of own) {
    const p = prefixOf(t.tool_name);
    if (p) prefixCount.set(p, (prefixCount.get(p) ?? 0) + 1);
  }
  const hostCount = new Map<string, number>();
  for (const t of own) {
    const p = prefixOf(t.tool_name);
    if (p && (prefixCount.get(p) ?? 0) >= 2) continue;
    const ep = apiEndpoint(t);
    if (ep?.host) hostCount.set(ep.host, (hostCount.get(ep.host) ?? 0) + 1);
  }

  const groups = new Map<string, ToolGroup>();
  const add = (key: string, title: string, tool: ToolSummary) => {
    const group = groups.get(key) ?? { key, title, hosts: [], tools: [] };
    group.tools.push(tool);
    const host = apiEndpoint(tool)?.host;
    if (host && !group.hosts.includes(host)) group.hosts.push(host);
    groups.set(key, group);
  };

  for (const t of own) {
    const p = prefixOf(t.tool_name);
    const host = apiEndpoint(t)?.host;
    if (p && (prefixCount.get(p) ?? 0) >= 2) add(`prefix:${p}`, titleCase(p), t);
    else if (host && (hostCount.get(host) ?? 0) >= 2) add(`host:${host}`, host, t);
    else add(OTHER_GROUP, "Outras", t);
  }

  const ordered = [...groups.values()]
    .filter((g) => g.key !== OTHER_GROUP)
    .sort((a, b) => a.title.localeCompare(b.title, "pt-BR"));
  const other = groups.get(OTHER_GROUP);
  if (other) ordered.push({ ...other, title: ordered.length ? "Outras" : "Suas tools" });
  const system = tools.filter((t) => t.is_seed);
  if (system.length) {
    const hosts = [...new Set(system.map((t) => apiEndpoint(t)?.host).filter((h): h is string => !!h))];
    ordered.push({ key: SYSTEM_GROUP, title: "Sistema e exemplos", hosts, tools: system });
  }
  for (const g of ordered) g.tools.sort((a, b) => a.label.localeCompare(b.label, "pt-BR"));
  return ordered;
}

// -- Filtros ------------------------------------------------------------

export interface ToolFilters {
  query: string;
  kind: ToolKind | null;
  sideEffect: boolean;
  unclassified: boolean;
  disabled: boolean;
  unused: boolean;
}

export const EMPTY_FILTERS: ToolFilters = {
  query: "",
  kind: null,
  sideEffect: false,
  unclassified: false,
  disabled: false,
  unused: false,
};

export function hasActiveFilters(f: ToolFilters): boolean {
  return !!f.query.trim() || f.kind !== null || f.sideEffect || f.unclassified || f.disabled || f.unused;
}

/** Mesmo critério da fila de revisão dos Logs: só conta tool ligada. */
export function isUnclassified(tool: Pick<ToolSummary, "side_effect" | "enabled">): boolean {
  return tool.side_effect == null && tool.enabled;
}

/** Tool própria que nenhum agente (fora os drafts) usa; as de sistema não entram nessa conta. */
export function isUnused(tool: Pick<ToolSummary, "tool_name" | "is_seed">, usage: Map<string, ToolUsage>): boolean {
  return !tool.is_seed && (usage.get(tool.tool_name)?.agents.length ?? 0) === 0;
}

export function matchesFilters(tool: ToolSummary, f: ToolFilters, usage: Map<string, ToolUsage>): boolean {
  if (f.kind && tool.kind !== f.kind) return false;
  if (f.sideEffect && tool.side_effect !== true) return false;
  if (f.unclassified && !isUnclassified(tool)) return false;
  if (f.disabled && tool.enabled) return false;
  if (f.unused && !isUnused(tool, usage)) return false;
  const q = f.query.trim().toLowerCase();
  if (!q) return true;
  const u = usage.get(tool.tool_name);
  const haystack = [
    tool.tool_name,
    tool.label,
    tool.description ?? "",
    apiEndpoint(tool)?.url ?? "",
    ...(u?.agents ?? []),
    ...(u?.drafts ?? []),
  ];
  return haystack.join(" ").toLowerCase().includes(q);
}
