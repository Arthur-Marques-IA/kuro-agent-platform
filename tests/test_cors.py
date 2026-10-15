"""CORS: quais páginas, no navegador, podem ler as respostas da API (`CORS_ORIGINS`)."""

import pytest
from agno.os.utils import update_cors_middleware
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_service.api import auth, cors
from agent_service.config import get_settings

PREFLIGHT = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"}


def _app(monkeypatch, origens: list[str] | None) -> TestClient:
    """Como o `main.py` monta: o AgentOS põe o CORS (com a lista dele no lugar de uma vazia),
    o Kuro impõe a sua e a auth fica por fora de tudo."""
    monkeypatch.setattr(get_settings(), "admin_api_key", "adm")
    monkeypatch.setattr(get_settings(), "runtime_api_key", "rt")
    app = FastAPI()

    @app.post("/chat")
    def chat() -> dict:
        return {"ok": True}

    update_cors_middleware(app, origens or ["http://localhost:3000", "https://os.agno.com"])
    cors.enforce(app, origens)
    auth.install(app)
    return TestClient(app)


def test_padrao_e_a_lista_do_agentos():
    assert cors.parse_origins(None) is None
    assert cors.parse_origins("  ") is None


def test_lista_e_none():
    assert cors.parse_origins("https://painel.exemplo.com/, http://localhost:3000") == [
        "https://painel.exemplo.com",
        "http://localhost:3000",
    ]
    assert cors.parse_origins("none") == []


@pytest.mark.parametrize("valor", ["*", "https://a.com,*", "painel.exemplo.com", "https://a.com/caminho"])
def test_recusa_o_que_nao_e_origem(valor):
    with pytest.raises(cors.CorsConfigError):
        cors.parse_origins(valor)


def test_preflight_passa_pela_auth_e_recebe_os_headers(monkeypatch):
    """Com a auth ligada, o `OPTIONS` (que nunca leva a chave) dava 401: CORS quebrado para qualquer página."""
    cliente = _app(monkeypatch, ["https://painel.exemplo.com"])
    resposta = cliente.options("/chat", headers={"Origin": "https://painel.exemplo.com", **PREFLIGHT})
    assert resposta.status_code == 200
    assert resposta.headers["access-control-allow-origin"] == "https://painel.exemplo.com"
    # A chamada de verdade continua exigindo a chave.
    assert cliente.post("/chat", headers={"Origin": "https://painel.exemplo.com"}).status_code == 401
    assert cliente.post("/chat", headers={"Authorization": "Bearer rt"}).status_code == 200


def test_origem_fora_da_lista_nao_recebe_permissao(monkeypatch):
    cliente = _app(monkeypatch, ["https://painel.exemplo.com"])
    resposta = cliente.options("/chat", headers={"Origin": "https://os.agno.com", **PREFLIGHT})
    assert "access-control-allow-origin" not in resposta.headers


def test_none_fecha_para_todas_as_origens(monkeypatch):
    """O AgentOS troca uma lista vazia pela padrão dele; o `none` só vale porque o Kuro impõe depois."""
    cliente = _app(monkeypatch, [])
    resposta = cliente.options("/chat", headers={"Origin": "http://localhost:3000", **PREFLIGHT})
    assert "access-control-allow-origin" not in resposta.headers


def test_options_sem_origin_continua_exigindo_a_chave(monkeypatch):
    cliente = _app(monkeypatch, None)
    assert cliente.options("/chat").status_code == 401
