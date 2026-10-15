"""Feedback do operador por trecho recuperado pelo RAG.

- `chunk_feedback`: uma marcação por trecho de uma busca de um run (`span_id` +
  `chunk_id`): `relevant`, `irrelevant` ou `wrong` (conteúdo errado/desatualizado),
  com comentário opcional, quem marcou e quando. Pergunta, collection e documento
  são copiados da span (`metadata.references`), não do cliente — é o material do
  gabarito de um eval de RAG (pergunta → trechos relevantes).

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chunk_feedback",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("run_id", sa.String, nullable=False),
        sa.Column("span_id", sa.String, nullable=False),
        sa.Column("agent_type", sa.String, nullable=True),
        sa.Column("query", sa.Text, nullable=False),
        sa.Column("collection", sa.String, nullable=True),
        sa.Column("content_id", sa.String, nullable=True),
        sa.Column("chunk_id", sa.String, nullable=False),
        sa.Column("document", sa.String, nullable=True),
        sa.Column("rank", sa.Integer, nullable=True),
        sa.Column("label", sa.String, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("created_by", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("span_id", "chunk_id", name="uq_chunk_feedback_span_chunk"),
    )
    op.create_index("ix_chunk_feedback_run_id", "chunk_feedback", ["run_id"])
    op.create_index("ix_chunk_feedback_collection_label", "chunk_feedback", ["collection", "label"])


def downgrade() -> None:
    op.drop_index("ix_chunk_feedback_collection_label", table_name="chunk_feedback")
    op.drop_index("ix_chunk_feedback_run_id", table_name="chunk_feedback")
    op.drop_table("chunk_feedback")
