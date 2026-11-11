import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from tests import pg  # noqa: E402

os.environ.setdefault("GOOGLE_API_KEY", "test-key")
if pg.TEST_DATABASE_URL:
    # Postgres de verdade (`TEST_DATABASE_URL`, ver docs/desenvolvimento.md): a suíte
    # inteira roda num banco novo criado só para esta sessão e apagado no fim — o
    # equivalente ao arquivo SQLite descartável de baixo. Servidor fora do ar é erro,
    # não pulo: senão o job de Postgres do CI ficaria verde sem testar nada.
    os.environ["DATABASE_URL"] = pg.create_session_database()
else:
    # Arquivo, não `:memory:`: o SQLite em memória é um banco por thread, e as rotas
    # fazem o I/O do banco numa thread do pool (`run_in_threadpool`).
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{Path(tempfile.mkdtemp()) / 'agent_service.db'}")
os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode())

from agent_service.agents.seed import seed_default_agents  # noqa: E402
from agent_service.migrations import upgrade_database  # noqa: E402
from agent_service.tools.seed import seed_default_tools  # noqa: E402
from agent_service.documents.store import seed_default_collection  # noqa: E402

upgrade_database()
seed_default_collection()
seed_default_tools()
seed_default_agents()

import socket  # noqa: E402

import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402

from agent_service.cli import connection  # noqa: E402
from agent_service.observability import run_store  # noqa: E402
from agent_service.tools import egress  # noqa: E402

ENV_FILE_KEY = connection.env_file_key
"""Guardado antes de `sem_env_do_repositorio` trocá-lo — o teste da leitura do `.env` o devolve."""

_IP_PUBLICO = "93.184.216.34"
DNS_DO_SISTEMA = socket.getaddrinfo
"""Guardado antes de qualquer teste trocar o resolvedor — ver `resolvedor_real`."""


def pytest_collection_modifyitems(config, items):
    """`@pytest.mark.postgres` precisa de um Postgres de verdade: sem `TEST_DATABASE_URL`, pula."""
    if pg.TEST_DATABASE_URL:
        return
    pular = pytest.mark.skip(reason="precisa de Postgres: defina TEST_DATABASE_URL (ver docs/desenvolvimento.md)")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(pular)


def pytest_sessionfinish(session, exitstatus):
    if pg.TEST_DATABASE_URL:
        from agent_service.db import get_db

        get_db().db_engine.dispose()
        pg.drop_session_database()


@pytest.fixture
def banco_postgres_vazio():
    """Um banco Postgres novo e vazio (sem migração nenhuma), apagado no fim do teste."""
    url = pg.create_database("kuro_vazio")
    engine = sa.create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        pg.drop_database(url)


@pytest.fixture
def resolvedor_real(monkeypatch):
    """Desfaz o `dns_deterministico` para o teste que precisa do resolvedor do SO.

    Sem isto nenhum teste exercitaria `getaddrinfo` de verdade, e um erro nos
    argumentos ou no índice da tupla que ele devolve passaria batido em toda a
    suíte para só aparecer na primeira chamada real."""
    monkeypatch.setattr(egress.socket, "getaddrinfo", DNS_DO_SISTEMA)


@pytest.fixture(autouse=True)
def dns_deterministico(monkeypatch):
    """Nenhum teste consulta DNS de verdade.

    O controle de egress (`tools/egress.py`) resolve o host antes de deixar uma
    tool sair para a rede. Sem isto, os testes de tool dependeriam de rede e de
    hosts reais — e `exemplo.test` nem resolve. Todo host passa a ser público;
    o teste que quer ver o bloqueio acontecendo substitui isto por conta dele.
    """
    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_IP_PUBLICO, port))]

    monkeypatch.setattr(egress.socket, "getaddrinfo", resolve)


@pytest.fixture(autouse=True)
def sem_env_do_repositorio(monkeypatch, tmp_path_factory):
    """Sem `KURO_API_KEY`, o `kuro` lê a `ADMIN_API_KEY` do `.env` da pasta atual e do
    checkout (`cli/connection.py`). A suíte roda no repositório, e o `.env` de quem
    desenvolve não pode entrar nos testes: a pasta lida passa a ser uma vazia."""
    vazia = tmp_path_factory.mktemp("sem-env")
    monkeypatch.setattr(
        connection, "env_file_key", lambda url, directories=None: ENV_FILE_KEY(url, directories or [vazia])
    )


@pytest.fixture
def env_da_pasta_atual(monkeypatch):
    """Desfaz o `sem_env_do_repositorio` para o teste da leitura do `.env` (que escolhe a pasta)."""
    monkeypatch.setattr(connection, "env_file_key", ENV_FILE_KEY)
    return ENV_FILE_KEY


@pytest.fixture(autouse=True)
def gravacao_sincrona(monkeypatch):
    """O trace store local grava numa thread sem esperar, para não atrasar a
    resposta. Nos testes grava na hora, para o teste poder ler logo em seguida."""
    monkeypatch.setattr(run_store, "_submit", lambda fn: fn())
