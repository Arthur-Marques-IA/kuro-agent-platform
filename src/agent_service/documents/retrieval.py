"""Busca na base de conhecimento com rastro: quais trechos o agente leu.

O Agno dá ao agente a tool `search_knowledge_base` e devolve ao modelo os
trechos como `Document.to_dict()` — só `name`, `meta_data` e `content`. O
trace gravava esse texto cru como saída da tool, mas sem como voltar à origem:
o id do trecho, o documento (`content_id`) e a posição dele ficavam de fora.

`knowledge_retriever` é o gancho que o Agno consulta antes da busca padrão. O
retriever daqui faz a mesma busca (`Knowledge.aretrieve`, com o mesmo escopo de
usuário), devolve ao modelo o mesmo formato de antes — a resposta não muda — e
guarda à parte, num registro por run (mesmo padrão de `tools/failures.py`), a
versão completa de cada trecho. O trace (`observability/tracing.py`) junta esse
registro à span da busca em `metadata.references`.
"""

import asyncio
import logging
from contextvars import ContextVar
from typing import Any, Callable

from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge
from sqlalchemy import select

logger = logging.getLogger(__name__)

SEARCH_TOOL = "search_knowledge_base"

SNIPPET_META = ("chunk", "chunk_size", "page", "section", "source_type", "source_url", "page_title", "crawled_at")
"""Campos do `meta_data` do trecho que vão para a referência (o resto é ruído de ingestão)."""

_searches: ContextVar[list[dict[str, Any]] | None] = ContextVar("knowledge_searches", default=None)


def start_run() -> None:
    """Registro novo para o run que começa."""
    _searches.set([])


def record_search(entry: dict[str, Any]) -> None:
    registry = _searches.get()
    if registry is not None:
        registry.append(entry)


def take_search(query: Any) -> dict[str, Any] | None:
    """A busca feita com esta `query` — a mais antiga ainda não consumida.

    A span da tool só tem os argumentos e o texto devolvido; a query é o que
    liga os dois lados. Consumir (em vez de só ler) mantém certo o caso de o
    modelo repetir a mesma busca no mesmo run."""
    registry = _searches.get()
    if not registry:
        return None
    for i, entry in enumerate(registry):
        if entry["query"] == query:
            return registry.pop(i)
    return None


def _score(doc: Document) -> float | None:
    if doc.reranking_score is not None:
        return round(float(doc.reranking_score), 4)
    value = (doc.meta_data or {}).get("similarity_score")
    return round(float(value), 4) if isinstance(value, (int, float)) else None


def _content_ids(knowledge: Knowledge, chunk_ids: list[str]) -> dict[str, str]:
    """`content_id` de cada trecho — o PgVector não devolve essa coluna na busca."""
    vector_db = knowledge.vector_db
    table = getattr(vector_db, "table", None)
    session = getattr(vector_db, "Session", None)
    if not chunk_ids or table is None or session is None:
        return {}
    with session() as sess:
        rows = sess.execute(select(table.c.id, table.c.content_id).where(table.c.id.in_(chunk_ids))).fetchall()
    return {row.id: row.content_id for row in rows if row.content_id}


def reference(doc: Document, *, rank: int, content_id: str | None = None) -> dict[str, Any]:
    """Um trecho consultado, como o trace e a API mostram."""
    meta = doc.meta_data or {}
    return {
        "rank": rank,
        "score": _score(doc),
        "document": doc.name,
        "content_id": content_id or doc.content_id,
        "chunk_id": doc.id,
        **{key: meta[key] for key in SNIPPET_META if meta.get(key) is not None},
        "content": doc.content,
    }


async def describe_hits(knowledge: Knowledge, documents: list[Document]) -> list[dict[str, Any]]:
    """As referências de uma lista de resultados (também usado pelo `/search` da collection)."""
    ids = [d.id for d in documents if d.id]
    try:
        content_ids = await asyncio.to_thread(_content_ids, knowledge, ids)
    except Exception:
        # Sem o content_id a referência ainda serve (texto, documento, score):
        # não vale derrubar a busca do agente por causa do rastro.
        logger.warning("Não consegui ler o content_id dos trechos", exc_info=True)
        content_ids = {}
    return [reference(doc, rank=i, content_id=content_ids.get(doc.id or "")) for i, doc in enumerate(documents, 1)]


def make_retriever(collection_name: str, knowledge: Knowledge) -> Callable[..., Any]:
    """O `knowledge_retriever` de um agente com `knowledge_collection`.

    O Agno chama o mesmo retriever nos dois caminhos: no `arun` (o do serviço) ele
    aguarda o que voltar; num `run` síncrono, não. Por isso o retriever devolve a
    corrotina quando há event loop nesta thread e roda ela até o fim quando não há."""

    async def aretrieve(query: str, num_documents: int | None, filters: Any, run_context: Any) -> list[dict[str, Any]] | None:
        user_id = getattr(run_context, "user_id", None)
        documents = await knowledge.aretrieve(
            query=query, max_results=num_documents, filters=filters, user_id=user_id
        )
        record_search(
            {
                "query": query,
                "collection": collection_name,
                "results": await describe_hits(knowledge, documents or []),
            }
        )
        if not documents:
            return None
        # O mesmo formato que o Agno mandaria ao modelo sem retriever próprio.
        return [doc.to_dict() for doc in documents]

    def retriever(
        query: str,
        num_documents: int | None = None,
        filters: Any = None,
        run_context: Any = None,
        **_: Any,
    ) -> Any:
        coro = aretrieve(query, num_documents, filters, run_context)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        return coro

    return retriever


def sources_from(searches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """As fontes de um run, para a resposta da API: um item por documento (ou página).

    `searches` são as buscas que o run fez (o `metadata.references` de cada span de
    busca). Trechos do mesmo documento viram uma fonte só — a chave é o
    `content_id`, depois a `source_url`, depois o nome —, com o melhor score e os
    trechos usados na ordem em que apareceram. O texto dos trechos fica de fora:
    está no trace (`/runs/{run_id}`)."""
    by_key: dict[str, dict[str, Any]] = {}
    for search in searches:
        for hit in search.get("results") or []:
            key = hit.get("content_id") or hit.get("source_url") or hit.get("document")
            if not key:
                continue
            source = by_key.get(key)
            if source is None:
                source = by_key[key] = {
                    "title": hit.get("page_title") or hit.get("document"),
                    "document": hit.get("document"),
                    "source_url": hit.get("source_url"),
                    "content_id": hit.get("content_id"),
                    "collection": search.get("collection"),
                    "score": hit.get("score"),
                    "chunk_ids": [],
                }
            score = hit.get("score")
            if score is not None and (source["score"] is None or score > source["score"]):
                source["score"] = score
            chunk_id = hit.get("chunk_id")
            if chunk_id and chunk_id not in source["chunk_ids"]:
                source["chunk_ids"].append(chunk_id)
    # Mais relevante primeiro; sem score (busca sem similaridade) vai para o fim.
    return sorted(by_key.values(), key=lambda s: -s["score"] if s["score"] is not None else float("inf"))
