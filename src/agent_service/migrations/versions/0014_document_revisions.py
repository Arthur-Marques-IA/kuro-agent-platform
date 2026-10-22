"""Revisões de documento e estado da marcação de trecho — o ciclo de correção do RAG.

- `document_revisions`: uma linha por revisão de um documento de collection
  (`content_id` estável): hash do conteúdo, o texto quando é texto, quem gravou e
  uma nota. Documentos de antes desta migração ganham a revisão 1 na primeira vez
  que alguém precisa dela (`documents/revisions.py`), sem varrer a base aqui.
- `chunk_feedback` ganha `document_revision` (a revisão em que o trecho foi visto) e
  o estado: `status` (`open`, `resolved`, `dismissed`), `resolved_in_revision`,
  `resolved_by`, `resolved_at` e `resolution_note`. As marcações existentes ficam
  `open`, com a revisão desconhecida.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_revisions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("collection", sa.String, nullable=False),
        sa.Column("content_id", sa.String, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.String, nullable=True),
        sa.Column("text", sa.Text, nullable=True),
        sa.Column("filename", sa.String, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_by", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("content_id", "revision", name="uq_document_revisions_content_revision"),
    )
    op.create_index("ix_document_revisions_collection", "document_revisions", ["collection"])

    op.add_column("chunk_feedback", sa.Column("document_revision", sa.Integer, nullable=True))
    op.add_column("chunk_feedback", sa.Column("status", sa.String, nullable=False, server_default="open"))
    op.add_column("chunk_feedback", sa.Column("resolved_in_revision", sa.Integer, nullable=True))
    op.add_column("chunk_feedback", sa.Column("resolved_by", sa.String, nullable=True))
    op.add_column("chunk_feedback", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chunk_feedback", sa.Column("resolution_note", sa.Text, nullable=True))
    op.create_index("ix_chunk_feedback_content_status", "chunk_feedback", ["content_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_chunk_feedback_content_status", table_name="chunk_feedback")
    for column in ("resolution_note", "resolved_at", "resolved_by", "resolved_in_revision", "status", "document_revision"):
        op.drop_column("chunk_feedback", column)
    op.drop_index("ix_document_revisions_collection", table_name="document_revisions")
    op.drop_table("document_revisions")
