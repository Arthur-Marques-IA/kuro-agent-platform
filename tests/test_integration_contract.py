"""Contrato de integração: o que outro módulo precisa mandar para falar com o agente.

O ponto do endpoint é ser derivado — as dependências obrigatórias saem da
definição do agente e das tools que as consomem, não de um exemplo escrito à mão.
"""

import pytest
from fastapi import HTTPException

from agent_service.api.agents_routes import AgentDefinitionIn, create_agent, delete_agent
from agent_service.api.integration_routes import get_integration_contract
from agent_service.api.tools_routes import ToolIn, create_tool, delete_tool

TOOL_CONFIG = {
    "method": "GET",
    "url": "https://exemplo.test/clientes/{cpf}",
    "parameters": [
        {"name": "cpf", "type": "string", "location": "path", "required": True,
         "source": "dependency", "dependency": "cpf"},
    ],
}


@pytest.fixture
def agente():
    create_tool(ToolIn(tool_name="ficha_int", kind="api", label="Ficha", config=TOOL_CONFIG))
    create_agent(
        AgentDefinitionIn(
            agent_type="agente_int",
            name="Integração",
            instructions=["Ajude."],
            tools=["ficha_int"],
            dependency_fields=[
                {"name": "cpf", "type": "string", "required": True, "description": "CPF do cliente"},
                {"name": "vip", "type": "boolean", "required": False},
            ],
        )
    )
    yield "agente_int"
    delete_agent("agente_int")
    delete_tool("ficha_int")


def test_contract_says_who_needs_each_dependency(agente):
    contrato = get_integration_contract(agente, base_url="https://api.exemplo.com")

    cpf = next(d for d in contrato.dependencies if d.name == "cpf")
    assert cpf.required is True
    assert cpf.required_by_tools == ["ficha_int"], "o campo é exigido pela tool, não só declarado"
    assert contrato.warnings == []


def test_example_and_urls_use_the_given_base_url(agente):
    contrato = get_integration_contract(agente, base_url="https://api.exemplo.com/")

    assert contrato.chat_url == "https://api.exemplo.com/chat"
    assert contrato.stream_url == "https://api.exemplo.com/chat/stream"
    assert contrato.request_example["dependencies"]["cpf"] == "texto"
    assert "curl -X POST https://api.exemplo.com/chat" in contrato.curl
    assert '"agent_type": "agente_int"' in contrato.curl


def test_unknown_agent_is_404():
    with pytest.raises(HTTPException) as exc:
        get_integration_contract("nao-existe", base_url="http://x")
    assert exc.value.status_code == 404


# -- URL pública ----------------------------------------------------------------


@pytest.fixture
def sem_url_publica(monkeypatch, tmp_path):
    for var in ("KURO_PUBLIC_URL", "KURO_API_DOMAIN", "AGENT_SERVICE_BIND"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KURO_PUBLIC_DIR", str(tmp_path / "sem-profile-ip"))
    return monkeypatch


def test_sem_base_url_usa_o_dominio_publico_do_servidor(agente, sem_url_publica):
    """Pedido de dentro do container (MCP, CLI no `compose exec`), o cliente só conhece
    `localhost:8000`: o contrato saía com essa URL, que não serve para quem integra."""
    sem_url_publica.setenv("KURO_API_DOMAIN", "203-0-113-10.sslip.io")

    contrato = get_integration_contract(agente, base_url=None)

    assert contrato.base_url == "https://203-0-113-10.sslip.io"
    assert contrato.base_url_source == "domain"
    assert contrato.chat_url == "https://203-0-113-10.sslip.io/chat"
    assert contrato.openapi_url == "https://203-0-113-10.sslip.io/integration/openapi.json"
    assert "localhost" not in contrato.curl


def test_kuro_public_url_vence_o_dominio(agente, sem_url_publica):
    sem_url_publica.setenv("KURO_API_DOMAIN", "kuro.exemplo.com")
    sem_url_publica.setenv("KURO_PUBLIC_URL", "https://api.exemplo.com/kuro/")

    contrato = get_integration_contract(agente, base_url=None)

    assert contrato.base_url == "https://api.exemplo.com/kuro"
    assert contrato.base_url_source == "explicit"


def test_sem_nada_configurado_e_o_host_na_porta_publicada(agente, sem_url_publica):
    sem_url_publica.setenv("AGENT_SERVICE_BIND", "127.0.0.1:59000")

    contrato = get_integration_contract(agente, base_url=None)

    assert contrato.base_url == "http://127.0.0.1:59000"
    assert contrato.base_url_source == "local"
    assert contrato.warnings == []


def test_cliente_de_dentro_do_container_nao_manda_o_proprio_endereco():
    """A CLI/MCP no container fala com `localhost:8000`: mandar isso como `base_url` venceria
    a URL pública do servidor."""
    import httpx

    from agent_service.cli.client import Client

    pedidos: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pedidos.append(request.url)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    Client("http://localhost:8000", transport=transport).integration("a")
    Client("https://kuro.exemplo.com", transport=transport).integration("a")
    assert "base_url" not in pedidos[0].params
    assert pedidos[1].params["base_url"] == "https://kuro.exemplo.com"


# -- o que quem integra precisa além do corpo ------------------------------------


def test_contrato_traz_resposta_erros_eventos_e_campos(agente):
    contrato = get_integration_contract(agente, base_url="https://api.exemplo.com")

    assert contrato.request_example["metadata"] == {"conversation_id": "98231"}
    assert contrato.request_example["dry_run"] is False
    nomes = {f.name for f in contrato.request_fields}
    assert {"metadata", "dry_run", "attachments", "dependencies", "session_id"} <= nomes
    assert {"run_id", "agent_version", "config_hash", "sources", "content"} <= set(contrato.response_example)
    assert [e.event for e in contrato.stream_events] == ["run", "message", "usage", "state", "sources", "error", "done"]
    assert any(e.status == "409" for e in contrato.errors)


def test_analista_tem_resposta_com_result_e_sem_stream():
    create_agent(
        AgentDefinitionIn(
            agent_type="analista_int",
            name="Analista",
            instructions=["Extraia."],
            kind="analysis",
            response_schema=[
                {"name": "acao", "type": "string", "required": True, "enum": ["aprovar", "negar"]},
                {"name": "parcelas", "type": "array", "required": False,
                 "items": {"type": "object", "fields": [{"name": "valor", "type": "number", "required": True}]}},
            ],
        )
    )
    try:
        contrato = get_integration_contract("analista_int", base_url="https://api.exemplo.com")
    finally:
        delete_agent("analista_int")

    assert contrato.stream_url is None and contrato.stream_events == []
    assert contrato.response_example["result"] == {"acao": "aprovar", "parcelas": [{"valor": 1.5}]}
    assert "document" in {f.name for f in contrato.request_fields}


# -- OpenAPI de quem integra -----------------------------------------------------


def test_openapi_runtime_so_tem_rotas_de_runtime_e_os_schemas_delas():
    from agent_service.api.integration_routes import runtime_openapi

    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/chat": {"post": {"requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/ChatRequest"}}}}}},
            "/agents/{agent_type}/procedures/{session_id}": {"get": {}},
            "/agents/{agent_type}/procedures": {"get": {}},
            "/agents": {"post": {"requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/AgentIn"}}}}}},
            "/health": {"get": {}},
        },
        "components": {"schemas": {
            "ChatRequest": {"properties": {"attachments": {"items": {"$ref": "#/components/schemas/AttachmentIn"}}}},
            "AttachmentIn": {"properties": {}},
            "AgentIn": {"properties": {}},
        }},
    }

    filtrado = runtime_openapi(spec)

    assert set(filtrado["paths"]) == {"/chat", "/agents/{agent_type}/procedures/{session_id}", "/health"}
    assert set(filtrado["components"]["schemas"]) == {"ChatRequest", "AttachmentIn"}


def test_rota_do_openapi_runtime_abre_com_a_chave_runtime(monkeypatch):
    """A rota montada de verdade: middleware de auth, `request.app.openapi()` e o filtro."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent_service.api import auth
    from agent_service.api.integration_routes import openapi_router
    from agent_service.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_api_key", "adm")
    monkeypatch.setattr(get_settings(), "runtime_api_key", "rt-nova,rt-antiga")
    app = FastAPI()
    app.include_router(openapi_router)

    @app.post("/chat")
    def chat() -> dict:
        return {}

    @app.get("/agents")
    def agents() -> list:
        return []

    auth.install(app)
    cliente = TestClient(app)

    assert cliente.get("/integration/openapi.json").status_code == 401
    resposta = cliente.get("/integration/openapi.json", headers={"Authorization": "Bearer rt-antiga"})
    assert resposta.status_code == 200
    assert "/chat" in resposta.json()["paths"] and "/agents" not in resposta.json()["paths"]


# -- OpenAPI por agente --------------------------------------------------------------


def _app_de_runtime(monkeypatch):
    """As rotas de verdade (`/chat`, `/analyze`, scores, o OpenAPI) com a auth ligada."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent_service.api import auth
    from agent_service.api.integration_routes import openapi_router
    from agent_service.api.integration_routes import router as integration_router
    from agent_service.api.observability_routes import router as observability_router
    from agent_service.api.routes import router
    from agent_service.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_api_key", "adm")
    monkeypatch.setattr(get_settings(), "runtime_api_key", "rt")
    monkeypatch.setenv("KURO_PUBLIC_URL", "https://kuro.exemplo.com")
    app = FastAPI()
    for r in (router, integration_router, openapi_router, observability_router):
        app.include_router(r)
    auth.install(app)
    return TestClient(app)


def _refs_resolvem(doc: dict) -> None:
    from agent_service.api.integration_routes import _refs

    citados: set[str] = set()
    _refs(doc, citados)
    assert citados <= set(doc["components"]["schemas"]), citados - set(doc["components"]["schemas"])


@pytest.fixture
def analista():
    create_agent(
        AgentDefinitionIn(
            agent_type="auditor-int",
            name="Auditor",
            instructions=["Audite."],
            kind="analysis",
            dependency_fields=[{"name": "canal", "type": "string", "required": True, "enum": ["whatsapp", "email"]}],
            response_schema=[
                {"name": "acao", "type": "string", "required": True, "enum": ["responder", "escalar"]},
                {"name": "parcelas", "type": "array", "items": {"type": "object", "fields": [
                    {"name": "valor", "type": "number", "required": True}]}},
            ],
        )
    )
    yield "auditor-int"
    delete_agent("auditor-int")


def test_openapi_do_agente_abre_com_a_runtime_e_o_contrato_nao(agente, monkeypatch):
    cliente = _app_de_runtime(monkeypatch)
    runtime = {"Authorization": "Bearer rt"}
    assert cliente.get(f"/agents/{agente}/openapi.json").status_code == 401
    assert cliente.get(f"/agents/{agente}/openapi.json", headers=runtime).status_code == 200
    # O contrato (com tools e avisos) e a definição continuam só para admin.
    assert cliente.get(f"/agents/{agente}/integration", headers=runtime).status_code == 403
    assert cliente.get("/agents/nao-existe/openapi.json", headers=runtime).status_code == 404


def test_openapi_do_conversacional_tem_agent_type_fixo_e_dependencies_tipadas(agente, monkeypatch):
    doc = _app_de_runtime(monkeypatch).get(f"/agents/{agente}/openapi.json", headers={"Authorization": "Bearer rt"}).json()

    assert set(doc["paths"]) == {"/chat", "/chat/stream", "/observability/scores", "/observability/references"}
    assert doc["servers"] == [{"url": "https://kuro.exemplo.com"}]
    assert doc["security"] == [{"runtime": []}]
    pedido = doc["components"]["schemas"]["AgenteIntChatRequest"]
    assert pedido["properties"]["agent_type"]["enum"] == ["agente_int"]
    deps = pedido["properties"]["dependencies"]
    assert deps["properties"]["cpf"]["type"] == "string" and deps["required"] == ["cpf"]
    assert deps["properties"]["vip"]["type"] == "boolean"
    assert "dependencies" in pedido["required"]
    chat = doc["paths"]["/chat"]["post"]
    assert "409" in chat["responses"]
    assert any(p["name"] == "Idempotency-Key" for p in chat.get("parameters", []))
    assert "ChatRequest" not in doc["components"]["schemas"]  # o genérico saiu
    _refs_resolvem(doc)
    # Nada da configuração interna do agente.
    texto = str(doc)
    assert "Ajude." not in texto and "ficha_int" not in texto


def test_openapi_do_analista_tipa_o_result(analista, monkeypatch):
    doc = _app_de_runtime(monkeypatch).get(f"/agents/{analista}/openapi.json", headers={"Authorization": "Bearer rt"}).json()

    assert set(doc["paths"]) == {"/analyze", "/observability/scores", "/observability/references"}
    pedido = doc["components"]["schemas"]["AuditorIntAnalyzeRequest"]
    assert pedido["properties"]["dependencies"]["properties"]["canal"]["enum"] == ["whatsapp", "email"]
    resultado = doc["components"]["schemas"]["AuditorIntAnalyzeResponse"]["properties"]["result"]
    assert resultado["properties"]["acao"]["enum"] == ["responder", "escalar"]
    assert resultado["properties"]["parcelas"]["items"]["properties"]["valor"]["type"] == "number"
    assert resultado["required"] == ["acao"]
    _refs_resolvem(doc)


def test_contrato_aponta_para_o_openapi_do_agente(agente):
    contrato = get_integration_contract(agente, base_url="https://api.exemplo.com")
    assert contrato.agent_openapi_url == "https://api.exemplo.com/agents/agente_int/openapi.json"
