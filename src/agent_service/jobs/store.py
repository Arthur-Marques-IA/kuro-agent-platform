"""A tabela `jobs` e as operações atômicas sobre ela.

O schema é criado pelo Alembic (`migrations/versions/0011_jobs.py`). Toda transição
de estado é um `UPDATE ... WHERE` com a condição que a torna válida (status, dono do
lease) e confere o `rowcount`: duas réplicas disputando o mesmo job, um lease que
venceu no meio da execução ou um cancelamento chegando junto com o fim nunca
resultam em duas escritas valendo.

Horários vão sempre como parâmetro (`_now()` do Python), nunca `now()` do SQL: no
SQLite dos testes o `now()` é texto e compararia errado.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError

from agent_service.config import get_settings
from agent_service.db import get_db

metadata = MetaData()

jobs = Table(
    "jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("run_after", DateTime(timezone=True), nullable=False),
    Column("locked_by", String, nullable=True),
    Column("locked_until", DateTime(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("dedupe_key", String, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Index("uq_jobs_dedupe_active", "dedupe_key", unique=True,
          postgresql_where=text("status IN ('queued', 'running')"),
          sqlite_where=text("status IN ('queued', 'running')")),
)

STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
ACTIVE = ("queued", "running")
NOTIFY_CHANNEL = "kuro_jobs"
MAX_BACKOFF = timedelta(hours=1)


class JobConflictError(Exception):
    """Já há um job ativo (`queued`/`running`) com a mesma `dedupe_key`."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _engine():
    return get_db().db_engine


def _row(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def backoff(attempts: int) -> timedelta:
    """Espera antes da próxima tentativa: base × 2^(tentativas−1), até 1 hora."""
    base = timedelta(seconds=max(get_settings().jobs_backoff_seconds, 0))
    return min(base * (2 ** max(attempts - 1, 0)), MAX_BACKOFF)


def enqueue(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Põe um job na fila. Com `dedupe_key` já ativa, `JobConflictError`.

    `conn` permite enfileirar na mesma transação de outra escrita: o job só existe
    se ela for confirmada."""
    now = _now()
    values = {
        "id": uuid4().hex,
        "kind": kind,
        "payload": payload or {},
        "status": "queued",
        "attempts": 0,
        "max_attempts": max_attempts or get_settings().jobs_max_attempts,
        "run_after": run_after or now,
        "dedupe_key": dedupe_key,
        "created_at": now,
        "updated_at": now,
    }
    if conn is None:
        try:
            with _engine().begin() as own:
                _insert(own, values)
        except IntegrityError as exc:
            raise JobConflictError(dedupe_key) from exc
    else:
        # Savepoint: o conflito não pode envenenar a transação de quem chamou.
        try:
            with conn.begin_nested():
                _insert(conn, values)
        except IntegrityError as exc:
            raise JobConflictError(dedupe_key) from exc
    return values


def _insert(conn: Any, values: dict[str, Any]) -> None:
    conn.execute(insert(jobs).values(**values))
    if conn.dialect.name == "postgresql":
        # Sai no commit: o worker que escuta (LISTEN) acorda na hora.
        conn.execute(text("SELECT pg_notify(:channel, :kind)"), {"channel": NOTIFY_CHANNEL, "kind": values["kind"]})


def get_job(job_id: str) -> dict[str, Any] | None:
    with _engine().connect() as conn:
        row = conn.execute(select(jobs).where(jobs.c.id == job_id)).fetchone()
    return _row(row) if row else None


def active_job(dedupe_key: str) -> dict[str, Any] | None:
    with _engine().connect() as conn:
        row = conn.execute(
            select(jobs).where(jobs.c.dedupe_key == dedupe_key, jobs.c.status.in_(ACTIVE))
        ).fetchone()
    return _row(row) if row else None


def list_jobs(
    *, status: str | None = None, kind: str | None = None, dedupe_key: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    stmt = select(jobs).order_by(jobs.c.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(jobs.c.status == status)
    if kind:
        stmt = stmt.where(jobs.c.kind == kind)
    if dedupe_key:
        stmt = stmt.where(jobs.c.dedupe_key == dedupe_key)
    with _engine().connect() as conn:
        return [_row(r) for r in conn.execute(stmt).fetchall()]


def counts() -> dict[str, int]:
    with _engine().connect() as conn:
        rows = conn.execute(select(jobs.c.status, func.count()).group_by(jobs.c.status)).fetchall()
    return {status: 0 for status in STATUSES} | {status: n for status, n in rows}


def claim(worker_id: str, kinds: list[str], lease: timedelta) -> dict[str, Any] | None:
    """Pega o próximo job vencido de um dos `kinds` e o marca como `running` deste worker.

    No Postgres, `FOR UPDATE SKIP LOCKED`: réplicas disputando não esperam umas pelas
    outras nem pegam o mesmo job. O `status='queued'` do UPDATE é a garantia final
    (também no SQLite, que não tem SKIP LOCKED)."""
    if not kinds:
        return None
    now = _now()
    candidate = (
        select(jobs.c.id)
        .where(jobs.c.status == "queued", jobs.c.run_after <= now, jobs.c.kind.in_(kinds))
        .order_by(jobs.c.run_after)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    with _engine().begin() as conn:
        row = conn.execute(
            update(jobs)
            .where(jobs.c.id == candidate, jobs.c.status == "queued")
            .values(status="running", locked_by=worker_id, locked_until=now + lease,
                    attempts=jobs.c.attempts + 1, updated_at=now)
            .returning(*jobs.c)
        ).fetchone()
    return _row(row) if row else None


def renew(job_id: str, worker_id: str, lease: timedelta) -> bool:
    """Estende o lease. `False` = o job não é mais deste worker (lease vencido e
    devolvido à fila, ou cancelado): quem o roda deve parar."""
    now = _now()
    with _engine().begin() as conn:
        result = conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id, jobs.c.locked_by == worker_id, jobs.c.status == "running")
            .values(locked_until=now + lease, updated_at=now)
        )
    return result.rowcount == 1


def _owned(job_id: str, worker_id: str):
    return (jobs.c.id == job_id, jobs.c.locked_by == worker_id, jobs.c.status == "running")


def complete(job_id: str, worker_id: str, *, follow_up: dict[str, Any] | None = None) -> bool:
    """Fecha o job como `succeeded` e, se houver, enfileira o próximo (`follow_up`:
    argumentos de `enqueue`) na mesma transação — o agendamento recorrente não se
    perde entre um e outro."""
    now = _now()
    with _engine().begin() as conn:
        result = conn.execute(
            update(jobs).where(*_owned(job_id, worker_id))
            .values(status="succeeded", locked_by=None, locked_until=None, last_error=None,
                    finished_at=now, updated_at=now)
        )
        if result.rowcount != 1:
            return False
        if follow_up is not None:
            try:
                enqueue(conn=conn, **follow_up)
            except JobConflictError:
                pass  # alguém já pôs o próximo na fila (um sync manual, por exemplo)
    return True


def fail(job_id: str, worker_id: str, error: str) -> str | None:
    """Registra a falha de uma tentativa: volta à fila com backoff, ou `failed` se as
    tentativas acabaram. Devolve o novo status (ou `None` se o job não era mais deste worker)."""
    now = _now()
    with _engine().begin() as conn:
        row = conn.execute(select(jobs.c.attempts, jobs.c.max_attempts).where(*_owned(job_id, worker_id))).fetchone()
        if row is None:
            return None
        attempts, max_attempts = row
        if attempts >= max_attempts:
            values = {"status": "failed", "finished_at": now}
        else:
            values = {"status": "queued", "run_after": now + backoff(attempts)}
        result = conn.execute(
            update(jobs).where(*_owned(job_id, worker_id))
            .values(**values, locked_by=None, locked_until=None, last_error=error[:2000], updated_at=now)
        )
    return values["status"] if result.rowcount == 1 else None


def release(job_id: str, worker_id: str) -> bool:
    """Devolve à fila sem gastar tentativa — o serviço está desligando, não foi o job que falhou."""
    now = _now()
    with _engine().begin() as conn:
        result = conn.execute(
            update(jobs).where(*_owned(job_id, worker_id))
            .values(status="queued", run_after=now, attempts=jobs.c.attempts - 1,
                    locked_by=None, locked_until=None, updated_at=now)
        )
    return result.rowcount == 1


def reap_expired() -> list[dict[str, Any]]:
    """Jobs `running` cujo lease venceu: o processo que os rodava morreu. Voltam à
    fila (com backoff), ou viram `failed` se já gastaram as tentativas. Devolve os
    que viraram `failed`, para o handler poder reagir."""
    now = _now()
    given_up: list[dict[str, Any]] = []
    with _engine().begin() as conn:
        expired = conn.execute(
            select(jobs).where(jobs.c.status == "running", jobs.c.locked_until < now)
            .with_for_update(skip_locked=True)
        ).fetchall()
        for row in map(_row, expired):
            error = f"Lease vencido em {row['locked_by']} (o processo parou no meio)."
            if row["attempts"] >= row["max_attempts"]:
                values = {"status": "failed", "finished_at": now}
            else:
                values = {"status": "queued", "run_after": now + backoff(row["attempts"])}
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == row["id"], jobs.c.status == "running", jobs.c.locked_until < now)
                .values(**values, locked_by=None, locked_until=None, last_error=error, updated_at=now)
            )
            if result.rowcount == 1 and values["status"] == "failed":
                given_up.append({**row, **values, "last_error": error})
    return given_up


def cancel(job_id: str) -> dict[str, Any] | None:
    """Cancela um job ativo. Um `running` para no próximo `renew` de quem o roda."""
    now = _now()
    with _engine().begin() as conn:
        conn.execute(
            update(jobs).where(jobs.c.id == job_id, jobs.c.status.in_(ACTIVE))
            .values(status="cancelled", locked_by=None, locked_until=None, finished_at=now, updated_at=now)
        )
    return get_job(job_id)


def cancel_dedupe(dedupe_key: str, *, only_queued: bool = False) -> list[str]:
    """Cancela os jobs ativos de uma chave; devolve os ids cancelados."""
    now = _now()
    statuses = ("queued",) if only_queued else ACTIVE
    with _engine().begin() as conn:
        rows = conn.execute(
            update(jobs).where(jobs.c.dedupe_key == dedupe_key, jobs.c.status.in_(statuses))
            .values(status="cancelled", locked_by=None, locked_until=None, finished_at=now, updated_at=now)
            .returning(jobs.c.id)
        ).fetchall()
    return [r[0] for r in rows]


def advance(dedupe_key: str, run_after: datetime | None = None) -> bool:
    """Puxa para `run_after` (agora, por padrão) o job ainda na fila desta chave.
    `False` se não há job `queued` com ela."""
    now = _now()
    with _engine().begin() as conn:
        result = conn.execute(
            update(jobs).where(jobs.c.dedupe_key == dedupe_key, jobs.c.status == "queued")
            .values(run_after=run_after or now, updated_at=now)
        )
        if result.rowcount and conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_notify(:channel, '')"), {"channel": NOTIFY_CHANNEL})
    return bool(result.rowcount)


def retry(job_id: str) -> dict[str, Any]:
    """Põe de volta na fila um job `failed` ou `cancelled`, com as tentativas zeradas.
    `JobConflictError` se outro job da mesma chave já estiver ativo; `ValueError`
    se o job não terminou."""
    now = _now()
    job = get_job(job_id)
    if job is None:
        raise LookupError(job_id)
    if job["status"] not in ("failed", "cancelled"):
        raise ValueError(f"o job está {job['status']}; só failed ou cancelled voltam para a fila")
    try:
        with _engine().begin() as conn:
            conn.execute(
                update(jobs).where(jobs.c.id == job_id, jobs.c.status.in_(("failed", "cancelled")))
                .values(status="queued", attempts=0, run_after=now, finished_at=None, updated_at=now)
            )
            if conn.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_notify(:channel, :kind)"), {"channel": NOTIFY_CHANNEL, "kind": job["kind"]})
    except IntegrityError as exc:
        raise JobConflictError(job["dedupe_key"]) from exc
    return get_job(job_id)  # type: ignore[return-value]
