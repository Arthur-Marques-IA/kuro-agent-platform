"""Configuração de busca de uma collection: modo, idioma, k e reranker.

- `search_mode`: `vector` (padrão, o comportamento de sempre) ou `hybrid`, que
  soma a similaridade do vetor com o ranking do full-text do Postgres. É o
  `SearchType.hybrid` do `PgVector` do Agno: `score = w·vetor + (1−w)·texto`, com
  `w = vector_weight` (0–1, padrão 0,5), o vetor em `max(0, 1 − distância cosseno)`
  e o texto em `r/(r + 0,1)` sobre o `ts_rank_cd`.
- `language`: o dicionário do full-text (`portuguese`, `english`, `simple`...).
  O Agno 3.0.9 aceita direto (`PgVector(content_language=...)`), sem subclasse —
  vai como parâmetro do `to_tsvector`/`websearch_to_tsquery`. Duas ressalvas do
  Agno que explicam as escolhas daqui:
    * o `hybrid_search` dele não filtra por `@@` (está comentado), então pontua
      todas as linhas da tabela, com o `to_tsvector` calculado na hora: não há
      índice GIN em uso, e numa collection muito grande o híbrido custa um full scan;
    * o `_create_gin_index` dele interpola o idioma sem aspas (vira nome de
      coluna) — por isso não é chamado aqui.
  Mesmo sem índice, o idioma fica fixo depois da criação, como o embedder: é
  regra de produto. Trocar o dicionário muda quais trechos casam, e um índice
  GIN futuro (`to_tsvector('<idioma>', content)`) seria de um idioma só. Para
  trocar, crie outra collection. Collections anteriores a esta configuração têm
  `language` nulo, que é o `english` do Agno.
- `max_results`: quantos trechos a busca devolve (k), para o agente e para o
  `/search` da collection.
- `reranker`: só o gancho. Os rerankers do Agno precisam de dependência pesada
  (sentence-transformers/torch) ou de um serviço externo com chave (Cohere,
  Infinity, Bedrock); nenhum deles está no projeto. `RERANKERS` é onde um entra:
  o nome vira aceito no cadastro e `build_reranker` monta a instância.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import text

SEARCH_MODES = ("vector", "hybrid")
DEFAULT_MAX_RESULTS = 10
"""O `Knowledge.max_results` do Agno — o k de antes desta configuração."""
DEFAULT_VECTOR_WEIGHT = 0.5
DEFAULT_LANGUAGE = "portuguese"
"""O idioma de uma collection nova."""
LEGACY_LANGUAGE = "english"
"""O idioma das collections anteriores à coluna (`language` nulo): o padrão do Agno."""
MAX_RESULTS_LIMIT = 50

RERANKERS: dict[str, Callable[[], Any]] = {}
"""Rerankers aceitos: nome → fábrica da instância (`agno.knowledge.reranker.base.Reranker`). Vazio por ora."""

_LANGUAGE_RE = re.compile(r"^[a-z_]{2,40}$")


class SearchConfigError(ValueError):
    """Configuração de busca inválida — a API devolve como 422."""


@dataclass(frozen=True)
class SearchConfig:
    """A configuração efetiva (com os padrões resolvidos). Imutável e hashable:
    entra na chave do cache de `documents/collections.py`."""

    search_mode: str = "vector"
    language: str | None = None
    max_results: int = DEFAULT_MAX_RESULTS
    vector_weight: float = DEFAULT_VECTOR_WEIGHT
    reranker: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> "SearchConfig":
        row = row or {}
        weight = row.get("vector_weight")
        return cls(
            search_mode=row.get("search_mode") or "vector",
            language=row.get("language"),
            max_results=row.get("max_results") or DEFAULT_MAX_RESULTS,
            vector_weight=DEFAULT_VECTOR_WEIGHT if weight is None else float(weight),
            reranker=row.get("reranker"),
        )

    @property
    def content_language(self) -> str:
        return self.language or LEGACY_LANGUAGE

    @property
    def score_kind(self) -> str:
        """O que o `score` de um trecho mede — o trace e o console rotulam por isto."""
        if self.reranker:
            return "rerank"
        return "hybrid" if self.search_mode == "hybrid" else "similarity"

    def public(self) -> dict[str, Any]:
        """Como a API mostra (com os padrões resolvidos)."""
        return {
            "search_mode": self.search_mode,
            "language": self.content_language,
            "max_results": self.max_results,
            "vector_weight": self.vector_weight,
            "reranker": self.reranker,
            "score_kind": self.score_kind,
        }


def snapshot(row: dict[str, Any]) -> dict[str, Any]:
    """O que entra na versão da collection (`documents/versions.py`): embedder e busca.

    `vector_weight` só entra no modo híbrido (no vetorial não muda nada) e
    `reranker` só quando definido — assim as collections que já existiam mantêm
    o hash da v1 gravada pela migração 0012."""
    config = SearchConfig.from_row(row)
    snap: dict[str, Any] = {
        "embedder_provider": (row.get("embedder_provider") or "google").lower(),
        "embedder_model": row.get("embedder_model"),
        "embedder_dimensions": row.get("embedder_dimensions"),
        "search_mode": config.search_mode,
        "language": config.language,
        "max_results": config.max_results,
    }
    if config.search_mode == "hybrid":
        snap["vector_weight"] = config.vector_weight
    if config.reranker:
        snap["reranker"] = config.reranker
    return snap


def validate(values: dict[str, Any]) -> None:
    """Confere os campos de busca que vieram (só os presentes)."""
    mode = values.get("search_mode")
    if mode is not None and mode not in SEARCH_MODES:
        raise SearchConfigError(f"search_mode deve ser um de {', '.join(SEARCH_MODES)} (veio {mode!r})")
    k = values.get("max_results")
    if k is not None and not 1 <= k <= MAX_RESULTS_LIMIT:
        raise SearchConfigError(f"max_results deve estar entre 1 e {MAX_RESULTS_LIMIT} (veio {k})")
    weight = values.get("vector_weight")
    if weight is not None and not 0 <= weight <= 1:
        raise SearchConfigError(f"vector_weight deve estar entre 0 e 1 (veio {weight})")
    reranker = values.get("reranker")
    if reranker is not None and reranker not in RERANKERS:
        aceitos = ", ".join(RERANKERS) or "nenhum por enquanto — o gancho existe, nenhum reranker está instalado"
        raise SearchConfigError(f"reranker {reranker!r} não é suportado (aceitos: {aceitos})")


def validate_language(language: str, engine: Any) -> None:
    """O dicionário existe neste Postgres? O nome vai para o SQL do full-text,
    então o formato é conferido mesmo onde não dá para consultar `pg_ts_config`."""
    if not _LANGUAGE_RE.match(language):
        raise SearchConfigError(f"language deve ser o nome de um dicionário do Postgres (ex.: portuguese), veio {language!r}")
    if engine.dialect.name != "postgresql":
        return
    with engine.connect() as conn:
        found = conn.execute(text("SELECT 1 FROM pg_ts_config WHERE cfgname = :name"), {"name": language}).first()
    if found is None:
        raise SearchConfigError(
            f"language {language!r} não é um dicionário de full-text deste Postgres (veja SELECT cfgname FROM pg_ts_config)"
        )


def build_reranker(name: str | None) -> Any:
    if not name:
        return None
    factory = RERANKERS.get(name)
    if factory is None:
        raise SearchConfigError(f"reranker {name!r} não é suportado")
    return factory()
