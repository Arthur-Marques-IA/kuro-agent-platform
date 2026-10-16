"""Configuração de busca por collection, versionada, e a versão dela gravada no run.

- `document_collections`: `search_mode` (`vector` | `hybrid`), `language` (dicionário
  do full-text do Postgres), `max_results` (k), `vector_weight` (peso do vetor no
  híbrido) e `reranker` (gancho; hoje só `NULL`). Tudo `NULL` = o comportamento de
  antes (busca vetorial, k=10). As collections existentes ficam com `language`
  `NULL` (o `english` do Agno) — só faz diferença se alguém ligar o híbrido.
- `collection_versions`: um snapshot imutável por configuração de busca, como
  `agent_versions` (`documents/versions.py`). As collections existentes ganham a v1.
- `runs.knowledge_collection`, `runs.collection_version`, `runs.collection_config_hash`:
  com que configuração de busca o run consultou a base.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15
"""

import hashlib
import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# Cópia de `documents/versions.py` no momento desta migração — ela não pode
# importar o código da aplicação, que muda depois.
_DEFAULT_PROVIDER = "google"
_DEFAULT_MAX_RESULTS = 10


def _snapshot(row: sa.Row) -> dict:
    return {
        "embedder_provider": (row.embedder_provider or _DEFAULT_PROVIDER).lower(),
        "embedder_model": row.embedder_model,
        "embedder_dimensions": row.embedder_dimensions,
        "search_mode": "vector",
        "language": None,
        "max_results": _DEFAULT_MAX_RESULTS,
    }


def _hash(config: dict) -> str:
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def upgrade() -> None:
    with op.batch_alter_table("document_collections") as batch:
        batch.add_column(sa.Column("search_mode", sa.String, nullable=True))
        batch.add_column(sa.Column("language", sa.String, nullable=True))
        batch.add_column(sa.Column("max_results", sa.Integer, nullable=True))
        batch.add_column(sa.Column("vector_weight", sa.Float, nullable=True))
        batch.add_column(sa.Column("reranker", sa.String, nullable=True))

    versions = op.create_table(
        "collection_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("collection", sa.String, nullable=False, index=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("config_hash", sa.String, nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("collection", "config_hash", name="uq_collection_versions_hash"),
        sa.UniqueConstraint("collection", "version", name="uq_collection_versions_version"),
    )

    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("knowledge_collection", sa.String, nullable=True))
        batch.add_column(sa.Column("collection_version", sa.Integer, nullable=True))
        batch.add_column(sa.Column("collection_config_hash", sa.String, nullable=True))

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT name, embedder_provider, embedder_model, embedder_dimensions FROM document_collections")
    ).fetchall()
    now = datetime.now(timezone.utc)
    if rows:
        op.bulk_insert(
            versions,
            [
                {
                    "collection": row.name,
                    "version": 1,
                    "config_hash": _hash(_snapshot(row)),
                    "config": _snapshot(row),
                    "created_at": now,
                }
                for row in rows
            ],
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("collection_config_hash")
        batch.drop_column("collection_version")
        batch.drop_column("knowledge_collection")
    op.drop_table("collection_versions")
    with op.batch_alter_table("document_collections") as batch:
        batch.drop_column("reranker")
        batch.drop_column("vector_weight")
        batch.drop_column("max_results")
        batch.drop_column("language")
        batch.drop_column("search_mode")
