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
    for name in ("agent_delete", "agent_promote", "agent_set", "agent_apply", "tool_apply", "feedback_send",
                 "run_chunk_feedback"):
        assert tools[name].annotations.destructive_hint is True, name
    for name in ("run_score", "collection_add"):
        assert tools[name].annotations.destructive_hint is False, name
    # Coletar site sai para a web; ressincronizar também troca e remove páginas.
    assert tools["collection_add_site"].annotations.open_world_hint is True
    assert tools["collection_sync_site"].annotations.destructive_hint is True
    assert set(tools["collection_rm_site"].input_schema["properties"]) == {"name", "source_id"}
    # Chave de modelo não passa pelo contexto do modelo.
    # Chave de modelo não passa pelo contexto do modelo: listar e testar, sim; cadastrar, não.
    assert {name for name in tools if name.startswith("credential")} == {"credentials_list", "credentials_test"}
    assert set(tools["credentials_test"].input_schema["properties"]) == {"credential_id"}


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


async def test_every_parameter_has_a_description(api):
    """O modelo só sabe o que o schema diz: um parâmetro só com `title` o faz adivinhar o formato."""
    server, _, _ = api
    async with McpClient(server) as mcp:
        tools = (await mcp.list_tools()).tools
    missing = [
        f"{t.name}.{name}"
        for t in tools
        for name, prop in t.input_schema.get("properties", {}).items()
        if not prop.get("description")
    ]
    assert missing == []


async def test_run_show_caps_the_whole_response(api):
    """30 spans de 4000 caracteres passavam de 100 mil caracteres no contexto do modelo."""
    server, routes, _ = api
    spans = [{"name": f"span-{i}", "input": "i" * 4000, "output": "o" * 4000} for i in range(60)]
    routes[("GET", "/observability/runs/r1/trace")] = httpx.Response(
        200, json={"run": {"run_id": "r1", "output": "x"}, "spans": spans, "scores": []}
    )
    async with McpClient(server) as mcp:
        result = await mcp.call_tool("run_show", {"run_id": "r1"})
    data = _data(result)
    assert data["truncated"] is True
    assert len(json.dumps(data, ensure_ascii=False)) <= 31_000
    assert data["run"]["run_id"] == "r1"
    assert "itens a mais" in data["spans"][-1]


async def test_small_responses_are_not_marked_truncated(api):
    server, routes, _ = api
    routes[("GET", "/observability/runs/r1/trace")] = httpx.Response(
        200, json={"run": {"run_id": "r1", "output": "curto"}, "spans": [], "scores": []}
    )
    async with McpClient(server) as mcp:
        result = await mcp.call_tool("run_show", {"run_id": "r1"})
    assert "truncated" not in _data(result)


async def test_agent_versions_lists_summaries_and_shows_one_in_full(api):
    """Com um prompt de 20 mil caracteres e 15 versões, a lista despejava 300 mil caracteres."""
    server, routes, _ = api
    long_text = "regra " * 4000
    routes[("GET", "/agents/suporte/versions")] = httpx.Response(
        200,
        json=[{"version": v, "instructions": [long_text], "created_at": "2026-10-01T00:00:00Z"} for v in range(1, 16)],
    )
    async with McpClient(server) as mcp:
        listing = _data(await mcp.call_tool("agent_versions", {"agent_type": "suporte"}))
        one = _data(await mcp.call_tool("agent_versions", {"agent_type": "suporte", "version": 3}))
        missing = await mcp.call_tool("agent_versions", {"agent_type": "suporte", "version": 99})
    assert len(listing["items"]) == 15
    assert listing["items"][0]["chars"] == len(long_text) and len(listing["items"][0]["preview"]) < 300
    assert len(json.dumps(listing)) < 30_000
    assert one["version"] == 3 and one["instructions"]
    assert missing.is_error and "v99" in _text(missing)


async def test_agent_get_editable_is_never_cut(api):
    """O que `editable=true` devolve volta no agent_apply: cortado, gravaria o prompt pela metade."""
    server, routes, _ = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "instructions": ["x" * 50_000]})
    async with McpClient(server) as mcp:
        editable = _data(await mcp.call_tool("agent_get", {"agent_type": "suporte", "editable": True}))
        view = _data(await mcp.call_tool("agent_get", {"agent_type": "suporte"}))
    assert editable["instructions"] == ["x" * 50_000]
    assert view["truncated"] is True


@pytest.mark.parametrize("existing_kind", [None, "python"])
async def test_python_tools_are_refused_when_mcp_runs_on_the_server(existing_kind):
    """Pelo SSH e por `/mcp` o MCP roda no servidor: uma tool Python executaria código no
    processo do serviço, ao alcance dos segredos que `local_files=False` protege."""
    calls: list[httpx.Request] = []
    tools = [{"tool_name": "script", "kind": existing_kind}] if existing_kind else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/tools" and request.method == "GET":
            return httpx.Response(200, json=tools)
        return httpx.Response(200, json={})

    server = build_server(Client("http://kuro.test", transport=httpx.MockTransport(handler)), local_files=False)
    definition = {"tool_name": "script", "config": {"code": "import os"}}
    if existing_kind is None:
        definition["kind"] = "python"
    async with McpClient(server) as mcp:
        result = await mcp.call_tool("tool_apply", {"definition": definition})
    assert result.is_error and "kind=python" in _text(result)
    assert _mutations(calls) == []


async def test_python_tools_still_work_when_mcp_runs_locally(api):
    server, routes, calls = api
    routes[("GET", "/tools")] = httpx.Response(200, json=[])
    routes[("POST", "/tools")] = httpx.Response(201, json={"tool_name": "script", "kind": "python"})
    async with McpClient(server) as mcp:
        result = await mcp.call_tool("tool_apply", {"definition": {"tool_name": "script", "kind": "python"}})
    assert not result.is_error, _text(result)
    assert _mutations(calls) == ["POST /tools"]


async def test_runs_list_never_drops_runs_nor_cuts_the_cursor(api):
    """Tirar itens de uma página faria o `next_cursor` pular os que saíram, e um cursor ou id
    cortado não volta mais para a API."""
    server, routes, _ = api
    cursor = "c" * 400
    items = [{"run_id": f"run-{i:03d}-" + "x" * 300, "message": "m" * 3000, "output": "o" * 3000} for i in range(100)]
    routes[("GET", "/observability/runs")] = httpx.Response(200, json={"items": items, "next_cursor": cursor})
    async with McpClient(server) as mcp:
        data = _data(await mcp.call_tool("runs_list", {"limit": 100}))
    assert data["next_cursor"] == cursor
    assert [r["run_id"] for r in data["items"]] == [r["run_id"] for r in items]
    assert data["truncated"] is True


# -- M4: o que faltava frente à CLI -------------------------------------------------


async def test_shadow_cycle_reference_export_and_eval(api):
    """runs_reference → runs_export → eval(cases): o ciclo shadow → dataset sem sair do MCP."""
    server, routes, calls = api
    routes[("POST", "/observability/references")] = httpx.Response(201, json={"run_id": "r1", "source": "legacy"})
    case = {"id": "r1", "input": "x" * 20_000, "dependencies": {}, "expected": {"acao": "escalar"}}
    routes[("GET", "/observability/export")] = httpx.Response(200, json=[{**case, "id": f"r{i}"} for i in range(5)])
    async with McpClient(server) as mcp:
        saved = await mcp.call_tool("runs_reference", {"run_id": "r1", "reference": {"acao": "escalar"}})
        exported = _data(await mcp.call_tool("runs_export", {"agent_type": "r8", "since": "2026-10-01"}))
    assert not saved.is_error, _text(saved)
    assert json.loads(calls[0].content) == {"run_id": "r1", "reference": {"acao": "escalar"}, "source": "legacy"}
    # Nenhum caso cortado: os que não cabem ficam de fora, e a resposta diz quantos.
    assert all(c["input"] == "x" * 20_000 for c in exported["cases"])
    assert exported["count"] == 1 and exported["omitted"] == 4
    assert calls[-1].url.params["agent_type"] == "r8" and calls[-1].url.params["since"] == "2026-10-01"


async def test_agents_export_never_cuts_and_lists_what_did_not_fit(api):
    server, routes, _ = api
    big = [{**AGENT, "agent_type": f"a{i}", "instructions": ["x" * 20_000]} for i in range(3)]
    seed = {**AGENT, "agent_type": "seed", "is_seed": True}
    routes[("GET", "/agents")] = httpx.Response(200, json=big + [seed])
    async with McpClient(server) as mcp:
        data = _data(await mcp.call_tool("agents_export", {}))
        missing = await mcp.call_tool("agents_export", {"agent_types": ["nao-existe"]})
    assert [a["agent_type"] for a in data["items"]] == ["a0"]
    assert data["items"][0]["instructions"] == ["x" * 20_000]
    assert "prompt_version" not in data["items"][0]  # só o editável
    assert data["remaining"] == ["a1", "a2"]  # o seed não sai sem ser pedido
    assert missing.is_error and "nao-existe" in _text(missing)


async def test_credentials_test_calls_the_saved_key_test(api):
    server, routes, calls = api
    routes[("POST", "/model-credentials/c1/test")] = httpx.Response(
        200, json={"ok": False, "message": "401 do provedor", "tested_at": "2026-10-14T10:00:00Z"}
    )
    async with McpClient(server) as mcp:
        data = _data(await mcp.call_tool("credentials_test", {"credential_id": "c1"}))
    assert data == {"credential_id": "c1", "ok": False, "message": "401 do provedor", "tested_at": "2026-10-14T10:00:00Z"}
    assert _mutations(calls) == ["POST /model-credentials/c1/test"]


async def test_collection_create_and_set_refuses_embedder_change(api):
    server, routes, calls = api
    routes[("POST", "/collections")] = httpx.Response(201, json={"name": "manuais"})
    routes[("PUT", "/collections/manuais")] = httpx.Response(200, json={"name": "manuais", "label": "v2"})
    async with McpClient(server) as mcp:
        await mcp.call_tool("collection_create", {"name": "manuais", "label": "Manuais", "embedder": "ollama"})
        swap = await mcp.call_tool("collection_set", {"name": "manuais", "changes": {"embedder": "openai"}})
        ok = await mcp.call_tool("collection_set", {"name": "manuais", "changes": {"label": "v2"}})
    assert json.loads(calls[0].content) == {"name": "manuais", "label": "Manuais", "embedder_provider": "ollama"}
    assert swap.is_error and "embedder" in _text(swap)
    assert not ok.is_error and json.loads(calls[-1].content) == {"label": "v2"}
    assert _mutations(calls) == ["POST /collections", "PUT /collections/manuais"]


async def test_chat_and_analyze_send_attachments_and_metadata(api):
    server, routes, calls = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    routes[("GET", "/agents/extrator")] = httpx.Response(200, json={**AGENT, "agent_type": "extrator", "kind": "analysis"})
    routes[("POST", "/chat/stream")] = httpx.Response(
        200, content=_sse(("run", {"run_id": "r1"}), ("done", {})), headers={"content-type": "text/event-stream"}
    )
    routes[("POST", "/analyze")] = httpx.Response(200, json={"run_id": "r2", "result": {}})
    anexo = {"url": "https://exemplo.com/contrato.pdf", "mime_type": "application/pdf"}
    async with McpClient(server) as mcp:
        await mcp.call_tool(
            "chat",
            {"agent_type": "suporte", "message": "veja", "dependencies": {"cpf": "1"}, "attachments": [anexo],
             "metadata": {"conversation_id": "98231"}},
        )
        await mcp.call_tool(
            "analyze",
            {"agent_type": "extrator", "attachments": [anexo], "dependencies": {"cpf": "1"}, "user_id": "u1"},
        )
        both = await mcp.call_tool(
            "analyze", {"agent_type": "extrator", "attachments": [{**anexo, "content_base64": "AAA="}],
                        "dependencies": {"cpf": "1"}},
        )
    chat_body = json.loads(next(r.content for r in calls if r.url.path == "/chat/stream"))
    assert chat_body["attachments"] == [anexo] and chat_body["metadata"] == {"conversation_id": "98231"}
    analyze_body = json.loads(next(r.content for r in calls if r.url.path == "/analyze"))
    # Só anexo: o /analyze exige `document`, e vai a instrução curta da CLI.
    assert analyze_body["document"] == "Analise o(s) anexo(s)." and analyze_body["user_id"] == "u1"
    assert both.is_error and "url ou content_base64" in _text(both)


# -- M6: defaults coerentes ---------------------------------------------------------


async def test_runs_list_hides_tests_by_default_but_not_inside_a_session(api):
    """O chat daqui é dry_run: filtrado pela conversa, o run que ele acabou de criar aparece."""
    server, routes, calls = api
    routes[("GET", "/observability/runs")] = httpx.Response(200, json={"items": [], "next_cursor": None})
    async with McpClient(server) as mcp:
        await mcp.call_tool("runs_list", {"agent_type": "suporte"})
        await mcp.call_tool("runs_list", {"session_id": "mcp-abc"})
        await mcp.call_tool("runs_list", {"include_tests": True, "min_chars": 20, "user_id": "cli"})
    default, session, explicit = (r.url.params for r in calls)
    assert default["include_dry_run"] == "false"
    assert "include_dry_run" not in session and session["session_id"] == "mcp-abc"
    assert "include_dry_run" not in explicit
    assert explicit["min_message_chars"] == "20" and explicit["user_id"] == "cli"


async def test_sessions_list_shows_every_user_and_paginates(api):
    """Com `user_id="mcp"` fixo, a conversa da CLI ou de uma integração sumia sem aviso."""
    server, routes, calls = api
    page = {"data": [{"session_id": "s1", "session_name": "oi"}], "meta": {"page": 2, "total_pages": 3}}
    routes[("GET", "/sessions")] = httpx.Response(200, json=page)
    routes[("GET", "/sessions/s1/runs")] = httpx.Response(200, json=[])
    routes[("GET", "/observability/sessions/s1/transcript")] = httpx.Response(200, json={"items": []})
    async with McpClient(server) as mcp:
        data = _data(await mcp.call_tool("sessions_list", {"agent_type": "suporte", "page": 2}))
        await mcp.call_tool("session_show", {"session_id": "s1"})
        await mcp.call_tool("session_show", {"session_id": "s1", "raw": True})
    listed, shown, raw = calls
    assert shown.url.path == "/observability/sessions/s1/transcript"
    assert raw.url.path == "/sessions/s1/runs"
    assert "user_id" not in listed.url.params and listed.url.params["page"] == "2"
    assert listed.url.params["component_id"] == "suporte"
    assert data["meta"]["total_pages"] == 3
    assert "user_id" not in shown.url.params


async def test_runs_sessions_groups_by_conversation(api):
    server, routes, calls = api
    routes[("GET", "/observability/sessions")] = httpx.Response(200, json={"items": [{"session_id": "s1"}], "scanned": 1})
    async with McpClient(server) as mcp:
        data = _data(await mcp.call_tool("runs_sessions", {"agent_type": "r8", "limit": 999}))
    assert data["items"] == [{"session_id": "s1"}]
    assert calls[0].url.params["limit"] == "200"


async def test_run_feedback_and_related_timeline(api):
    """O 👎 de um analista com a saída correta e a linha do tempo por ciclo_id."""
    server, routes, calls = api
    routes[("POST", "/observability/runs/r1/feedback")] = httpx.Response(
        201, json={"run_id": "r1", "value": 0, "reference_saved": True}
    )
    routes[("GET", "/observability/runs/r1/related")] = httpx.Response(
        200, json={"key": "ciclo_id", "value": "c9", "items": [{"run_id": "r0"}, {"run_id": "r1"}]}
    )
    async with McpClient(server) as mcp:
        saved = _data(await mcp.call_tool("run_feedback", {"run_id": "r1", "value": 0, "reference": {"nota": 5}}))
        related = _data(await mcp.call_tool("run_related", {"run_id": "r1"}))
    assert saved["reference_saved"] is True
    assert json.loads(calls[0].content) == {"value": 0, "comment": None, "reference": {"nota": 5}}
    assert related["key"] == "ciclo_id" and [r["run_id"] for r in related["items"]] == ["r0", "r1"]
