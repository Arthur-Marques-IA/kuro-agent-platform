"""Feedback do operador por trecho recuperado pelo RAG (tabela `chunk_feedback`).

No trace de um run, cada trecho que a busca devolveu (`metadata.references` da span
`search_knowledge_base`) pode ser marcado `relevant`, `irrelevant` ou `wrong`
(o conteúdo do documento está errado ou desatualizado). O Kuro só registra e aponta:
nenhuma marcação esconde ou despublica documento — quem decide é o operador.

As marcações `relevant` são o gabarito de um eval de RAG (pergunta → trechos e
documentos relevantes): `export_relevant` as agrupa por pergunta, para quem quiser
guardar o dataset ou só subir para uma comparação.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, delete, func, select

from agent_service.db import get_db
from agent_service.observability.run_store import RunNotFoundError, run_spans, runs

Label = Literal["relevant", "irrelevant", "wrong"]
LABELS: tuple[str, ...] = ("relevant", "irrelevant", "wrong")

metadata = MetaData()

chunk_feedback = Table(
    "chunk_feedback",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, nullable=False),
    Column("span_id", String, nullable=False),
    Column("agent_type", String, nullable=True),
    Column("query", Text, nullable=False),
    Column("collection", String, nullable=True),
    Column("content_id", String, nullable=True),
    Column("chunk_id", String, nullable=False),
    Column("document", String, nullable=True),
    Column("rank", Integer, nullable=True),
    Column("label", String, nullable=False),
    Column("comment", Text, nullable=True),
    Column("created_by", String, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class ChunkNotFoundError(LookupError):
    """O trecho não aparece em nenhuma busca do run (ou na span indicada)."""


def _row(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _find_chunk(conn: Any, run_id: str, chunk_id: str, span_id: str | None) -> tuple[str, dict, dict]:
    """A span e a busca em que o trecho apareceu, e o próprio trecho."""
    stmt = select(run_spans.c.id, run_spans.c.metadata).where(run_spans.c.run_id == run_id)
    if span_id:
        stmt = stmt.where(run_spans.c.id == span_id)
    for sid, meta in conn.execute(stmt.order_by(run_spans.c.started_at)).all():
        search = (meta or {}).get("references")
        if not isinstance(search, dict):
            continue
        for hit in search.get("results") or []:
            if hit.get("chunk_id") == chunk_id:
                return sid, search, hit
    raise ChunkNotFoundError(chunk_id)


def mark(
    run_id: str,
    chunk_id: str,
    label: Label,
    *,
    span_id: str | None = None,
    comment: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Grava a marcação de um trecho (substitui a anterior do mesmo trecho na mesma busca).

    Sem `span_id`, vale a primeira busca do run que devolveu o trecho."""
    with get_db().db_engine.begin() as conn:
        agent_type = conn.execute(select(runs.c.agent_type).where(runs.c.run_id == run_id)).scalar()
        if agent_type is None:
            raise RunNotFoundError(run_id)
        sid, search, hit = _find_chunk(conn, run_id, chunk_id, span_id)
        conn.execute(
            delete(chunk_feedback).where(chunk_feedback.c.span_id == sid, chunk_feedback.c.chunk_id == chunk_id)
        )
        values = {
            "id": uuid.uuid4().hex,
            "run_id": run_id,
            "span_id": sid,
            "agent_type": agent_type,
            "query": str(search.get("query") or ""),
            "collection": search.get("collection"),
            "content_id": hit.get("content_id"),
            "chunk_id": chunk_id,
            "document": hit.get("document"),
            "rank": hit.get("rank"),
            "label": label,
            "comment": comment or None,
            "created_by": created_by or None,
            "created_at": datetime.now(timezone.utc),
        }
        conn.execute(chunk_feedback.insert().values(**values))
    return values


def remove(feedback_id: str) -> bool:
    with get_db().db_engine.begin() as conn:
        return conn.execute(delete(chunk_feedback).where(chunk_feedback.c.id == feedback_id)).rowcount > 0


def list_feedback(
    *,
    run_id: str | None = None,
    collection: str | None = None,
    label: str | None = None,
    content_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    stmt = select(chunk_feedback)
    if run_id:
        stmt = stmt.where(chunk_feedback.c.run_id == run_id)
    if collection:
        stmt = stmt.where(chunk_feedback.c.collection == collection)
    if label:
        stmt = stmt.where(chunk_feedback.c.label == label)
    if content_id:
        stmt = stmt.where(chunk_feedback.c.content_id == content_id)
    with get_db().db_engine.connect() as conn:
        rows = conn.execute(stmt.order_by(chunk_feedback.c.created_at.desc()).limit(limit)).all()
    return [_row(r) for r in rows]


def flagged_documents(*, collection: str | None = None, label: str = "wrong", limit: int = 20) -> list[dict[str, Any]]:
    """Documentos com mais marcações `label` (padrão `wrong`), com os runs que as geraram."""
    key = func.coalesce(chunk_feedback.c.content_id, chunk_feedback.c.document)
    stmt = (
        select(
            key.label("key"),
            func.max(chunk_feedback.c.content_id).label("content_id"),
            func.max(chunk_feedback.c.document).label("document"),
            chunk_feedback.c.collection,
            func.count().label("count"),
            func.max(chunk_feedback.c.created_at).label("last_at"),
        )
        .where(chunk_feedback.c.label == label, key.is_not(None))
        .group_by(key, chunk_feedback.c.collection)
        .order_by(func.count().desc(), func.max(chunk_feedback.c.created_at).desc())
        .limit(limit)
    )
    if collection:
        stmt = stmt.where(chunk_feedback.c.collection == collection)
    with get_db().db_engine.connect() as conn:
        groups = [_row(r) for r in conn.execute(stmt).all()]
        for group in groups:
            marks = conn.execute(
                select(chunk_feedback.c.run_id, chunk_feedback.c.query, chunk_feedback.c.comment)
                .where(chunk_feedback.c.label == label, key == group["key"])
                .where(chunk_feedback.c.collection.is_(None) if group["collection"] is None
                       else chunk_feedback.c.collection == group["collection"])
                .order_by(chunk_feedback.c.created_at.desc())
                .limit(20)
            ).all()
            group["run_ids"] = list(dict.fromkeys(m.run_id for m in marks))
            group["comments"] = [m.comment for m in marks if m.comment][:5]
            group.pop("key")
    return groups


def export_relevant(
    *,
    collection: str | None = None,
    agent_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """Gabarito de RAG: uma linha por (pergunta, collection), com os trechos e
    documentos marcados `relevant` e os runs de onde vieram."""
    stmt = select(chunk_feedback).where(chunk_feedback.c.label == "relevant")
    if collection:
        stmt = stmt.where(chunk_feedback.c.collection == collection)
    if agent_type:
        stmt = stmt.where(chunk_feedback.c.agent_type == agent_type)
    if since:
        stmt = stmt.where(chunk_feedback.c.created_at >= since)
    if until:
        stmt = stmt.where(chunk_feedback.c.created_at < until)
    with get_db().db_engine.connect() as conn:
        rows = conn.execute(stmt.order_by(chunk_feedback.c.created_at)).all()
    cases: dict[tuple[str, str | None], dict[str, Any]] = {}
    for row in rows:
        case = cases.setdefault(
            (row.query, row.collection),
            {
                "query": row.query,
                "collection": row.collection,
                "relevant_chunk_ids": [],
                "relevant_content_ids": [],
                "run_ids": [],
            },
        )
        for field, value in (
            ("relevant_chunk_ids", row.chunk_id),
            ("relevant_content_ids", row.content_id),
            ("run_ids", row.run_id),
        ):
            if value and value not in case[field]:
                case[field].append(value)
    return list(cases.values())
