"""Segredos de tools `kind="api"` cifrados em repouso (`tools/crypto.py`).

O banco guarda `enc:v1:<Fernet>` em `auth.token/value/password` e nos headers de
credencial; a API continua mascarando; a chamada decifra; texto plano legado
continua funcionando e regravar cifra; a migração 0017 cifra o que existe.
"""

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from cryptography.fernet import Fernet

from agent_service.agents.store import create_definition, delete_definition, get_definition
from agent_service.agents.versions import effective_config
from agent_service.api.tools_routes import (
    ToolIn,
    ToolInvokeIn,
    ToolUpdateIn,
    create_tool,
    delete_tool,
    get_tool,
    invoke_tool,
    update_tool,
)
from agent_service.config import get_settings
from agent_service.db import get_db
from agent_service.migrations import _config
from agent_service.models import crypto
from agent_service.tools import api_tool

PREFIX = "enc:v1:"


@pytest.fixture
def chave(monkeypatch):
    """Troca a `CREDENTIALS_ENCRYPTION_KEY` no meio do teste (e desfaz no fim)."""

    def trocar(valor):
        monkeypatch.setattr(get_settings(), "credentials_encryption_key", valor)
        crypto.reset_cache()

    yield trocar
    monkeypatch.undo()
    crypto.reset_cache()


@pytest.fixture
def rede():
    enviadas: list[httpx.Request] = []

    def handler(request):
        enviadas.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.object(api_tool, "_client", client):
        yield enviadas


def _raw(tool_name: str) -> str:
    """A coluna `config` como está no banco — sem passar pelo store."""
    with get_db().db_engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT config FROM tool_definitions WHERE tool_name = :n"), {"n": tool_name}
        ).scalar_one()


def _config_raw(tool_name: str) -> dict:
    raw = _raw(tool_name)
    return json.loads(raw) if isinstance(raw, str) else raw


def _invocar(tool_name: str) -> dict:
    return asyncio.run(invoke_tool(tool_name, ToolInvokeIn()))


CONFIG = {
    "method": "GET",
    "url": "https://exemplo.test/x",
    "auth": {"type": "bearer", "token": "segredo-real-123"},
    "headers": {"X-Api-Key": "chave-do-header", "Accept": "application/json"},
}


@pytest.fixture
def tool_cifrada():
    create_tool(ToolIn(tool_name="sec_api", kind="api", label="Sec", config=CONFIG))
    yield "sec_api"
    delete_tool("sec_api")


def test_grava_cifrado_e_a_api_continua_mascarando(tool_cifrada):
    raw = _raw(tool_cifrada)
    assert "segredo-real-123" not in raw and "chave-do-header" not in raw

    config = _config_raw(tool_cifrada)
    assert config["auth"]["token"].startswith(PREFIX)
    assert config["headers"]["X-Api-Key"].startswith(PREFIX)
    # O que não é segredo fica legível: valor a valor, não o objeto inteiro.
    assert config["headers"]["Accept"] == "application/json"
    assert config["auth"]["type"] == "bearer" and config["url"] == CONFIG["url"]

    lida = get_tool(tool_cifrada)
    assert lida["secrets_at_rest"] == "encrypted"
    assert lida["config"]["auth"]["token"] == "••••••••"
    assert lida["config"]["headers"]["X-Api-Key"] == "••••••••"
    assert PREFIX not in json.dumps(lida["config"])


def test_chamada_usa_o_valor_decifrado(tool_cifrada, rede):
    out = _invocar(tool_cifrada)
    assert out["ok"] is True
    (req,) = rede
    assert req.headers["Authorization"] == "Bearer segredo-real-123"
    assert req.headers["X-Api-Key"] == "chave-do-header"


def test_editar_sem_reenviar_o_segredo_preserva_o_cifrado(tool_cifrada, rede):
    antes = _config_raw(tool_cifrada)
    eco = get_tool(tool_cifrada)["config"]  # mascarado, como o `--editable`
    update_tool(tool_cifrada, ToolUpdateIn(config={**eco, "url": "https://exemplo.test/y"}))

    depois = _config_raw(tool_cifrada)
    assert depois["auth"]["token"] == antes["auth"]["token"]  # o mesmo cifrado, não cifrado de novo
    assert depois["headers"]["X-Api-Key"] == antes["headers"]["X-Api-Key"]
    assert depois["url"] == "https://exemplo.test/y"
    assert _invocar(tool_cifrada)["ok"] is True
    assert rede[-1].headers["Authorization"] == "Bearer segredo-real-123"


def test_trocar_o_segredo_grava_o_novo_cifrado(tool_cifrada, rede):
    update_tool(tool_cifrada, ToolUpdateIn(config={**CONFIG, "auth": {"type": "bearer", "token": "novo-456"}}))
    assert "novo-456" not in _raw(tool_cifrada)
    _invocar(tool_cifrada)
    assert rede[-1].headers["Authorization"] == "Bearer novo-456"


def test_legado_em_texto_plano_funciona_e_regravar_cifra(rede):
    with get_db().db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tool_definitions (tool_name, kind, label, config, enabled, is_seed) "
                "VALUES ('legado_api', 'api', 'Legado', :c, 1, 0)"
            ),
            {"c": json.dumps(CONFIG)},
        )
    try:
        assert get_tool("legado_api")["secrets_at_rest"] == "plaintext"
        assert _invocar("legado_api")["ok"] is True
        assert rede[-1].headers["Authorization"] == "Bearer segredo-real-123"

        # `kuro tools get --editable | kuro tools apply -f -`: o eco da máscara regrava cifrado.
        update_tool("legado_api", ToolUpdateIn(config=get_tool("legado_api")["config"]))
        assert "segredo-real-123" not in _raw("legado_api")
        assert get_tool("legado_api")["secrets_at_rest"] == "encrypted"
        assert _invocar("legado_api")["ok"] is True
        assert rede[-1].headers["Authorization"] == "Bearer segredo-real-123"
    finally:
        delete_tool("legado_api")


def test_chave_trocada_devolve_erro_claro_sem_chamar_nem_vazar(tool_cifrada, rede, chave):
    cifrado = _config_raw(tool_cifrada)["auth"]["token"]
    chave(Fernet.generate_key().decode())

    out = _invocar(tool_cifrada)
    assert out["ok"] is False
    assert out["failure"] == "config"
    assert "CREDENTIALS_ENCRYPTION_KEY" in out["result"]
    assert PREFIX not in out["result"] and cifrado[len(PREFIX) :] not in out["result"]
    assert rede == []  # o destino não recebeu o cifrado como token

    # A leitura pela API não quebra e não mostra o cifrado.
    lida = get_tool(tool_cifrada)
    assert lida["config"]["auth"]["token"] == "••••••••"


def test_sem_chave_grava_em_texto_plano_e_funciona(chave, rede):
    chave(None)
    create_tool(ToolIn(tool_name="sem_chave_api", kind="api", label="Sem chave", config=CONFIG))
    try:
        assert _config_raw("sem_chave_api")["auth"]["token"] == "segredo-real-123"
        assert get_tool("sem_chave_api")["secrets_at_rest"] == "plaintext"
        assert _invocar("sem_chave_api")["ok"] is True
        assert rede[-1].headers["Authorization"] == "Bearer segredo-real-123"
    finally:
        delete_tool("sem_chave_api")


def test_cifrar_nao_cria_versao_nova_do_agente(tool_cifrada):
    create_definition(agent_type="usa-sec", name="Usa sec", instructions=["oi"], tools=[tool_cifrada])
    try:
        antes = effective_config(get_definition("usa-sec"))["tools"]
        # Regravar o mesmo segredo muda o cifrado (IV aleatório), não a configuração.
        update_tool(tool_cifrada, ToolUpdateIn(config=CONFIG))
        assert effective_config(get_definition("usa-sec"))["tools"] == antes
    finally:
        delete_definition("usa-sec")


# -- migração 0017 ------------------------------------------------------------


def _migrar(engine: sa.Engine, alvo: str, *, descer: bool = False) -> None:
    config = _config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if descer else command.upgrade)(config, alvo)


def _banco_com_tool_legada(tmp_path) -> sa.Engine:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'tools.db'}")
    _migrar(engine, "0016")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tool_definitions (tool_name, kind, label, config, enabled, is_seed) "
                "VALUES ('velha', 'api', 'Velha', :c, 1, 0), ('calc', 'builtin', 'Calc', :b, 1, 0)"
            ),
            {"c": json.dumps(CONFIG), "b": json.dumps({"builtin_id": "calculator", "params": {}})},
        )
    return engine


def _config_de(engine: sa.Engine, nome: str = "velha") -> dict:
    with engine.connect() as conn:
        raw = conn.execute(
            sa.text("SELECT config FROM tool_definitions WHERE tool_name = :n"), {"n": nome}
        ).scalar_one()
    return json.loads(raw)


def test_migracao_cifra_o_existente_e_o_downgrade_decifra(tmp_path):
    engine = _banco_com_tool_legada(tmp_path)

    _migrar(engine, "head")
    config = _config_de(engine)
    assert config["auth"]["token"].startswith(PREFIX)
    assert config["headers"]["X-Api-Key"].startswith(PREFIX)
    assert config["headers"]["Accept"] == "application/json"
    assert crypto.decrypt_secret(config["auth"]["token"][len(PREFIX) :]) == "segredo-real-123"
    assert _config_de(engine, "calc") == {"builtin_id": "calculator", "params": {}}

    _migrar(engine, "head")  # idempotente: não cifra de novo
    assert _config_de(engine) == config

    _migrar(engine, "0016", descer=True)
    assert _config_de(engine) == CONFIG


def test_migracao_sem_chave_nao_falha_e_deixa_em_texto_plano(tmp_path, chave):
    engine = _banco_com_tool_legada(tmp_path)
    chave(None)

    _migrar(engine, "head")  # sobe mesmo assim
    assert _config_de(engine) == CONFIG

    chave("nao-e-uma-chave-fernet")
    _migrar(engine, "0016", descer=True)  # nada cifrado: desce sem a chave
    _migrar(engine, "head")
    assert _config_de(engine) == CONFIG


def test_downgrade_sem_chave_com_segredo_cifrado_para_com_erro(tmp_path, chave):
    engine = _banco_com_tool_legada(tmp_path)
    _migrar(engine, "head")
    chave(None)
    with pytest.raises(RuntimeError, match="CREDENTIALS_ENCRYPTION_KEY"):
        _migrar(engine, "0016", descer=True)
