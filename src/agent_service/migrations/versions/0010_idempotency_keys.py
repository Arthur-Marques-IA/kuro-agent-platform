"""`Idempotency-Key` em /chat e /analyze.

- `idempotency_keys`: uma linha por chave (endpoint + agente + chave), com o hash do
  corpo, o status (`in_progress` | `done`) e a resposta guardada para devolver a quem
  repete (`api/idempotency.py`). A chave única é a trava: de duas chamadas simultâneas
  com a mesma chave, só uma grava.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("endpoint", sa.String, nullable=False),
        sa.Column("agent_type", sa.String, nullable=False),
        sa.Column("key", sa.String, nullable=False),
        sa.Column("request_hash", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("response", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("endpoint", "agent_type", "key", name="uq_idempotency_keys_key"),
    )
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_created_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
