"""Base: o schema como estava antes do Alembic (2026-09-25).

Serve tanto para um banco vazio quanto para um criado pelo antigo
`create_all` + `_add_missing_columns`: cada tabela é criada se não existir, e
numa tabela existente só entram as colunas que faltam. Isso absorve os
`ALTER TABLE` que ficavam espalhados nos `init_store()` dos stores.

Também roda, uma vez, as duas conversões de dados que viviam no startup:
credenciais da tabela antiga `model_provider_credentials` e notas de feedback
que eram markdown solto.

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""

import re
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_JSON_EMPTY_LIST = sa.text("'[]'")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _ensure_table(name: str, columns: list[sa.Column], indexes: tuple[str, ...] = ()) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(name):
        op.create_table(name, *columns)
        for column in indexes:
            op.create_index(f"ix_{name}_{column}", name, [column])
        return
    existing = {c["name"] for c in inspector.get_columns(name)}
    for column in columns:
        if column.name not in existing:
            op.add_column(name, column)


def upgrade() -> None:
    _ensure_table(
        "agent_definitions",
        [
            sa.Column("agent_type", sa.String, primary_key=True),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("instructions", sa.JSON, nullable=False),
            sa.Column("tools", sa.JSON, nullable=False),
            sa.Column("model_provider", sa.String, nullable=True),
            sa.Column("model_id", sa.String, nullable=True),
            sa.Column("model_credential_id", sa.String, nullable=True),
            sa.Column("knowledge_collection", sa.String, nullable=True),
            sa.Column("dependency_fields", sa.JSON, nullable=False),
            sa.Column("memory_backend", sa.String, nullable=False),
            sa.Column("num_history_runs", sa.Integer, nullable=False),
            sa.Column("kind", sa.String, nullable=False, server_default="conversational"),
            sa.Column("response_schema", sa.JSON, nullable=False, server_default=_JSON_EMPTY_LIST),
            sa.Column("is_seed", sa.Boolean, nullable=False),
            sa.Column("prompt_version", sa.Integer, nullable=False),
            *_timestamps(),
        ],
    )
    _ensure_table(
        "agent_prompt_versions",
        [
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("agent_type", sa.String, nullable=False),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("instructions", sa.JSON, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        ],
        indexes=("agent_type",),
    )
    _ensure_table(
        "agent_feedback_notes",
        [
            sa.Column("agent_type", sa.String, primary_key=True),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("rules", sa.JSON, nullable=False, server_default=_JSON_EMPTY_LIST),
            sa.Column("version", sa.Integer, nullable=False, server_default="1"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        ],
    )
    _ensure_table(
        "agent_feedback_versions",
        [
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("agent_type", sa.String, nullable=False),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("rules", sa.JSON, nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("origin", sa.String, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        ],
        indexes=("agent_type",),
    )
    _ensure_table(
        "tool_definitions",
        [
            sa.Column("tool_name", sa.String, primary_key=True),
            sa.Column("kind", sa.String, nullable=False),
            sa.Column("label", sa.String, nullable=False),
            sa.Column("description", sa.String, nullable=True),
            sa.Column("config", sa.JSON, nullable=False),
            sa.Column("enabled", sa.Boolean, nullable=False),
            sa.Column("is_seed", sa.Boolean, nullable=False),
            *_timestamps(),
        ],
    )
    _ensure_table(
        "model_credentials",
        [
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("provider", sa.String, nullable=False),
            sa.Column("label", sa.String, nullable=False),
            sa.Column("api_key_encrypted", sa.String, nullable=True),
            sa.Column("key_hint", sa.String, nullable=True),
            sa.Column("base_url", sa.String, nullable=True),
            sa.Column("enabled", sa.Boolean, nullable=False),
            sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_test_ok", sa.Boolean, nullable=True),
            sa.Column("last_test_message", sa.String, nullable=True),
            *_timestamps(),
        ],
        indexes=("provider",),
    )
    _ensure_table(
        "document_collections",
        [
            sa.Column("name", sa.String, primary_key=True),
            sa.Column("label", sa.String, nullable=False),
            sa.Column("description", sa.String, nullable=True),
            sa.Column("is_seed", sa.Boolean, nullable=False),
            sa.Column("embedder_provider", sa.String, nullable=True),
            sa.Column("embedder_model", sa.String, nullable=True),
            sa.Column("embedder_dimensions", sa.Integer, nullable=True),
            *_timestamps(),
        ],
    )
    _ensure_table(
        "runs",
        [
            sa.Column("run_id", sa.String, primary_key=True),
            sa.Column("trace_id", sa.String, nullable=False),
            sa.Column("tenant_id", sa.String, nullable=False),
            sa.Column("agent_type", sa.String, nullable=False),
            sa.Column("agent_name", sa.String, nullable=True),
            sa.Column("prompt_version", sa.Integer, nullable=True),
            sa.Column("config_hash", sa.String, nullable=True),
            sa.Column("endpoint", sa.String, nullable=True),
            sa.Column("user_id", sa.String, nullable=True),
            sa.Column("session_id", sa.String, nullable=True),
            sa.Column("status", sa.String, nullable=False),
            sa.Column("status_message", sa.Text, nullable=True),
            sa.Column("message", sa.Text, nullable=True),
            sa.Column("output", sa.Text, nullable=True),
            sa.Column("model", sa.String, nullable=True),
            sa.Column("input_tokens", sa.Integer, nullable=False),
            sa.Column("output_tokens", sa.Integer, nullable=False),
            sa.Column("total_tokens", sa.Integer, nullable=False),
            sa.Column("cost_usd", sa.Float, nullable=True),
            sa.Column("latency_ms", sa.Float, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        ],
        indexes=("tenant_id", "agent_type", "session_id", "started_at"),
    )
    _ensure_table(
        "run_spans",
        [
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("run_id", sa.String, nullable=False),
            sa.Column("type", sa.String, nullable=False),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("level", sa.String, nullable=False),
            sa.Column("status_message", sa.Text, nullable=True),
            sa.Column("input", sa.JSON, nullable=True),
            sa.Column("output", sa.JSON, nullable=True),
            sa.Column("model", sa.String, nullable=True),
            sa.Column("input_tokens", sa.Integer, nullable=False),
            sa.Column("output_tokens", sa.Integer, nullable=False),
            sa.Column("total_tokens", sa.Integer, nullable=False),
            sa.Column("cost_usd", sa.Float, nullable=True),
            sa.Column("latency_ms", sa.Float, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata", sa.JSON, nullable=True),
        ],
        indexes=("run_id",),
    )
    _ensure_table(
        "run_scores",
        [
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("run_id", sa.String, nullable=False),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("value", sa.Float, nullable=False),
            sa.Column("data_type", sa.String, nullable=False),
            sa.Column("comment", sa.Text, nullable=True),
            sa.Column("user_id", sa.String, nullable=True),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        ],
        indexes=("run_id",),
    )

    _migrate_legacy_credentials()
    migrate_markdown_notes(op.get_bind())


# -- conversões de dados (uma vez) ----------------------------------------------

_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _rules_from_markdown(content: str) -> list[dict[str, str]]:
    rules = []
    for line in content.splitlines():
        texto = _BULLET.sub("", line).strip()
        if texto:
            rules.append({"id": uuid.uuid4().hex[:8], "texto": texto})
    return rules


def _rules_to_markdown(rules: list[dict[str, str]]) -> str:
    return "\n".join(f"- {r['texto']}" for r in rules)


def migrate_markdown_notes(connection: sa.engine.Connection) -> None:
    """Notas que eram markdown solto viram regras com id, com a v1 no histórico.

    Converter só na leitura não bastava: os ids saíam diferentes a cada consulta
    (`--remove <id>` nunca casava) e não havia v1 para onde voltar."""
    notes = sa.table("agent_feedback_notes", sa.column("agent_type"), sa.column("content"), sa.column("rules", sa.JSON))
    versions = sa.table(
        "agent_feedback_versions",
        sa.column("agent_type"),
        sa.column("version"),
        sa.column("rules", sa.JSON),
        sa.column("content"),
        sa.column("origin"),
    )
    # O filtro é em Python: comparar JSON com `[]` no SQL depende do dialeto.
    for agent_type, content, rules in connection.execute(
        sa.select(notes.c.agent_type, notes.c.content, notes.c.rules)
    ).all():
        if rules or not content:
            continue
        converted = _rules_from_markdown(content)
        if not converted:
            continue
        markdown = _rules_to_markdown(converted)
        connection.execute(
            sa.update(notes).where(notes.c.agent_type == agent_type).values(rules=converted, content=markdown)
        )
        connection.execute(
            sa.insert(versions).values(
                agent_type=agent_type, version=1, rules=converted, content=markdown, origin="merge"
            )
        )


def _migrate_legacy_credentials() -> None:
    """Uma credencial por provedor (tabela antiga) vira uma linha em `model_credentials`."""
    connection = op.get_bind()
    if not sa.inspect(connection).has_table("model_provider_credentials"):
        return
    columns = (
        "api_key_encrypted",
        "key_hint",
        "base_url",
        "enabled",
        "last_tested_at",
        "last_test_ok",
        "last_test_message",
    )
    legacy = sa.table("model_provider_credentials", sa.column("provider"), *(sa.column(c) for c in columns))
    current = sa.table(
        "model_credentials",
        sa.column("id"),
        sa.column("provider"),
        sa.column("label"),
        *(sa.column(c) for c in columns),
    )
    if connection.execute(sa.select(current.c.id).limit(1)).first() is not None:
        return  # já migrado (ou já há credenciais cadastradas)
    for row in connection.execute(sa.select(legacy)).all():
        data = dict(row._mapping)
        connection.execute(sa.insert(current).values(id=uuid.uuid4().hex, label="Padrão", **data))
