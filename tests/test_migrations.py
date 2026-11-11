"""Migrações (Alembic): banco vazio e banco criado pelo antigo `create_all`."""

import sqlalchemy as sa
from alembic.script import ScriptDirectory

from agent_service.migrations import _config, upgrade_database

HEAD = ScriptDirectory.from_config(_config()).get_current_head()


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _head(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()


def test_empty_database_goes_to_head(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'vazio.db'}")

    upgrade_database(engine)
    upgrade_database(engine)  # idempotente

    tables = set(sa.inspect(engine).get_table_names())
    assert {"agent_definitions", "tool_definitions", "runs", "run_spans", "agent_versions", "procedure_runs"} <= tables
    assert "agent_version" in _columns(engine, "runs")
    assert "stages" in _columns(engine, "agent_definitions")
    assert _head(engine) == HEAD


def test_database_from_before_alembic_gets_missing_columns_and_keeps_data(tmp_path):
    """O banco de produção nasceu de `create_all` + `ALTER TABLE` à mão, sem
    `alembic_version`. A base precisa adotar esse banco sem recriar nada."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legado.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE agent_definitions (agent_type VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,"
                " instructions JSON NOT NULL, tools JSON NOT NULL, model_provider VARCHAR, model_id VARCHAR,"
                " dependency_fields JSON NOT NULL, memory_backend VARCHAR NOT NULL, num_history_runs INTEGER NOT NULL,"
                " is_seed BOOLEAN NOT NULL, prompt_version INTEGER NOT NULL, created_at TIMESTAMP, updated_at TIMESTAMP)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO agent_definitions VALUES ('antigo', 'Antigo', '[\"oi\"]', '[]', NULL, NULL, '[]',"
                " 'common', 10, 0, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            sa.text("CREATE TABLE agent_feedback_notes (agent_type VARCHAR PRIMARY KEY, content TEXT NOT NULL, updated_at TIMESTAMP)")
        )
        conn.execute(
            sa.text("INSERT INTO agent_feedback_notes VALUES ('antigo', '- confirme o CPF\n- seja breve', CURRENT_TIMESTAMP)")
        )

    upgrade_database(engine)

    assert {"kind", "response_schema", "model_credential_id", "knowledge_collection"} <= _columns(engine, "agent_definitions")
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT kind, prompt_version FROM agent_definitions WHERE agent_type = 'antigo'")).one()
        assert tuple(row) == ("conversational", 3)
        # A nota em markdown virou regras, com a v1 no histórico.
        versions = conn.execute(sa.text("SELECT version FROM agent_feedback_versions WHERE agent_type = 'antigo'")).all()
        assert [v[0] for v in versions] == [1]
    assert _head(engine) == HEAD


def test_uma_cabeca_so():
    """Duas branches criando a revisão seguinte ao mesmo tempo dão duas cabeças, e o
    `upgrade head` do startup para. O teste das migrações em Postgres (`test_postgres.py`)
    vai e volta por elas; este roda sempre."""
    heads = ScriptDirectory.from_config(_config()).get_heads()
    assert len(heads) == 1, f"mais de uma cabeça nas migrações: {heads}"
