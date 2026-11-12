"""A subida do serviço: nada no import, tudo no lifespan, na ordem certa e sob o lock de migração."""

import os
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from agent_service import main
from agent_service import migrations
from agent_service.api import auth
from agent_service.config import get_settings


def test_importar_o_main_nao_toca_no_banco_nem_le_configuracao(tmp_path):
    """Num processo limpo, sem chave nem ALLOW_OPEN e com um banco que não existe: o import
    passa, e o arquivo do banco nem chega a ser criado (antes, o import migrava e semeava)."""
    banco = tmp_path / "nunca.db"
    env = {k: v for k, v in os.environ.items() if k not in {"ALLOW_OPEN", "ADMIN_API_KEY", "RUNTIME_API_KEY"}}
    env["DATABASE_URL"] = f"sqlite:///{banco}"
    env["CORS_ORIGINS"] = "*"  # inválido: só a subida pode recusar
    resultado = subprocess.run(
        [sys.executable, "-c", "import agent_service.main as m; print(type(m.app).__name__)"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == "Service"
    if banco.exists():
        with sqlite3.connect(banco) as conn:
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_sem_lifespan_a_requisicao_recebe_503():
    """`TestClient` sem `with` não roda o lifespan: o app de verdade ainda não existe."""
    resposta = TestClient(main.Service()).get("/health")
    assert resposta.status_code == 503


@pytest.fixture
def ordem(monkeypatch):
    """Registra a ordem das etapas da subida, chamando as funções de verdade."""
    chamadas: list[str] = []

    def espia(nome, fn):
        def chamada(*args, **kwargs):
            chamadas.append(nome)
            return fn(*args, **kwargs)

        return chamada

    for nome in (
        "upgrade_database",
        "seed_default_collection",
        "seed_default_tools",
        "seed_default_agents",
        "build_app",
    ):
        monkeypatch.setattr(main, nome, espia(nome, getattr(main, nome)))
    monkeypatch.setattr(auth, "check_startup_auth", espia("check_startup_auth", auth.check_startup_auth))
    monkeypatch.setattr(main, "warm_up_model_listing", lambda: chamadas.append("warm_up"))

    def lifespan_espia(nome):
        @asynccontextmanager
        async def lifespan(_app=None):
            chamadas.append(nome)
            yield

        return lifespan

    monkeypatch.setattr(main, "scheduler_lifespan", lifespan_espia("scheduler"))
    monkeypatch.setattr(main.jobs_worker, "lifespan", lifespan_espia("jobs_worker"))

    real_lock = main.migration_lock

    @contextmanager
    def lock(engine):
        with real_lock(engine):
            chamadas.append("lock")
            yield
            chamadas.append("unlock")

    monkeypatch.setattr(main, "migration_lock", lock)
    return chamadas


def test_o_lifespan_migra_semeia_monta_e_so_depois_sobe_os_workers(ordem, monkeypatch):
    # As collections são pgvector (Postgres); no SQLite da suíte o AgentOS sobe sem elas.
    monkeypatch.setattr(main, "all_collections", lambda: [])
    service = main.Service()
    with TestClient(service) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/agents").status_code == 200
    # Tudo que mexe no schema (migração, seeds, o AgentOS e as tabelas do Agno) e os workers
    # sob o lock, uma réplica de cada vez; o lock é solto quando a subida termina.
    assert ordem == [
        "check_startup_auth",
        "lock",
        "upgrade_database",
        "seed_default_collection",
        "seed_default_tools",
        "seed_default_agents",
        "build_app",
        "scheduler",
        "jobs_worker",
        "unlock",
        "warm_up",
    ]
    assert service.app is None  # desligado, volta ao 503


def test_sem_chave_a_subida_falha_antes_de_tocar_no_banco(ordem, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_api_key", None)
    monkeypatch.setattr(get_settings(), "runtime_api_key", None)
    monkeypatch.setattr(get_settings(), "allow_open", False)
    with pytest.raises(auth.OpenServiceRefused, match="ALLOW_OPEN=1"):
        with TestClient(main.Service()):
            pass
    assert ordem == ["check_startup_auth"]


# -- o lock de migração -------------------------------------------------------------


def _engine(dialeto: str, *, livre: bool = True, falha_unlock: bool = False):
    engine = MagicMock()
    engine.dialect.name = dialeto
    conexao = engine.connect.return_value.execution_options.return_value
    sqls: list[str] = []

    def execute(sql, params=None):
        sqls.append(str(sql))
        assert params == {"k": migrations.MIGRATION_LOCK_KEY}
        if "unlock" in str(sql) and falha_unlock:
            raise RuntimeError("conexão caiu")
        resultado = MagicMock()
        resultado.scalar.return_value = livre
        return resultado

    conexao.execute.side_effect = execute
    return engine, conexao, sqls


def test_lock_so_no_postgres():
    engine, _, sqls = _engine("sqlite")
    with migrations.migration_lock(engine):
        pass
    engine.connect.assert_not_called()
    assert sqls == []


def test_lock_no_postgres_pega_e_solta_uma_vez(caplog):
    engine, conexao, sqls = _engine("postgresql")
    with caplog.at_level("INFO", logger="agent_service.migrations"):
        with migrations.migration_lock(engine):
            assert sqls == ["SELECT pg_try_advisory_lock(:k)"]
    engine.connect.return_value.execution_options.assert_called_once_with(isolation_level="AUTOCOMMIT")
    assert sqls == ["SELECT pg_try_advisory_lock(:k)", "SELECT pg_advisory_unlock(:k)"]
    assert "Esperando" not in caplog.text
    conexao.close.assert_called_once()


def test_lock_ocupado_espera_a_outra_instancia(caplog):
    engine, _, sqls = _engine("postgresql", livre=False)
    with caplog.at_level("INFO", logger="agent_service.migrations"):
        with migrations.migration_lock(engine):
            pass
    assert sqls == [
        "SELECT pg_try_advisory_lock(:k)",
        "SELECT pg_advisory_lock(:k)",
        "SELECT pg_advisory_unlock(:k)",
    ]
    assert "Esperando outra instância" in caplog.text


def test_lock_e_solto_mesmo_se_a_migracao_falha():
    engine, conexao, sqls = _engine("postgresql")
    with pytest.raises(ValueError):
        with migrations.migration_lock(engine):
            raise ValueError("migração quebrou")
    assert sqls[-1] == "SELECT pg_advisory_unlock(:k)"
    assert sqls.count("SELECT pg_advisory_unlock(:k)") == 1
    conexao.close.assert_called_once()


def test_unlock_falhando_descarta_a_conexao():
    """Voltar ao pool ainda com o lock travaria as outras réplicas."""
    engine, conexao, _ = _engine("postgresql", falha_unlock=True)
    with migrations.migration_lock(engine):
        pass
    conexao.invalidate.assert_called_once()
    conexao.close.assert_called_once()


def test_kuro_migrate_usa_o_mesmo_lock(monkeypatch):
    eventos: list[str] = []

    @contextmanager
    def lock(_engine):
        eventos.append("lock")
        yield
        eventos.append("unlock")

    monkeypatch.setattr(migrations, "migration_lock", lock)
    monkeypatch.setattr(migrations, "upgrade_database", lambda engine=None: eventos.append("upgrade"))
    migrations.main()
    assert eventos == ["lock", "upgrade", "unlock"]
