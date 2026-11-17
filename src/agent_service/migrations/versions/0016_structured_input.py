"""Entrada estruturada do `/chat`: `events` e `context` em colunas próprias.

- `runs.input_message`: a fala pura do contato;
- `runs.input_events`: os eventos desde a última resposta (JSON);
- `runs.input_context`: os blocos de contexto, cada um com o hash do conteúdo (JSON).

`runs.message` continua com o texto que o modelo leu (montado, na entrada
estruturada). Os runs já gravados ficam com as três NULL — "entrada não
estruturada": não há como separar os trechos do texto antigo sem adivinhar, e
nenhuma retrocarga é feita.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("input_message", sa.Text, nullable=True))
    op.add_column("runs", sa.Column("input_events", sa.JSON, nullable=True))
    op.add_column("runs", sa.Column("input_context", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("input_context")
        batch.drop_column("input_events")
        batch.drop_column("input_message")
