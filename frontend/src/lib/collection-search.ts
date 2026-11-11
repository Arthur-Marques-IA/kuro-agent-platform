/**
 * Leitura defensiva da configuração de busca de uma collection (`collection.search`).
 *
 * Serviços anteriores à configuração de busca por collection (migração 0012) não devolvem
 * `search`, e um serviço intermediário pode devolver só parte dos campos. Em vez de quebrar a
 * tela (`Cannot read properties of undefined (reading 'search_mode')`), quem desenha recebe
 * `null` — o objeto inteiro faltou: o serviço é mais antigo que o console — ou o objeto com os
 * padrões do serviço nos campos que faltaram. Roda com `npm test`.
 */
import type { CollectionSearchConfig, ScoreKind, SearchMode } from "./types";

/** Os mesmos padrões do serviço (`src/agent_service/documents/search.py`). */
export const SEARCH_DEFAULTS: CollectionSearchConfig = {
  search_mode: "vector",
  language: "portuguese",
  max_results: 10,
  vector_weight: 0.5,
  reranker: null,
  score_kind: "similarity",
};

const MODES: readonly SearchMode[] = ["vector", "hybrid"];
const SCORE_KINDS: readonly ScoreKind[] = ["similarity", "hybrid", "rerank"];

function finite(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function collectionSearch(collection: { search?: unknown } | null | undefined): CollectionSearchConfig | null {
  const raw = collection?.search;
  if (!raw || typeof raw !== "object") return null;
  const s = raw as Record<string, unknown>;
  const mode = MODES.includes(s.search_mode as SearchMode) ? (s.search_mode as SearchMode) : SEARCH_DEFAULTS.search_mode;
  const scoreKind = SCORE_KINDS.includes(s.score_kind as ScoreKind)
    ? (s.score_kind as ScoreKind)
    : mode === "hybrid"
      ? "hybrid"
      : SEARCH_DEFAULTS.score_kind;
  return {
    search_mode: mode,
    language: typeof s.language === "string" && s.language ? s.language : SEARCH_DEFAULTS.language,
    max_results: finite(s.max_results, SEARCH_DEFAULTS.max_results),
    vector_weight: finite(s.vector_weight, SEARCH_DEFAULTS.vector_weight),
    reranker: typeof s.reranker === "string" ? s.reranker : null,
    score_kind: scoreKind,
  };
}
