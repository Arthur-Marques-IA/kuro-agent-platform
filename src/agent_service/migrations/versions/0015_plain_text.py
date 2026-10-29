"""Resposta em texto puro por agente.

`plain_text`: com ele ligado o agente é montado com `markdown=False` e o Agno não
acrescenta "Use markdown to format your answers." ao prompt — num canal de texto
puro (WhatsApp) o `**negrito**` e as listas com `*` chegavam crus ao contato.
Padrão desligado: os agentes que já existem não mudam (nem de versão).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_definitions",
        sa.Column("plain_text", sa.Boolean, nullable=False, server_default=sa.false()),
    )
