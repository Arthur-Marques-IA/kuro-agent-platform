"""Migrações do schema com Alembic — o que substitui `create_all` + `ALTER TABLE` à mão.

`upgrade_database()` roda na subida do serviço (o lifespan de `main.py`, não no import),
dentro de `migration_lock()`, e leva o banco até a última revisão. Não há
`alembic.ini`: a configuração é montada aqui, apontando para `versions/` dentro do pacote, então funciona igual no container e no `uv run`.

As tabelas do Agno (`agno_*`) e as de vetores do pgvector ficam de fora: o
próprio Agno as cria e mantém.

Para uma mudança de schema nova: crie `versions/NNNN_descricao.py` com
`revision`/`down_revision` encadeados e operações explícitas (`op.add_column`,
`op.create_table`...). Nunca importe as tabelas do código da aplicação dentro de
uma migração — elas mudam depois, e a migração precisa descrever o schema
daquele momento.

Para rodar à mão (ex.: antes de subir uma versão nova): `uv run kuro-migrate`.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from agent_service.db import get_db

logger = logging.getLogger(__name__)

_SCRIPT_LOCATION = Path(__file__).parent


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    return config


def upgrade_database(engine: Engine | None = None) -> None:
    """Leva o banco até a última revisão. Idempotente: sem revisão nova, não faz nada.

    Um banco anterior ao Alembic (tabelas criadas por `create_all`) passa pela
    revisão de base como qualquer outro: ela cria o que falta e adiciona as
    colunas que faltam, sem recriar nada."""
    engine = engine or get_db().db_engine
    config = _config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


MIGRATION_LOCK_KEY = 0x6B75726F6D696772
"""Chave do `pg_advisory_lock` da migração: "kuromigr" em ASCII, um bigint fixo. Toda
instância do Kuro usa a mesma, então só uma migra por vez num mesmo banco. Trocar a chave
entre versões deixaria uma réplica velha e uma nova migrando juntas."""


@contextmanager
def migration_lock(engine: Engine) -> Iterator[None]:
    """Só uma instância migra (e semeia) por vez: `pg_advisory_lock` numa conexão dedicada.

    Duas réplicas subindo juntas (ou `--workers N`) correriam o mesmo `upgrade` ao mesmo
    tempo. Com o lock, a segunda espera; quando o pega, acha o banco já migrado e segue.
    O lock é de sessão: vive na conexão, que fica em AUTOCOMMIT para não segurar uma
    transação aberta durante a migração. Fora do Postgres (SQLite dos testes), sem lock."""
    if engine.dialect.name != "postgresql":
        yield
        return
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        got = connection.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY}).scalar()
        if not got:
            logger.info(
                "Esperando outra instância terminar de migrar o banco (pg_advisory_lock %d)...", MIGRATION_LOCK_KEY
            )
            connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
            logger.info("Lock de migração obtido.")
        try:
            yield
        finally:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
            except Exception:
                # Uma conexão que volta ao pool ainda com o lock travaria as outras réplicas:
                # descartada, o Postgres solta o lock ao fechar a sessão.
                logger.warning("Não consegui soltar o lock de migração; descartando a conexão", exc_info=True)
                connection.invalidate()
    finally:
        connection.close()


def main() -> None:
    """`kuro-migrate`: aplica as migrações pendentes e sai (com o mesmo lock da subida)."""
    logging.basicConfig(level=logging.INFO)
    engine = get_db().db_engine
    with migration_lock(engine):
        upgrade_database(engine)
    logger.info("Banco na última revisão.")
