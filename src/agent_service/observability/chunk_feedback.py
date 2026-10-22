"""Feedback do operador por trecho recuperado pelo RAG (tabela `chunk_feedback`).

No trace de um run, cada trecho que a busca devolveu (`metadata.references` da span
`search_knowledge_base`) pode ser marcado `relevant`, `irrelevant` ou `wrong`
(o conteúdo do documento está errado ou desatualizado). O Kuro só registra e aponta:
nenhuma marcação esconde ou despublica documento — quem decide é o operador.

Cada marcação guarda a revisão do documento em que o trecho foi visto
(`documents/revisions.py`) e um estado: `open` até alguém corrigir o documento
(`resolved`, com a revisão que corrigiu) ou dizer que ela não procedia
(`dismissed`). Remarcar um trecho abre de novo; uma marcação nova sobre a revisão
corrigida é um problema novo, e aparece aberta.

As marcações `relevant` são o gabarito de um eval de RAG (pergunta → trechos e
documentos relevantes): `export_relevant` as agrupa por pergunta, para quem quiser
guardar o dataset ou só subir para uma comparação.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, delete, func, select, true, update

from agent_service.db import get_db
from agent_service.documents import revisions
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
    Column("document_revision", Integer, nullable=True),
    Column("status", String, nullable=False, server_default="open"),
    Column("resolved_in_revision", Integer, nullable=True),
    Column("resolved_by", String, nullable=True),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("resolution_note", Text, nullable=True),
)

Status = Literal["open", "resolved", "dismissed"]
STATUSES: tuple[str, ...] = ("open", "resolved", "dismissed")


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
        run = conn.execute(select(runs.c.agent_type, runs.c.started_at).where(runs.c.run_id == run_id)).first()
        if run is None:
            raise RunNotFoundError(run_id)
        agent_type = run.agent_type
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
            "document_revision": hit.get(revisions.FIELD),
            "status": "open",
        }
        if values["document_revision"] is None and values["content_id"] and run.started_at:
            # Trecho indexado antes de a revisão ir na metadata: a que estava no ar no run.
            values["document_revision"] = revisions.revision_at(values["content_id"], run.started_at)
        conn.execute(chunk_feedback.insert().values(**values))
    return _with_state(values)


def _with_state(row: dict[str, Any], current: int | None = None) -> dict[str, Any]:
    """A marcação com `current_revision` e `stale` (aberta, mas vista numa revisão
    que já foi substituída — o problema pode já ter sido corrigido)."""
    if current is None and row.get("content_id"):
        latest = revisions.latest(row["content_id"])
        current = latest["revision"] if latest else None
    seen = row.get("document_revision")
    row["current_revision"] = current
    row["stale"] = bool(row.get("status") == "open" and seen is not None and current is not None and seen < current)
    return row


def resolve(
    feedback_ids: list[str],
    *,
    content_id: str,
    revision: int,
    by: str | None = None,
    note: str | None = None,
) -> int:
    """Resolve, na revisão `revision`, as marcações abertas destes ids que são do
    documento `content_id` (as de outro documento ficam como estão). Devolve quantas."""
    if not feedback_ids:
        return 0
    with get_db().db_engine.begin() as conn:
        return conn.execute(
            update(chunk_feedback)
            .where(
                chunk_feedback.c.id.in_(feedback_ids),
                chunk_feedback.c.content_id == content_id,
                chunk_feedback.c.status == "open",
            )
            .values(
                status="resolved",
                resolved_in_revision=revision,
                resolved_by=by or None,
                resolved_at=datetime.now(timezone.utc),
                resolution_note=note or None,
            )
        ).rowcount


def set_status(
    feedback_id: str, status: Status, *, by: str | None = None, note: str | None = None
) -> dict[str, Any] | None:
    """Dispensa (`dismissed`), reabre (`open`) ou resolve sem editar (`resolved`) uma marcação."""
    closing = status != "open"
    with get_db().db_engine.begin() as conn:
        row = conn.execute(select(chunk_feedback).where(chunk_feedback.c.id == feedback_id)).first()
        if row is None:
            return None
        resolved_in = None
        if status == "resolved" and row.content_id:
            latest = revisions.latest(row.content_id)
            resolved_in = latest["revision"] if latest else None
        conn.execute(
            update(chunk_feedback)
            .where(chunk_feedback.c.id == feedback_id)
            .values(
                status=status,
                resolved_in_revision=resolved_in,
                resolved_by=(by or None) if closing else None,
                resolved_at=datetime.now(timezone.utc) if closing else None,
                resolution_note=(note or None) if closing else None,
            )
        )
        row = conn.execute(select(chunk_feedback).where(chunk_feedback.c.id == feedback_id)).first()
    return _with_state(_row(row))


def close_for_document(content_id: str, *, status: Status, note: str) -> int:
    """Fecha todas as marcações abertas de um documento (ex.: ele foi removido)."""
    with get_db().db_engine.begin() as conn:
        return conn.execute(
            update(chunk_feedback)
            .where(chunk_feedback.c.content_id == content_id, chunk_feedback.c.status == "open")
            .values(status=status, resolved_at=datetime.now(timezone.utc), resolution_note=note)
        ).rowcount


def remove(feedback_id: str) -> bool:
    with get_db().db_engine.begin() as conn:
        return conn.execute(delete(chunk_feedback).where(chunk_feedback.c.id == feedback_id)).rowcount > 0


def list_feedback(
    *,
    run_id: str | None = None,
    collection: str | None = None,
    label: str | None = None,
    content_id: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    stmt = select(chunk_feedback)
    if status:
        stmt = stmt.where(chunk_feedback.c.status == status)
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
    currents: dict[str, int | None] = {}
    out = []
    for r in rows:
        row = _row(r)
        cid = row.get("content_id")
        if cid and cid not in currents:
            latest = revisions.latest(cid)
            currents[cid] = latest["revision"] if latest else None
        out.append(_with_state(row, currents.get(cid) if cid else None))
    return out


def flagged_documents(
    *, collection: str | None = None, label: str = "wrong", status: str | None = "open", limit: int = 20
) -> list[dict[str, Any]]:
    """Documentos com mais marcações `label` (padrão `wrong`) no estado `status`
    (padrão as abertas; `None` conta todas), com os runs que as geraram."""
    state = (chunk_feedback.c.status == status) if status else true()
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
        .where(chunk_feedback.c.label == label, key.is_not(None), state)
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
                select(
                    chunk_feedback.c.id,
                    chunk_feedback.c.run_id,
                    chunk_feedback.c.query,
                    chunk_feedback.c.comment,
                    chunk_feedback.c.document_revision,
                )
                .where(chunk_feedback.c.label == label, key == group["key"], state)
                .where(chunk_feedback.c.collection.is_(None) if group["collection"] is None
                       else chunk_feedback.c.collection == group["collection"])
                .order_by(chunk_feedback.c.created_at.desc())
                .limit(20)
            ).all()
            group["run_ids"] = list(dict.fromkeys(m.run_id for m in marks))
            group["comments"] = [m.comment for m in marks if m.comment][:5]
            group["feedback_ids"] = [m.id for m in marks]
            latest = revisions.latest(group["content_id"]) if group["content_id"] else None
            group["current_revision"] = latest["revision"] if latest else None
            seen = [m.document_revision for m in marks if m.document_revision is not None]
            group["stale"] = bool(seen and group["current_revision"] and max(seen) < group["current_revision"])
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
