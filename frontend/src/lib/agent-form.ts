/**
 * O estado do formulário de agente (`components/agents/agent-form.tsx`): os valores iniciais,
 * o que mudou e o corpo do `PUT`/`POST`.
 *
 * A regra que evita o "sujo ao abrir": as alterações são calculadas contra os **valores iniciais
 * do formulário** (`valuesFrom(agent)`), nunca contra a resposta crua da API. Assim um campo que
 * o serviço não devolveu (versão antiga, campo opcional) vira o mesmo default dos dois lados, e as
 * instruções passam pela mesma normalização (uma por linha) antes de comparar. Só vai no `PUT` o
 * que o usuário mexeu.
 *
 * Sem imports de `@/`: roda direto no test runner do Node (`npm test`).
 */
import type { ModelOption } from "./agent-meta";
import type {
  AgentDefinition,
  AgentDefinitionInput,
  AgentKind,
  DependencyField,
  MemoryBackend,
  ModelParams,
  ProcedureStage,
  SchemaField,
} from "./types";
import { cleanStages } from "./procedure-stages.ts";

export type AgentFormPayload = Partial<AgentDefinitionInput>;

export interface FormValues {
  agentType: string;
  name: string;
  kind: AgentKind;
  responseSchema: SchemaField[];
  /** Só em procedural. */
  stages: ProcedureStage[];
  /** Uma instrução por linha. */
  instructions: string;
  tools: string[];
  modelId: string;
  /** "" = usa a credencial padrão do provedor (a mais antiga habilitada). */
  credentialId: string;
  /** "" = agente sem base de conhecimento. */
  knowledgeCollection: string;
  dependencyFields: DependencyField[];
  memoryBackend: MemoryBackend;
  numHistoryRuns: number;
  /** Parâmetros de geração como texto: "" = padrão do provedor. */
  temperature: string;
  topP: string;
  maxTokens: string;
  /** Ajuste fino antigo (Gemini); só é preservado, não se edita mais pela tela. */
  thinkingBudget: string;
  reasoning: "" | "off" | "low" | "medium" | "high";
  /** Cache do prefixo (instructions + tools) no Claude; "" = desligado. */
  promptCache: "" | "5m" | "1h";
  sessionSummary: boolean;
  plainText: boolean;
  /** "" = RUN_TIMEOUT_SECONDS do serviço. */
  timeoutSeconds: string;
}

/** Os modelos conhecidos e o padrão — o seletor e o corpo do `PUT` resolvem o provedor por aqui. */
export interface ModelContext {
  models: ModelOption[];
  defaultModel: ModelOption;
}

export const DEFAULT_HISTORY_RUNS = 10;

const asText = (value: number | null | undefined) => (value === null || value === undefined ? "" : String(value));

/** Os valores do formulário para um agente salvo (ou vazios, num agente novo). Todo campo que
 * a API pode omitir cai num default aqui — e só aqui. */
export function valuesFrom(agent: AgentDefinition | undefined, defaultModelId: string, instructions?: string[]): FormValues {
  const params = agent?.model_params ?? null;
  return {
    agentType: agent?.agent_type ?? "",
    name: agent?.name ?? "",
    kind: agent?.kind ?? "conversational",
    responseSchema: agent?.response_schema ?? [],
    stages: agent?.stages ?? [],
    instructions: (instructions ?? agent?.instructions ?? []).join("\n"),
    tools: agent?.tools ?? [],
    modelId: agent?.model_id ?? defaultModelId,
    credentialId: agent?.model_credential_id ?? "",
    knowledgeCollection: agent?.knowledge_collection ?? "",
    dependencyFields: agent?.dependency_fields ?? [],
    // Agente novo nasce sem memória de longo prazo: liga quem precisa.
    memoryBackend: agent?.memory_backend ?? "none",
    numHistoryRuns: agent?.num_history_runs ?? DEFAULT_HISTORY_RUNS,
    temperature: asText(params?.temperature),
    topP: asText(params?.top_p),
    maxTokens: asText(params?.max_tokens),
    thinkingBudget: asText(params?.thinking_budget),
    reasoning: params?.reasoning ?? "",
    promptCache: params?.prompt_cache ?? "",
    sessionSummary: agent?.session_summary ?? false,
    plainText: agent?.plain_text ?? false,
    timeoutSeconds: asText(agent?.timeout_seconds),
  };
}

/** As instruções do texto do editor: uma por linha, sem linhas vazias. */
export function toLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

/** Faixas iguais às do backend (`models/params.py`). */
export const PARAM_LIMITS = {
  temperature: { min: 0, max: 2, integer: false, label: "Temperatura" },
  topP: { min: 0, max: 1, integer: false, label: "top_p" },
  maxTokens: { min: 1, max: 200000, integer: true, label: "Máx. de tokens" },
  thinkingBudget: { min: 0, max: 32768, integer: true, label: "Orçamento de raciocínio" },
  timeoutSeconds: { min: 1, max: 600, integer: true, label: "Tempo limite" },
} as const;

export function numberProblem(key: keyof typeof PARAM_LIMITS, raw: string): string | null {
  if (raw.trim() === "") return null;
  const limits = PARAM_LIMITS[key];
  const value = Number(raw);
  if (Number.isNaN(value)) return `${limits.label}: use um número.`;
  if (limits.integer && !Number.isInteger(value)) return `${limits.label}: use um número inteiro.`;
  if (value < limits.min || value > limits.max) return `${limits.label}: entre ${limits.min} e ${limits.max}.`;
  return null;
}

/** Os parâmetros do formulário no formato da API; `null` = padrão do provedor.
 * `thinking_budget` só vai para o google — em outro provedor o backend recusa. */
export function paramsFrom(values: FormValues, provider: string | undefined): ModelParams | null {
  const params: ModelParams = {};
  if (values.temperature.trim()) params.temperature = Number(values.temperature);
  if (values.topP.trim()) params.top_p = Number(values.topP);
  if (values.maxTokens.trim()) params.max_tokens = Number(values.maxTokens);
  const supportsReasoning = ["google", "openai"].includes(provider ?? "google");
  if (values.reasoning && supportsReasoning) {
    params.reasoning = values.reasoning;
  } else if (values.thinkingBudget.trim() && (provider ?? "google") === "google") {
    params.thinking_budget = Number(values.thinkingBudget);
  }
  if (values.promptCache && provider === "anthropic") params.prompt_cache = values.promptCache;
  return Object.keys(params).length ? params : null;
}

export function sameParams(a: ModelParams | null | undefined, b: ModelParams | null | undefined): boolean {
  const norm = (p: ModelParams | null | undefined) =>
    JSON.stringify(Object.entries(p ?? {}).sort(([x], [y]) => x.localeCompare(y)));
  return norm(a) === norm(b);
}

function sameList(a: string[], b: string[]) {
  return a.length === b.length && a.every((value, i) => value === b[i]);
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

const PARAM_KEYS = ["temperature", "topP", "maxTokens", "thinkingBudget", "reasoning", "promptCache"] as const;

function resolveModel(modelId: string, ctx: ModelContext): ModelOption {
  return ctx.models.find((m) => m.id === modelId) ?? ctx.defaultModel;
}

/** As instruções mudaram em relação aos valores iniciais (mesma normalização dos dois lados)? */
export function instructionsChanged(values: FormValues, initial: FormValues): boolean {
  return !sameList(toLines(values.instructions), toLines(initial.instructions));
}

/**
 * Só os campos que o usuário alterou — assim editar o nome não gera uma versão nova do prompt,
 * e abrir sem mexer em nada dá `{}`. `initial` é `valuesFrom(agent)`; `agent` só serve para os
 * parâmetros de geração, que comparam com o que está salvo.
 */
export function buildUpdate(
  values: FormValues,
  initial: FormValues,
  agent: AgentDefinition,
  ctx: ModelContext
): AgentFormPayload {
  const payload: AgentFormPayload = {};
  if (values.name.trim() !== initial.name.trim()) payload.name = values.name.trim();
  if (instructionsChanged(values, initial)) payload.instructions = toLines(values.instructions);
  if (!sameList([...values.tools].sort(), [...initial.tools].sort())) payload.tools = values.tools;
  const modelChanged = values.modelId !== initial.modelId;
  if (modelChanged) {
    const model = resolveModel(values.modelId, ctx);
    payload.model_provider = model.provider;
    payload.model_id = model.id;
  }
  if (values.credentialId !== initial.credentialId) payload.model_credential_id = values.credentialId || null;
  if (values.knowledgeCollection !== initial.knowledgeCollection) {
    payload.knowledge_collection = values.knowledgeCollection || null;
  }
  if (!same(values.dependencyFields, initial.dependencyFields)) payload.dependency_fields = values.dependencyFields;
  if (!same(values.responseSchema, initial.responseSchema)) payload.response_schema = values.responseSchema;
  if (values.kind === "procedural" && !same(cleanStages(values.stages), cleanStages(initial.stages))) {
    payload.stages = cleanStages(values.stages);
  }
  // Os parâmetros dependem do provedor (o que um não aceita, o outro descarta): só são
  // recalculados quando algum deles ou o modelo mudou, e só vão se o resultado diferir do salvo.
  if (modelChanged || PARAM_KEYS.some((key) => values[key] !== initial[key])) {
    const params = paramsFrom(values, resolveModel(values.modelId, ctx).provider);
    if (!sameParams(params, agent.model_params)) payload.model_params = params ?? {};
  }
  if (values.timeoutSeconds.trim() !== initial.timeoutSeconds.trim()) {
    payload.timeout_seconds = values.timeoutSeconds.trim() ? Number(values.timeoutSeconds) : null;
  }
  // `num_history_runs` e `memory_backend` não existem num agente analista: o
  // backend responde 422 se vierem com valor diferente do padrão.
  if (values.kind !== "analysis") {
    if (values.memoryBackend !== initial.memoryBackend) payload.memory_backend = values.memoryBackend;
    if (!Object.is(values.numHistoryRuns, initial.numHistoryRuns)) payload.num_history_runs = values.numHistoryRuns;
    if (values.sessionSummary !== initial.sessionSummary) payload.session_summary = values.sessionSummary;
    if (values.plainText !== initial.plainText) payload.plain_text = values.plainText;
  }
  return payload;
}

/** O corpo do `POST /agents` de um agente novo. */
export function buildCreate(values: FormValues, ctx: ModelContext): AgentDefinitionInput {
  const model = resolveModel(values.modelId, ctx);
  const analysis = values.kind === "analysis";
  return {
    agent_type: values.agentType,
    name: values.name.trim(),
    kind: values.kind,
    instructions: toLines(values.instructions),
    tools: values.tools,
    model_provider: model.provider,
    model_id: model.id,
    model_credential_id: values.credentialId || null,
    knowledge_collection: values.knowledgeCollection || null,
    dependency_fields: values.dependencyFields,
    response_schema: analysis ? values.responseSchema : [],
    stages: values.kind === "procedural" ? cleanStages(values.stages) : [],
    model_params: paramsFrom(values, model.provider),
    timeout_seconds: values.timeoutSeconds.trim() ? Number(values.timeoutSeconds) : null,
    // Omitidos em analysis: o backend recusa com 422 um campo que não
    // faz nada num agente one-shot, em vez de aceitá-lo calado.
    ...(analysis
      ? {}
      : {
          memory_backend: values.memoryBackend,
          num_history_runs: values.numHistoryRuns,
          session_summary: values.sessionSummary,
          plain_text: values.plainText,
        }),
  };
}

// --- Seções (índice lateral) -------------------------------------------------------------

export type FormSectionId =
  | "identidade"
  | "tipo"
  | "prompt"
  | "etapas"
  | "modelo"
  | "memoria"
  | "saida"
  | "contexto"
  | "tools";

export interface FormSectionInfo {
  id: FormSectionId;
  label: string;
}

/** As seções na ordem da página; Etapas só em procedural, Saída só em analista. */
export function formSections(kind: AgentKind): FormSectionInfo[] {
  const list: (FormSectionInfo | null)[] = [
    { id: "identidade", label: "Identidade" },
    { id: "tipo", label: "Tipo" },
    { id: "prompt", label: "Prompt" },
    kind === "procedural" ? { id: "etapas", label: "Etapas" } : null,
    { id: "modelo", label: "Modelo" },
    { id: "memoria", label: "Memória" },
    kind === "analysis" ? { id: "saida", label: "Saída" } : null,
    { id: "contexto", label: "Contexto" },
    { id: "tools", label: "Tools" },
  ];
  return list.filter((s): s is FormSectionInfo => s !== null);
}

const PAYLOAD_SECTION: Record<keyof AgentDefinitionInput, FormSectionId> = {
  agent_type: "identidade",
  name: "identidade",
  kind: "tipo",
  instructions: "prompt",
  stages: "etapas",
  model_provider: "modelo",
  model_id: "modelo",
  model_credential_id: "modelo",
  model_params: "modelo",
  timeout_seconds: "modelo",
  knowledge_collection: "modelo",
  memory_backend: "memoria",
  num_history_runs: "memoria",
  session_summary: "memoria",
  plain_text: "memoria",
  response_schema: "saida",
  dependency_fields: "contexto",
  tools: "tools",
};

/** Os erros de validação do formulário, por campo (`null` = ok). */
export interface FormErrors {
  name: string | null;
  agentType: string | null;
  instructions: string | null;
  numHistoryRuns: string | null;
  modelParams: string | null;
  responseSchema: string | null;
  stages: string | null;
}

const ERROR_SECTION: Record<keyof FormErrors, FormSectionId> = {
  name: "identidade",
  agentType: "identidade",
  instructions: "prompt",
  numHistoryRuns: "memoria",
  modelParams: "modelo",
  responseSchema: "saida",
  stages: "etapas",
};

/** Quais seções têm alteração não salva e quais têm erro — para o índice lateral. */
export function sectionStatus(
  changes: AgentFormPayload | null,
  errors: FormErrors | null
): { changed: Set<FormSectionId>; invalid: Set<FormSectionId> } {
  const changed = new Set<FormSectionId>();
  const invalid = new Set<FormSectionId>();
  for (const key of Object.keys(changes ?? {}) as (keyof AgentDefinitionInput)[]) {
    const section = PAYLOAD_SECTION[key];
    if (section) changed.add(section);
  }
  for (const key of Object.keys(errors ?? {}) as (keyof FormErrors)[]) {
    if (errors?.[key]) invalid.add(ERROR_SECTION[key]);
  }
  return { changed, invalid };
}

// --- Busca no editor de prompt -------------------------------------------------------------

export interface TextMatch {
  /** Posição no texto (para `setSelectionRange`). */
  start: number;
  end: number;
  /** Linha (0-based) onde a ocorrência começa. */
  line: number;
}

/** Ocorrências de `query` no texto, sem diferenciar maiúsculas. */
export function findMatches(text: string, query: string): TextMatch[] {
  const needle = query.toLocaleLowerCase("pt-BR");
  if (!needle) return [];
  const haystack = text.toLocaleLowerCase("pt-BR");
  // `toLocaleLowerCase` pode mudar o tamanho em alguns caracteres; nesse caso as posições
  // deixariam de bater com o texto original — melhor não destacar nada do que destacar errado.
  if (haystack.length !== text.length) return [];
  const matches: TextMatch[] = [];
  let line = 0;
  let lineCursor = 0;
  for (let at = haystack.indexOf(needle); at !== -1; at = haystack.indexOf(needle, at + needle.length)) {
    for (; lineCursor < at; lineCursor++) if (text[lineCursor] === "\n") line++;
    matches.push({ start: at, end: at + needle.length, line });
  }
  return matches;
}
