"""Checagens automáticas por agente e a origem dos scores.

- `agent_definitions.checks`: lista de checagens determinísticas (`name`, `field`,
  `op`, `value`, `when`, `severity`) avaliadas em toda execução real
  (`observability/checks.py`). Nulo = nenhuma, como antes: os agentes que já existem
  não mudam (nem de versão).
- `run_scores.source`: de onde veio o score. Nulo = nota humana ou do `kuro eval`
  (o que sempre foi); `check` = resultado de uma checagem automática.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_definitions", sa.Column("checks", sa.JSON, nullable=True))
    op.add_column("run_scores", sa.Column("source", sa.String, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("run_scores") as batch:
        batch.drop_column("source")
    with op.batch_alter_table("agent_definitions") as batch:
        batch.drop_column("checks")
