"""Bancos Postgres descartáveis para os testes (`TEST_DATABASE_URL`).

`TEST_DATABASE_URL` aponta para um Postgres com pgvector (a imagem
`pgvector/pgvector:pg16`, a mesma do compose) e um usuário que pode criar bancos.
O banco da URL só serve de porta de entrada: cada sessão do pytest cria o seu
(`kuro_test_<hex>`) e o apaga no fim; o teste das migrações cria mais um, vazio.
Sem a variável, nada aqui é usado e os testes `@pytest.mark.postgres` são pulados.

Este módulo não importa nada do `agent_service`: o `conftest.py` o usa antes de
definir o `DATABASE_URL`, que as configurações do serviço leem uma vez só.
"""

import os
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip() or None

_session_url: str | None = None


def _base_url() -> URL:
    url = make_url(TEST_DATABASE_URL or "")
    if url.get_backend_name() != "postgresql":
        raise RuntimeError(f"TEST_DATABASE_URL precisa ser um Postgres (veio {url.get_backend_name()!r})")
    # O serviço fala psycopg 3 (`postgresql+psycopg://`); `postgresql://` puro viraria psycopg2.
    return url.set(drivername="postgresql+psycopg")


def _admin_engine() -> sa.Engine:
    # CREATE/DROP DATABASE não rodam dentro de transação.
    return sa.create_engine(_base_url(), isolation_level="AUTOCOMMIT", pool_pre_ping=True)


def create_database(prefix: str) -> str:
    """Cria um banco novo no servidor de `TEST_DATABASE_URL`, com a extensão `vector`,
    e devolve a URL dele (com a senha, para o serviço conseguir conectar)."""
    name = f"{prefix}_{uuid4().hex[:10]}"
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except sa.exc.OperationalError as exc:
        raise RuntimeError(f"TEST_DATABASE_URL definido, mas o Postgres não respondeu: {exc}") from exc
    finally:
        admin.dispose()
    url = _base_url().set(database=name)
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        engine.dispose()
    return url.render_as_string(hide_password=False)


def drop_database(url: str) -> None:
    """Apaga o banco, derrubando conexões que tenham sobrado (Postgres 13+)."""
    name = make_url(url).database
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def create_session_database() -> str:
    global _session_url
    _session_url = create_database("kuro_test")
    return _session_url


def drop_session_database() -> None:
    if _session_url and not os.environ.get("KURO_TEST_KEEP_DB"):
        drop_database(_session_url)
