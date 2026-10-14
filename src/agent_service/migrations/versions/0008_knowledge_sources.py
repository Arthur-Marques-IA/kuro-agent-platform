"""Fontes de uma collection que o Kuro mantém sincronizadas (sites).

- `knowledge_sources`: um site (página única, sitemap ou crawl) ligado a uma
  collection, com as opções da coleta, o agendamento (`refresh`) e o resumo da
  última sincronização. `status='running'` é também a trava: o `UPDATE ... WHERE
  status <> 'running'` que inicia um sync garante que dois não rodem juntos.
- `knowledge_source_pages`: cada página indexada — o `content_id` dela na
  collection e o hash do texto, para pular a página que não mudou e remover a
  que sumiu do site.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("collection", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False, server_default="web"),
        sa.Column("url", sa.String, nullable=False),
        sa.Column("label", sa.String, nullable=True),
        sa.Column("mode", sa.String, nullable=False),
        sa.Column("options", sa.JSON, nullable=False),
        sa.Column("refresh", sa.String, nullable=False, server_default="never"),
        sa.Column("status", sa.String, nullable=False, server_default="pending"),
        sa.Column("status_message", sa.Text, nullable=True),
        sa.Column("progress", sa.JSON, nullable=True),
        sa.Column("pages_indexed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pages_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("collection", "url", "mode", name="uq_knowledge_sources_url"),
    )
    op.create_index("ix_knowledge_sources_collection", "knowledge_sources", ["collection"])
    op.create_table(
        "knowledge_source_pages",
        sa.Column("source_id", sa.String, primary_key=True),
        sa.Column("url", sa.String, primary_key=True),
        sa.Column("content_id", sa.String, nullable=True),
        sa.Column("title", sa.String, nullable=True),
        sa.Column("content_hash", sa.String, nullable=True),
        sa.Column("chunks", sa.Integer, nullable=False, server_default="0"),
        sa.Column("formatted", sa.String, nullable=True),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("knowledge_source_pages")
    op.drop_index("ix_knowledge_sources_collection", table_name="knowledge_sources")
    op.drop_table("knowledge_sources")
