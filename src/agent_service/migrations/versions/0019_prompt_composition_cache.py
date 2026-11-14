"""Composição do prompt e tokens de cache como campos do run.

- `runs.cache_read_tokens` / `cache_write_tokens`: tokens do prompt lidos do cache e escritos
  nele (antes só na metadata da span do modelo). Runs antigos ficam com 0: não há como saber.
- `runs.prompt_composition`: quanto do prompt da primeira chamada veio de cada parte
  (instruções, contexto, histórico...), em caracteres (JSON). NULL em runs antigos.
- `runs.prompt_chars` / `history_chars`: o total e a parte de histórico, para o panorama
  tirar a média sem abrir o JSON.

Nenhuma retrocarga, e o `cost_usd` dos runs antigos não é recalculado.

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
    op.add_column("runs", sa.Column("cache_read_tokens", sa.Integer, nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("cache_write_tokens", sa.Integer, nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("prompt_composition", sa.JSON, nullable=True))
    op.add_column("runs", sa.Column("prompt_chars", sa.Integer, nullable=True))
    op.add_column("runs", sa.Column("history_chars", sa.Integer, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("history_chars")
        batch.drop_column("prompt_chars")
        batch.drop_column("prompt_composition")
        batch.drop_column("cache_write_tokens")
        batch.drop_column("cache_read_tokens")
