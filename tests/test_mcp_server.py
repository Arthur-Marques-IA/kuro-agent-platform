"""Servidor MCP (`kuro-mcp`) contra uma API falsa — cliente MCP em memória, sem rede."""

import json

import httpx
import pytest

pytest.importorskip("mcp")

from mcp import Client as McpClient  # noqa: E402
from mcp_types import ElicitResult  # noqa: E402

from agent_service.cli.client import Client  # noqa: E402
from agent_service.mcp_server import build_server  # noqa: E402

AGENT = {
    "agent_type": "suporte",
    "name": "Suporte",
    "kind": "conversational",
    "instructions": ["Seja breve."],
    "tools": [],
    "model_provider": None,
    "model_id": None,
    "dependency_fields": [
        {"name": "cpf", "type": "string", "label": "CPF", "description": "", "required": True, "default": None}
    ],
    "num_history_runs": 10,
    "is_seed": False,
    "prompt_version": 2,
}

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# As duas gerações do protocolo: `legacy` (elicitation no meio da chamada) e `auto`
# (>= 2026-07-28: InputRequiredResult e nova tentativa). A confirmação tem de valer nas duas.
MODES = pytest.mark.parametrize("mode", ["legacy", "auto"])


@pytest.fixture
def api():
    calls: list[httpx.Request] = []
    routes: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        response = routes.get((request.method, request.url.path))
        if callable(response):  # rota com estado: responde diferente a cada chamada
            response = response(request)
        return response if response is not None else httpx.Response(404, json={"detail": "não encontrado"})

    client = Client("http://kuro.test", transport=httpx.MockTransport(handler))
    return build_server(client), routes, calls


def _answer(action: str, confirmar: bool = True):
    asked: list[str] = []

    async def callback(context, params):
        asked.append(params.message)
        return ElicitResult(action=action, content={"confirmar": confirmar} if action == "accept" else None)

    return callback, asked


def _data(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


def _mutations(calls: list[httpx.Request]) -> list[str]:
    return [f"{r.method} {r.url.path}" for r in calls if r.method != "GET"]


async def test_lists_tools_with_annotations_and_hides_confirmation_from_schema(api):
    server, _, _ = api
    async with McpClient(server) as mcp:
        tools = {t.name: t for t in (await mcp.list_tools()).tools}
    assert {"agents_list", "chat", "eval", "agent_delete", "agent_promote", "runs_list"} <= set(tools)
    # Não existe parâmetro que o modelo preencha para se autoconfirmar.
    assert set(tools["agent_delete"].input_schema["properties"]) == {"agent_type"}
    assert set(tools["agent_promote"].input_schema["properties"]) == {"source", "to"}
    assert tools["agents_list"].annotations.read_only_hint is True
    # `destructive_hint=False` significa "só acrescenta": quem sobrescreve ou apaga é True.
    for name in ("agent_delete", "agent_promote", "agent_set", "agent_apply", "tool_apply", "feedback_send"):
        assert tools[name].annotations.destructive_hint is True, name
    for name in ("run_score", "collection_add"):
        assert tools[name].annotations.destructive_hint is False, name
    # Coletar site sai para a web; ressincronizar também troca e remove páginas.
    assert tools["collection_add_site"].annotations.open_world_hint is True
    assert tools["collection_sync_site"].annotations.destructive_hint is True
    assert set(tools["collection_rm_site"].input_schema["properties"]) == {"name", "source_id"}
    # Chave de modelo não passa pelo contexto do modelo.
    assert not any(name.startswith("credential") and name != "credentials_list" for name in tools)


@MODES
async def test_delete_runs_only_after_user_confirms(api, mode):
    server, routes, calls = api
    routes[("DELETE", "/agents/suporte")] = httpx.Response(204)
    callback, asked = _answer("accept")
    async with McpClient(server, mode=mode, elicitation_callback=callback) as mcp:
        result = await mcp.call_tool("agent_delete", {"agent_type": "suporte"})
    assert not result.is_error, _text(result)
    assert _data(result) == {"deleted": "suporte"}
    assert len(asked) == 1 and "suporte" in asked[0]
    assert _mutations(calls) == ["DELETE /agents/suporte"]


@pytest.mark.parametrize("action", ["decline", "cancel"])
@MODES
async def test_delete_declined_sends_nothing(api, mode, action):
    server, routes, calls = api
    routes[("DELETE", "/agents/suporte")] = httpx.Response(204)
    callback, asked = _answer(action)
    async with McpClient(server, mode=mode, elicitation_callback=callback) as mcp:
        result = await mcp.call_tool("agent_delete", {"agent_type": "suporte"})
    assert len(asked) == 1
    assert result.is_error, "recusar não pode virar sucesso"
    assert _mutations(calls) == []


@MODES
async def test_delete_accepted_without_checkbox_is_cancelled(api, mode):
    server, routes, calls = api
    routes[("DELETE", "/agents/suporte")] = httpx.Response(204)
    callback, _ = _answer("accept", confirmar=False)
    async with McpClient(server, mode=mode, elicitation_callback=callback) as mcp:
        result = await mcp.call_tool("agent_delete", {"agent_type": "suporte"})
    assert _data(result) == {"cancelled": True}
    assert _mutations(calls) == []


@MODES
async def test_client_without_elicitation_cannot_delete(api, mode):
    server, routes, calls = api
    routes[("DELETE", "/agents/suporte")] = httpx.Response(204)
    async with McpClient(server, mode=mode) as mcp:
        try:
            result = await mcp.call_tool("agent_delete", {"agent_type": "suporte"})
            failure = _text(result) if result.is_error else None
        except Exception as exc:  # o SDK pode devolver como erro de protocolo
            failure = str(exc)
    assert failure is not None and "kuro agents delete suporte --yes" in failure
    assert _mutations(calls) == []


@MODES
async def test_promote_asks_and_posts(api, mode):
    server, routes, calls = api
    routes[("POST", "/agents/r8-draft/promote")] = httpx.Response(200, json={"unchanged": False, "agent_version": 4})
    callback, asked = _answer("accept")
    async with McpClient(server, mode=mode, elicitation_callback=callback) as mcp:
        result = await mcp.call_tool("agent_promote", {"source": "r8-draft", "to": "r8"})
    assert not result.is_error, _text(result)
    assert "'r8'" in asked[0]
    assert json.loads(calls[-1].content) == {"to": "r8"}


@MODES
async def test_rollback_to_identical_version_does_not_ask(api, mode):
    server, routes, calls = api
    routes[("GET", "/agents/suporte/versions")] = httpx.Response(
        200, json=[{"version": 1, "instructions": ["Seja breve."]}]
    )
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    callback, asked = _answer("accept")
    async with McpClient(server, mode=mode, elicitation_callback=callback) as mcp:
        result = await mcp.call_tool("agent_rollback", {"agent_type": "suporte", "version": 1})
    assert _data(result)["unchanged"] is True
    assert asked == []
    assert _mutations(calls) == []


@MODES
async def test_rollback_never_writes_without_asking_even_if_prompt_changed(api, mode):
    """O resolver viu o prompt igual e não perguntou; até o corpo rodar, alguém mudou o
    prompt. Gravar agora seria restaurar sem confirmação — tem de recusar."""
    server, routes, calls = api
    routes[("GET", "/agents/suporte/versions")] = httpx.Response(200, json=[{"version": 1, "instructions": ["Seja breve."]}])
    reads = {"n": 0}

    def agent(request):
        # 1ª leitura (resolver): prompt igual à v1. Depois (corpo da tool): já mudou.
        reads["n"] += 1
        return httpx.Response(200, json=AGENT if reads["n"] == 1 else {**AGENT, "instructions": ["Outro texto."]})

    routes[("GET", "/agents/suporte")] = agent
    routes[("PUT", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    callback, asked = _answer("accept")
    async with McpClient(server, mode=mode, elicitation_callback=callback) as mcp:
        result = await mcp.call_tool("agent_rollback", {"agent_type": "suporte", "version": 1})
    assert reads["n"] >= 2, "o cenário precisa que o corpo leia depois do resolver"
    assert asked == []
    assert result.is_error and "mudou" in _text(result)
    assert _mutations(calls) == []


@MODES
async def test_rollback_asks_then_restores(api, mode):
    server, routes, calls = api
    routes[("GET", "/agents/suporte/versions")] = httpx.Response(200, json=[{"version": 1, "instructions": ["Antigo."]}])
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    routes[("PUT", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "instructions": ["Antigo."], "prompt_version": 3})
    callback, asked = _answer("accept")
    async with McpClient(server, mode=mode, elicitation_callback=callback) as mcp:
        result = await mcp.call_tool("agent_rollback", {"agent_type": "suporte", "version": 1})
    assert not result.is_error, _text(result)
    assert len(asked) == 1 and "Antigo." in asked[0]
    assert _mutations(calls) == ["PUT /agents/suporte"]
    assert json.loads(calls[-1].content) == {"instructions": ["Antigo."]}


@MODES
async def test_rollback_unknown_version_explains(api, mode):
    server, routes, calls = api
    routes[("GET", "/agents/suporte/versions")] = httpx.Response(200, json=[{"version": 1, "instructions": ["a"]}])
    callback, asked = _answer("accept")
    async with McpClient(server, mode=mode, elicitation_callback=callback) as mcp:
        result = await mcp.call_tool("agent_rollback", {"agent_type": "suporte", "version": 7})
    assert result.is_error and "disponíveis: v1" in _text(result)
    assert asked == [] and _mutations(calls) == []


def _sse(*events: tuple[str, dict]) -> bytes:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


async def test_chat_is_dry_run_by_default_and_returns_session(api):
    server, routes, calls = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    routes[("POST", "/chat/stream")] = httpx.Response(
        200,
        content=_sse(("run", {"run_id": "r1", "trace_id": "t1"}), ("message", {"content": "Olá"}), ("done", {})),
        headers={"content-type": "text/event-stream"},
    )
    async with McpClient(server) as mcp:
        result = await mcp.call_tool("chat", {"agent_type": "suporte", "message": "oi", "dependencies": {"cpf": 123}})
    data = _data(result)
    assert data["content"] == "Olá" and data["run_id"] == "r1"
    assert data["session_id"].startswith("mcp-")
    body = json.loads(calls[-1].content)
    assert body["dry_run"] is True
    assert body["dependencies"] == {"cpf": "123"}  # convertido para o tipo declarado
    assert body["user_id"] == "mcp"


async def test_chat_missing_required_dependency(api):
    server, routes, calls = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    async with McpClient(server) as mcp:
        result = await mcp.call_tool("chat", {"agent_type": "suporte", "message": "oi"})
    assert result.is_error and "cpf" in _text(result)
    assert _mutations(calls) == []


async def test_apply_sends_only_changed_fields(api):
    server, routes, calls = api
    routes[("GET", "/agents")] = httpx.Response(200, json=[AGENT])
    routes[("PUT", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "num_history_runs": 5})
    async with McpClient(server) as mcp:
        result = await mcp.call_tool(
            "agent_apply",
            {"definition": {"agent_type": "suporte", "name": "Suporte", "num_history_runs": 5}},
        )
    data = _data(result)
    assert data["action"] == "update" and data["changed"] == ["num_history_runs"]
    assert json.loads(calls[-1].content) == {"num_history_runs": 5}


async def test_apply_rejects_non_editable_field(api):
    server, _, calls = api
    async with McpClient(server) as mcp:
        result = await mcp.call_tool("agent_apply", {"definition": {"agent_type": "x", "prompt_version": 3}})
    assert result.is_error and "prompt_version" in _text(result)
    assert calls == []


async def test_unauthorized_points_to_api_key(api):
    server, routes, _ = api
    routes[("GET", "/agents")] = httpx.Response(401, json={"detail": "chave de API ausente"})
    async with McpClient(server) as mcp:
        result = await mcp.call_tool("agents_list", {})
    assert result.is_error and "KURO_API_KEY" in _text(result)


async def test_run_show_clips_long_text(api):
    server, routes, _ = api
    routes[("GET", "/observability/runs/r1/trace")] = httpx.Response(
        200, json={"run": {"run_id": "r1", "output": "x" * 10_000}, "spans": [], "scores": []}
    )
    async with McpClient(server) as mcp:
        result = await mcp.call_tool("run_show", {"run_id": "r1"})
    output = _data(result)["run"]["output"]
    assert len(output) < 5000 and "cortado" in output


async def test_eval_with_inline_cases_returns_only_failures(api):
    server, routes, calls = api
    routes[("GET", "/agents/extrator")] = httpx.Response(200, json={**AGENT, "agent_type": "extrator", "kind": "analysis"})
    routes[("POST", "/analyze")] = httpx.Response(200, json={"run_id": "r1", "result": {"acao": "responder"}})
    routes[("POST", "/observability/scores")] = httpx.Response(200, json={})
    cases = [
        {"id": "a", "input": "x", "expected": {"acao": "responder"}},
        {"id": "b", "input": "y", "expected": {"acao": "escalar"}},
    ]
    async with McpClient(server) as mcp:
        result = await mcp.call_tool("eval", {"agent_type": "extrator", "cases": cases, "min_pass": 0.5})
    data = _data(result)
    assert data["verdict"]["ok"] is True
    assert [r["id"] for r in data["results"]] == ["b"]
    assert all(json.loads(r.content).get("dry_run") for r in calls if r.url.path == "/analyze")
