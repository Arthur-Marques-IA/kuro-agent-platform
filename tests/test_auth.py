"""Quem pode chamar o quê.

O serviço nasceu sem autenticação nenhuma: quem alcançasse a porta lia toda
conversa que passou por ele, reescrevia prompt de produção e lia as credenciais
de modelo. Estes testes fixam as três coisas que a trava precisa garantir — a
separação dos escopos, o fecho das rotas do AgentOS (que não passam pelos
routers deste repo) e o modo aberto de quem roda local sem chave.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_service.api import auth
from agent_service.config import get_settings

ADMIN = "chave-admin"
RUNTIME = "chave-runtime"


@pytest.fixture
def app_com_auth(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_api_key", ADMIN)
    monkeypatch.setattr(get_settings(), "runtime_api_key", RUNTIME)
    return _app()


@pytest.fixture
def app_sem_chaves(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_api_key", None)
    monkeypatch.setattr(get_settings(), "runtime_api_key", None)
    return _app()


def _app() -> TestClient:
    """Um app mínimo com o middleware: o alvo do teste é a trava, não as rotas."""
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/chat")
    def chat():
        return {"ok": True}

    @app.get("/agents")
    def agents():
        return []

    @app.get("/sessions")
    def sessions():
        """Rota do AgentOS: não passa por router nenhum deste repo, e é onde a
        conversa está. Uma dependência por router deixaria justo esta de fora."""
        return []

    auth.install(app)
    return TestClient(app)


def _com(chave: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {chave}"}


# -- escopos ----------------------------------------------------------------------


def test_sem_chave_a_rota_de_execucao_responde_401(app_com_auth):
    resposta = app_com_auth.post("/chat")
    assert resposta.status_code == 401
    assert "Authorization" in resposta.json()["detail"]


def test_chave_runtime_executa_mas_nao_administra(app_com_auth):
    """É a chave que vai para o outro módulo: se vazar, gasta token — não lê
    conversa alheia nem troca prompt."""
    assert app_com_auth.post("/chat", headers=_com(RUNTIME)).status_code == 200
    assert app_com_auth.get("/agents", headers=_com(RUNTIME)).status_code == 403
    assert app_com_auth.get("/sessions", headers=_com(RUNTIME)).status_code == 403


def test_chave_admin_alcanca_tudo(app_com_auth):
    for caminho in ("/agents", "/sessions"):
        assert app_com_auth.get(caminho, headers=_com(ADMIN)).status_code == 200
    assert app_com_auth.post("/chat", headers=_com(ADMIN)).status_code == 200


def test_rotas_do_agentos_tambem_ficam_fechadas(app_com_auth):
    """O motivo de a trava ser middleware e não `Depends` por router."""
    assert app_com_auth.get("/sessions").status_code == 401


def test_chave_errada_e_403(app_com_auth):
    assert app_com_auth.get("/agents", headers=_com("chave-inventada")).status_code == 403


def test_x_api_key_tambem_serve(app_com_auth):
    assert app_com_auth.post("/chat", headers={"X-API-Key": RUNTIME}).status_code == 200


def test_health_fica_aberta(app_com_auth):
    """É o healthcheck do container e de qualquer load balancer na frente."""
    assert app_com_auth.get("/health").status_code == 200


# -- modo aberto -------------------------------------------------------------------


def test_sem_chave_configurada_o_servico_segue_aberto(app_sem_chaves):
    """Comportamento de antes, para não quebrar quem roda local — o alerta fica
    em `/health` e no `kuro health`."""
    assert app_sem_chaves.get("/agents").status_code == 200
    assert not auth.auth_enabled()


def test_health_denuncia_que_esta_aberto(app_sem_chaves):
    from agent_service.api.routes import health

    assert health()["auth"] == "disabled"


def test_health_confirma_quando_esta_fechado(app_com_auth):
    from agent_service.api.routes import health

    assert health()["auth"] == "enabled"


# -- detalhes que quebram integração de terceiro ----------------------------------


def test_barra_no_fim_nao_muda_o_escopo(app_com_auth):
    """O middleware roda antes do roteamento, então o redirect de `/chat/` para
    `/chat` ainda não aconteceu. Sem normalizar, um cliente que põe barra no fim
    levaria 403 com a chave certa."""
    assert app_com_auth.post("/chat/", headers=_com(RUNTIME)).status_code in (200, 307)
    assert auth.required_scope("/chat/") == "runtime"


def test_openapi_nao_fica_publico_com_auth_ligada(app_com_auth):
    """Publicar a superfície inteira da API para quem alcança a porta é entregar
    o mapa antes da fechadura."""
    assert auth.required_scope("/openapi.json") == "admin"
    assert auth.required_scope("/docs") == "admin"
    assert app_com_auth.get("/openapi.json").status_code == 401
    assert app_com_auth.get("/openapi.json", headers=_com(ADMIN)).status_code == 200


def test_estado_de_conversa_procedural_aceita_runtime_e_o_resto_de_agents_nao():
    """Quem integra lê o `state` de uma sessão que ele mesmo conduz; o funil e a
    definição do agente continuam só para admin."""
    assert auth.required_scope("/agents/abertura/procedures/sessao-1") == "runtime"
    assert auth.required_scope("/agents/abertura/procedures/sessao-1/") == "runtime"
    assert auth.required_scope("/agents/abertura/procedures") == "admin"
    assert auth.required_scope("/agents/abertura") == "admin"
    assert auth.required_scope("/agents/abertura/procedures/sessao-1/x") == "admin"


def test_health_continua_aberta_por_necessidade(app_com_auth):
    assert auth.required_scope("/health") is None
    assert auth.required_scope("/health/") is None


def test_panorama_dos_logs_exige_admin():
    """Expõe custo e falhas de todos os agentes: nada de rota aberta por esquecimento."""
    assert auth.required_scope("/observability/overview") == "admin"


# -- rotação: mais de uma chave por escopo ---------------------------------------


def test_lista_de_chaves_vale_a_nova_e_a_antiga_juntas(monkeypatch):
    """Rotacionar sem derrubar ninguém: `nova,antiga` aceita as duas até os
    clientes trocarem; tirar a antiga da lista a revoga."""
    monkeypatch.setattr(get_settings(), "admin_api_key", ADMIN)
    monkeypatch.setattr(get_settings(), "runtime_api_key", " runtime-nova , runtime-antiga ,")
    cliente = _app()
    assert cliente.post("/chat", headers=_com("runtime-nova")).status_code == 200
    assert cliente.post("/chat", headers=_com("runtime-antiga")).status_code == 200
    assert cliente.get("/agents", headers=_com("runtime-antiga")).status_code == 403
    # A lista inteira não é uma chave.
    assert cliente.post("/chat", headers=_com("runtime-nova,runtime-antiga")).status_code == 403

    monkeypatch.setattr(get_settings(), "runtime_api_key", "runtime-nova")
    assert cliente.post("/chat", headers=_com("runtime-antiga")).status_code == 403


def test_chave_com_acento_nao_derruba_a_comparacao(app_com_auth):
    """`compare_digest` com str só aceita ASCII; uma chave estranha no header vira 403, não 500."""
    assert app_com_auth.post("/chat", headers={"X-API-Key": "chave-çãé".encode()}).status_code == 403


def test_openapi_de_integracao_e_da_chave_runtime():
    assert auth.required_scope("/integration/openapi.json") == "runtime"
    assert auth.required_scope("/openapi.json") == "admin"


def test_cliente_manda_so_a_chave_atual_de_uma_lista():
    """No container o compose copia `ADMIN_API_KEY` para `KURO_API_KEY`; numa rotação ela é
    `nova,antiga`, e mandar a lista inteira daria 403 na CLI, no MCP e no console."""
    import httpx

    from agent_service.cli.client import Client

    vistos: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistos.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"status": "ok"})

    Client("http://x", transport=httpx.MockTransport(handler), api_key="nova, antiga").health()
    assert vistos == ["Bearer nova"]
