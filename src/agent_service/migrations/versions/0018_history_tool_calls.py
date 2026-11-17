"""Limite de chamadas de tool antigas no histórico, por agente.

`history_tool_calls`: quantas chamadas de tool dos turnos anteriores voltam ao
modelo junto com o histórico (as mais recentes; vira `max_tool_calls_from_history`
do Agno). Nulo = todas, como antes: os agentes que já existem não mudam (nem de
versão).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_definitions", sa.Column("history_tool_calls", sa.Integer, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_definitions") as batch:
        batch.drop_column("history_tool_calls")
