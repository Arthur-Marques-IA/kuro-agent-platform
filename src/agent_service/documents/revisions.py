"""Revisões de um documento de collection (tabela `document_revisions`).

O `content_id` de um documento fica o mesmo quando ele é corrigido
(`collections.replace_document`, ou o sync de um site que mudou): cada versão do
conteúdo vira uma revisão numerada, com o texto quando é texto. A revisão vai na
metadata de cada trecho (`document_revision`), então o trace e a marcação de um
trecho dizem qual versão do documento o agente leu — e a marcação resolvida diz
em qual revisão foi corrigida (`observability/chunk_feedback.py`).

Documento indexado antes de existir esta tabela não tem linha nenhuma: ele ganha
a revisão 1, sem data, na primeira vez que alguém pergunta (`current`).
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, func, insert, select
from sqlalchemy.exc import IntegrityError

from agent_service.db import get_db

FIELD = "document_revision"
"""Chave da revisão na metadata do documento e de cada trecho dele."""

metadata = MetaData()

document_revisions = Table(
    "document_revisions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("collection", String, nullable=False),
    Column("content_id", String, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("content_hash", String, nullable=True),
    Column("text", Text, nullable=True),
    Column("filename", String, nullable=True),
    Column("note", Text, nullable=True),
    Column("created_by", String, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=True),
)


def _row(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def latest(content_id: str) -> dict[str, Any] | None:
    with get_db().db_engine.connect() as conn:
        row = conn.execute(
            select(document_revisions)
            .where(document_revisions.c.content_id == content_id)
            .order_by(document_revisions.c.revision.desc())
            .limit(1)
        ).first()
    return _row(row) if row else None


def current(collection: str, content_id: str) -> int:
    """Número da revisão atual — 1 (criada agora, sem data) para documento sem histórico."""
    found = latest(content_id)
    if found is not None:
        return found["revision"]
    try:
        with get_db().db_engine.begin() as conn:
            conn.execute(insert(document_revisions).values(collection=collection, content_id=content_id, revision=1))
    except IntegrityError:
        pass  # outro processo criou ao mesmo tempo
    return 1


def record(
    collection: str,
    content_id: str,
    *,
    revision: int,
    content_hash: str | None = None,
    text: str | None = None,
    filename: str | None = None,
    note: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Grava a revisão `revision` (idempotente: a mesma revisão de novo não duplica)."""
    values = {
        "collection": collection,
        "content_id": content_id,
        "revision": revision,
        "content_hash": content_hash,
        "text": text,
        "filename": filename,
        "note": note or None,
        "created_by": created_by or None,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        with get_db().db_engine.begin() as conn:
            conn.execute(insert(document_revisions).values(**values))
    except IntegrityError:
        pass
    return values


def next_number(content_id: str) -> int:
    with get_db().db_engine.connect() as conn:
        top = conn.execute(
            select(func.max(document_revisions.c.revision)).where(document_revisions.c.content_id == content_id)
        ).scalar()
    return (top or 1) + 1


def list_revisions(content_id: str) -> list[dict[str, Any]]:
    """Mais recente primeiro, sem o texto (`get_revision` traz o texto)."""
    columns = [c for c in document_revisions.c if c.name != "text"]
    with get_db().db_engine.connect() as conn:
        rows = conn.execute(
            select(*columns)
            .where(document_revisions.c.content_id == content_id)
            .order_by(document_revisions.c.revision.desc())
        ).all()
    return [_row(r) for r in rows]


def get_revision(content_id: str, revision: int) -> dict[str, Any] | None:
    with get_db().db_engine.connect() as conn:
        row = conn.execute(
            select(document_revisions).where(
                document_revisions.c.content_id == content_id, document_revisions.c.revision == revision
            )
        ).first()
    return _row(row) if row else None


def revision_at(content_id: str, when: datetime) -> int | None:
    """A revisão que estava no ar em `when` — para trechos indexados antes de a
    revisão ir na metadata. Revisão sem data (a 1 de antes da tabela) vale desde sempre."""
    with get_db().db_engine.connect() as conn:
        return conn.execute(
            select(func.max(document_revisions.c.revision)).where(
                document_revisions.c.content_id == content_id,
                (document_revisions.c.created_at.is_(None)) | (document_revisions.c.created_at <= when),
            )
        ).scalar()
