import type { MemoryBackend, ToolKind } from "@/lib/types";

export interface ModelOption {
  provider: string;
  id: string;
  label: string;
  /** Deduzido do nome pelo backend: `preview` e `alias` (`-latest`) podem mudar sem aviso. */
  channel?: "stable" | "preview" | "alias" | null;
}

/**
 * Modelos oferecidos no formulário — espelha o que `models/provider.py` sabe
 * montar. Só aparecem no seletor os provedores cadastrados e habilitados em
 * `/models` (ver `agent-form.tsx`); `google` sempre aparece, mesmo sem chave
 * salva, pelo fallback antigo via `GOOGLE_API_KEY` no ambiente.
 */
export const MODEL_OPTIONS: ModelOption[] = [
  // Apelidos `-latest`: acompanham a versão estável mais nova e não saem de linha.
  { provider: "google", id: "gemini-flash-latest", label: "Gemini Flash (mais recente)" },
  { provider: "google", id: "gemini-pro-latest", label: "Gemini Pro (mais recente)" },
  { provider: "google", id: "gemini-flash-lite-latest", label: "Gemini Flash-Lite (mais recente)" },
  { provider: "openai", id: "gpt-4.1", label: "GPT-4.1" },
  { provider: "openai", id: "gpt-4.1-mini", label: "GPT-4.1 Mini" },
  { provider: "openai", id: "gpt-4o-mini", label: "GPT-4o Mini" },
  { provider: "anthropic", id: "claude-sonnet-4-5", label: "Claude Sonnet 4.5" },
  { provider: "anthropic", id: "claude-haiku-4-5", label: "Claude Haiku 4.5" },
  { provider: "ollama", id: "llama3.1", label: "Llama 3.1 (Ollama)" },
  { provider: "ollama", id: "qwen2.5", label: "Qwen 2.5 (Ollama)" },
];

export const DEFAULT_MODEL = MODEL_OPTIONS[0];

export function modelLabel(modelId?: string | null): string {
  if (!modelId) return "Modelo padrão do serviço";
  return MODEL_OPTIONS.find((m) => m.id === modelId)?.label ?? modelId;
}

export const MEMORY_BACKENDS: Record<MemoryBackend, { label: string; description: string }> = {
  none: { label: "Desligada", description: "Só o histórico da sessão." },
  auto: {
    label: "Automática",
    description:
      "Depois de cada resposta, o que vale lembrar do usuário é extraído em paralelo, com o modelo auxiliar. O modelo do agente não ganha tool nem rodada extra.",
  },
  agentic: {
    label: "Agêntica",
    description:
      "O próprio modelo decide quando gravar, pela tool update_user_memory. Mais controle, mas cada gravação é uma chamada extra no meio da resposta.",
  },
  common: { label: "Agêntica", description: "Nome antigo da memória agêntica." },
};

/** Os modos oferecidos quando a memória de longo prazo está ligada (`common` é legado de `agentic`). */
export const MEMORY_MODES = ["auto", "agentic"] as const;

export const REASONING_LEVELS: { value: "" | "off" | "low" | "medium" | "high"; label: string }[] = [
  { value: "", label: "Padrão do modelo" },
  { value: "off", label: "Desligado" },
  { value: "low", label: "Baixo" },
  { value: "medium", label: "Médio" },
  { value: "high", label: "Alto" },
];

export const TOOL_KIND_META: Record<ToolKind, { label: string; description: string }> = {
  builtin: { label: "Padrão", description: "Uma toolkit pronta do Agno (busca, calculadora...)." },
  api: { label: "API", description: "Chama uma API HTTP existente, descrita em JSON — sem código." },
  python: { label: "Python", description: "Uma função Python enviada por você, executada num namespace restrito." },
};

/** Valida o JSON do campo `dependencies` do chat. */
export function parseDependencies(text: string): {
  value: Record<string, unknown> | null;
  error: string | null;
  count: number;
} {
  if (!text.trim()) return { value: null, error: null, count: 0 };
  try {
    const parsed: unknown = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { value: null, error: 'Use um objeto JSON, ex.: {"nome": "Maria"}', count: 0 };
    }
    const value = parsed as Record<string, unknown>;
    return { value, error: null, count: Object.keys(value).length };
  } catch (err) {
    return { value: null, error: `JSON inválido: ${(err as Error).message}`, count: 0 };
  }
}
