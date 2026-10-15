"""Cadastro das fontes de uma collection (sites) e das páginas indexadas de cada uma.

O schema é criado pelo Alembic (`migrations/versions/0008_knowledge_sources.py` e
`0009_knowledge_sync_costs.py`);
quem coleta e indexa é `documents/web_sync.py`.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)

from agent_service.db import get_db

metadata = MetaData()

knowledge_sources = Table(
    "knowledge_sources",
    metadata,
    Column("id", String, primary_key=True),
    Column("collection", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("url", String, nullable=False),
    Column("label", String, nullable=True),
    Column("mode", String, nullable=False),
    Column("options", JSON, nullable=False),
    Column("refresh", String, nullable=False),
    Column("status", String, nullable=False),
    Column("status_message", Text, nullable=True),
    Column("progress", JSON, nullable=True),
    Column("pages_indexed", Integer, nullable=False),
    Column("pages_failed", Integer, nullable=False),
    Column("last_synced_at", DateTime(timezone=True), nullable=True),
    Column("next_sync_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

knowledge_source_pages = Table(
    "knowledge_source_pages",
    metadata,
    Column("source_id", String, primary_key=True),
    Column("url", String, primary_key=True),
    Column("content_id", String, nullable=True),
    Column("title", String, nullable=True),
    Column("content_hash", String, nullable=True),
    Column("chunks", Integer, nullable=False),
    Column("formatted", String, nullable=True),
    Column("status", String, nullable=False),
    Column("error", Text, nullable=True),
    Column("fetched_at", DateTime(timezone=True), nullable=True),
)

knowledge_sync_costs = Table(
    "knowledge_sync_costs",
    metadata,
    Column("id", String, primary_key=True),
    Column("source_id", String, nullable=False),
    Column("collection", String, nullable=False),
    Column("model", String, nullable=True),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Float, nullable=True),
    Column("ended_at", DateTime(timezone=True), nullable=False),
)
"""O que o formatador gastou em cada sincronização. Fica mesmo depois de a fonte
ser removida: é dinheiro que já saiu, e os Logs somam isso ao custo do período."""

REFRESH_INTERVALS: dict[str, timedelta | None] = {
    "never": None,
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),
}

HEARTBEAT_SECONDS = 30
"""De quanto em quanto tempo um sync em andamento renova `updated_at`."""
STALE_RUNNING = timedelta(minutes=2)
"""Um sync `running` sem sinal de vida há mais que isto morreu com o processo
(restart no meio). Pelo heartbeat, e não por "estava rodando no boot": com mais de
uma réplica, quem sobe não pode derrubar o sync que outra está fazendo."""


class SourceNotFoundError(LookupError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def next_sync(refresh: str, after: datetime | None = None) -> datetime | None:
    interval = REFRESH_INTERVALS.get(refresh)
    return (after or _now()) + interval if interval else None


def _engine():
    return get_db().db_engine


def _row(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def create_source(
    *, collection: str, url: str, mode: str, options: dict[str, Any], refresh: str, label: str | None = None
) -> dict[str, Any]:
    source_id = uuid4().hex[:12]
    with _engine().begin() as conn:
        conn.execute(
            insert(knowledge_sources).values(
                id=source_id,
                collection=collection,
                kind="web",
                url=url,
                label=label,
                mode=mode,
                options=options,
                refresh=refresh,
                status="pending",
                pages_indexed=0,
                pages_failed=0,
            )
        )
    return get_source(source_id)  # type: ignore[return-value]


def find_source(collection: str, url: str, mode: str) -> dict[str, Any] | None:
    with _engine().begin() as conn:
        row = conn.execute(
            select(knowledge_sources).where(
                knowledge_sources.c.collection == collection,
                knowledge_sources.c.url == url,
                knowledge_sources.c.mode == mode,
            )
        ).fetchone()
    return _row(row) if row else None


def get_source(source_id: str) -> dict[str, Any] | None:
    with _engine().begin() as conn:
        row = conn.execute(select(knowledge_sources).where(knowledge_sources.c.id == source_id)).fetchone()
    return _row(row) if row else None


def list_sources(collection: str | None = None) -> list[dict[str, Any]]:
    stmt = select(knowledge_sources).order_by(knowledge_sources.c.created_at)
    if collection is not None:
        stmt = stmt.where(knowledge_sources.c.collection == collection)
    with _engine().begin() as conn:
        return [_row(r) for r in conn.execute(stmt).fetchall()]


def update_source(source_id: str, **values: Any) -> dict[str, Any]:
    with _engine().begin() as conn:
        conn.execute(
            update(knowledge_sources).where(knowledge_sources.c.id == source_id).values(**values, updated_at=_now())
        )
    row = get_source(source_id)
    if row is None:
        raise SourceNotFoundError(source_id)
    return row


def claim_source(source_id: str) -> bool:
    """Marca a fonte como `running` se ninguém estiver sincronizando. É a trava:
    duas chamadas ao mesmo tempo (ou o agendador e um clique) não rodam juntas."""
    stale = _now() - STALE_RUNNING
    with _engine().begin() as conn:
        result = conn.execute(
            update(knowledge_sources)
            .where(
                knowledge_sources.c.id == source_id,
                or_(knowledge_sources.c.status != "running", knowledge_sources.c.updated_at < stale),
            )
            .values(status="running", status_message=None, progress={"discovered": 0, "done": 0}, updated_at=_now())
        )
    return result.rowcount == 1


def due_sources() -> list[dict[str, Any]]:
    """Fontes agendadas cujo próximo sync já venceu."""
    now = _now()
    with _engine().begin() as conn:
        rows = conn.execute(
            select(knowledge_sources).where(
                knowledge_sources.c.refresh != "never",
                knowledge_sources.c.next_sync_at.is_not(None),
                knowledge_sources.c.next_sync_at <= now,
                or_(knowledge_sources.c.status != "running", knowledge_sources.c.updated_at < now - STALE_RUNNING),
            )
        ).fetchall()
    return [_row(r) for r in rows]


def heartbeat(source_id: str) -> None:
    with _engine().begin() as conn:
        conn.execute(
            update(knowledge_sources)
            .where(knowledge_sources.c.id == source_id, knowledge_sources.c.status == "running")
            .values(updated_at=_now())
        )


def fail_stale_running() -> int:
    """Um sync `running` sem heartbeat não vai terminar sozinho: o processo que o
    fazia caiu. Marca como falha, para a tela não mostrar "sincronizando" para sempre."""
    with _engine().begin() as conn:
        result = conn.execute(
            update(knowledge_sources)
            .where(knowledge_sources.c.status == "running", knowledge_sources.c.updated_at < _now() - STALE_RUNNING)
            .values(status="failed", status_message="Sincronização interrompida (o serviço reiniciou).", updated_at=_now())
        )
    return result.rowcount


def delete_source(source_id: str) -> None:
    with _engine().begin() as conn:
        conn.execute(delete(knowledge_source_pages).where(knowledge_source_pages.c.source_id == source_id))
        conn.execute(delete(knowledge_sources).where(knowledge_sources.c.id == source_id))


# -- custo do formatador -----------------------------------------------------------


def record_sync_cost(
    *, source_id: str, collection: str, model: str | None, input_tokens: int, output_tokens: int,
    cost_usd: float | None, ended_at: datetime | None = None,
) -> None:
    with _engine().begin() as conn:
        conn.execute(
            insert(knowledge_sync_costs).values(
                id=uuid4().hex,
                source_id=source_id,
                collection=collection,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                ended_at=ended_at or _now(),
            )
        )


def list_sync_costs(since: datetime, until: datetime) -> list[dict[str, Any]]:
    """As sincronizações que terminaram em `[since, until)`."""
    c = knowledge_sync_costs.c
    with _engine().connect() as conn:
        rows = conn.execute(select(knowledge_sync_costs).where(c.ended_at >= since, c.ended_at < until))
        return [_row(r) for r in rows]


# -- páginas -----------------------------------------------------------------------


def list_pages(source_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
    stmt = select(knowledge_source_pages).where(knowledge_source_pages.c.source_id == source_id)
    if status is not None:
        stmt = stmt.where(knowledge_source_pages.c.status == status)
    with _engine().begin() as conn:
        return [_row(r) for r in conn.execute(stmt.order_by(knowledge_source_pages.c.url)).fetchall()]


def upsert_page(source_id: str, url: str, **values: Any) -> None:
    key = and_(knowledge_source_pages.c.source_id == source_id, knowledge_source_pages.c.url == url)
    with _engine().begin() as conn:
        exists = conn.execute(select(knowledge_source_pages.c.url).where(key)).fetchone()
        if exists:
            conn.execute(update(knowledge_source_pages).where(key).values(**values))
        else:
            conn.execute(
                insert(knowledge_source_pages).values(
                    source_id=source_id, url=url, **{"chunks": 0, "status": "pending", **values}
                )
            )


def delete_page(source_id: str, url: str) -> None:
    with _engine().begin() as conn:
        conn.execute(
            delete(knowledge_source_pages).where(
                knowledge_source_pages.c.source_id == source_id, knowledge_source_pages.c.url == url
            )
        )


def source_of_content(content_id: str) -> dict[str, Any] | None:
    """A página de site que gerou este documento, se gerou — o console usa para
    dizer que remover o documento à mão não impede o próximo sync de trazê-lo de volta."""
    with _engine().begin() as conn:
        row = conn.execute(
            select(knowledge_source_pages).where(knowledge_source_pages.c.content_id == content_id)
        ).fetchone()
    return _row(row) if row else None
