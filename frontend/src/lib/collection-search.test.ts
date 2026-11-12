/** Leitura defensiva de `collection.search`. Roda com `npm test`. */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { SEARCH_DEFAULTS, collectionSearch } from "./collection-search.ts";

describe("collectionSearch", () => {
  it("sem `search` (serviço antigo) devolve null em vez de quebrar", () => {
    assert.equal(collectionSearch({}), null);
    assert.equal(collectionSearch({ search: null }), null);
    assert.equal(collectionSearch(undefined), null);
  });

  it("completa os campos que faltaram com os padrões do serviço", () => {
    assert.deepEqual(collectionSearch({ search: {} }), SEARCH_DEFAULTS);
    const out = collectionSearch({ search: { search_mode: "hybrid", vector_weight: 0.7 } });
    assert.equal(out?.search_mode, "hybrid");
    assert.equal(out?.vector_weight, 0.7);
    assert.equal(out?.score_kind, "hybrid");
    assert.equal(out?.max_results, SEARCH_DEFAULTS.max_results);
  });

  it("descarta valores fora do vocabulário", () => {
    const out = collectionSearch({ search: { search_mode: "bm25", score_kind: "x", max_results: "8" } });
    assert.equal(out?.search_mode, "vector");
    assert.equal(out?.score_kind, "similarity");
    assert.equal(out?.max_results, SEARCH_DEFAULTS.max_results);
  });

  it("mantém o que veio certo", () => {
    const search = {
      search_mode: "hybrid",
      language: "english",
      max_results: 8,
      vector_weight: 0.6,
      reranker: null,
      score_kind: "rerank",
    };
    assert.deepEqual(collectionSearch({ search }), search);
  });
});
