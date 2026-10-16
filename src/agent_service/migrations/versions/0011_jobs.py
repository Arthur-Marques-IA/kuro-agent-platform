"""Fila de jobs no próprio Postgres (`agent_service/jobs/`).

- `jobs`: uma linha por trabalho em background (`kind` + `payload`). O worker pega
  o próximo com `FOR UPDATE SKIP LOCKED`, segura um lease (`locked_until`) enquanto
  roda e grava o resultado. Agendar é enfileirar com `run_after` no futuro.
- `dedupe_key` com índice único parcial (só entre `queued`/`running`): a mesma
  coisa não entra duas vezes na fila — é a trava do "já está sincronizando".

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_ACTIVE = sa.text("status IN ('queued', 'running')")


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String, nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("dedupe_key", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])
    op.create_index(
        "uq_jobs_dedupe_active", "jobs", ["dedupe_key"], unique=True,
        postgresql_where=_ACTIVE, sqlite_where=_ACTIVE,
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_dedupe_active", table_name="jobs")
    op.drop_index("ix_jobs_created_at", table_name="jobs")
    op.drop_index("ix_jobs_status_run_after", table_name="jobs")
    op.drop_table("jobs")
