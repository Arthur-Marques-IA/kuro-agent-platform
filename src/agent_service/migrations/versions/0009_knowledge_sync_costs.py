"""Custo do formatador de sites por sincronização.

- `knowledge_sync_costs`: tokens e custo que o agente formatador gastou numa
  sincronização de uma fonte (`documents/web_sync.py`). Os Logs somam isso ao
  custo do período (`observability/overview.py`). Sem chave estrangeira para a
  fonte: o gasto continua contando depois de ela ser removida.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_sync_costs",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("source_id", sa.String, nullable=False),
        sa.Column("collection", sa.String, nullable=False),
        sa.Column("model", sa.String, nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_sync_costs_ended_at", "knowledge_sync_costs", ["ended_at"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_sync_costs_ended_at", table_name="knowledge_sync_costs")
    op.drop_table("knowledge_sync_costs")
