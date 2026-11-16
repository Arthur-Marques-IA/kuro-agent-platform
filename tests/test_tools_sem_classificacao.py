"""Tools sem classificação de efeito colateral: a sugestão pelo método HTTP (nunca gravada
sozinha), o aviso no cadastro, `kuro tools audit` e o que `null` faz em tempo de execução."""

import json
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from typer.testing import CliRunner


from agent_service.api.agents_routes import _validate_procedure_tools
from agent_service.api.tools_routes import (
    ToolIn,
    ToolUpdateIn,
    create_tool,
    delete_tool,
    get_tool,
    list_tools,
    update_tool,
)
from agent_service.cli import main as cli_main
from agent_service.cli.client import Client
from agent_service.cli.tools import unclassified_report
from agent_service.tools.api_tool import suggest_side_effect

runner = CliRunner()


def _config(method=None):
    config = {"url": "https://exemplo.test/x", "parameters": []}
    if method:
        config["method"] = method
    return config


@pytest.mark.parametrize(
    ("method", "expected"),
    [(None, False), ("GET", False), ("get", False), ("HEAD", False)]
    + [(m, True) for m in ("POST", "PUT", "PATCH", "DELETE")],
)
def test_suggestion_by_http_method(method, expected):
    assert suggest_side_effect("api", _config(method)) is expected


def test_suggestion_only_exists_for_api_tools():
    assert suggest_side_effect("builtin", {"builtin_id": "calculator"}) is None
    assert suggest_side_effect("python", {"code": "x"}) is None


@pytest.fixture
def nome():
    name = f"sug_{uuid4().hex[:8]}"
    yield name
    try:
        delete_tool(name)
    except Exception:
        pass


def test_api_returns_suggestion_without_saving_it_and_warns_on_apply(nome):
    created = create_tool(ToolIn(tool_name=nome, kind="api", label="Grava", config=_config("POST")))
    assert created["side_effect"] is None  # a sugestão não é gravada
    assert created["side_effect_suggested"] is True
    assert len(created["warnings"]) == 1
    assert "side_effect=true" in created["warnings"][0] and "kuro tools set" in created["warnings"][0]

    assert get_tool(nome)["side_effect"] is None and get_tool(nome)["side_effect_suggested"] is True
    listed = next(t for t in list_tools() if t["tool_name"] == nome)
    assert (listed["side_effect"], listed["side_effect_suggested"]) == (None, True)

    # Aceitar a sugestão é classificar: o aviso some, e a sugestão continua ao lado do valor.
    done = update_tool(nome, ToolUpdateIn(side_effect=True))
    assert done["side_effect"] is True and done["warnings"] == []
    assert done["side_effect_suggested"] is True
    # Discordar também vale: o operador manda.
    assert update_tool(nome, ToolUpdateIn(side_effect=False))["side_effect"] is False
    # Voltar a `null` avisa de novo.
    reset = update_tool(nome, ToolUpdateIn.model_validate({"side_effect": None}))
    assert reset["side_effect"] is None and len(reset["warnings"]) == 1


def test_get_tool_suggests_read_for_get(nome):
    create_tool(ToolIn(tool_name=nome, kind="api", label="Lê", config=_config("GET"), side_effect=False))
    out = get_tool(nome)
    assert out["side_effect"] is False and out["side_effect_suggested"] is False
    assert not out.get("warnings")


def test_unclassified_report_lists_agents_that_use_each_tool():
    tools = [
        {"tool_name": "a", "kind": "api", "enabled": True, "side_effect": None, "side_effect_suggested": True},
        {"tool_name": "b", "kind": "api", "enabled": True, "side_effect": False},
        {"tool_name": "c", "kind": "builtin", "enabled": True, "side_effect": None},
    ]
    agents = [{"agent_type": "x", "tools": ["a", "b"]}, {"agent_type": "y", "tools": ["a"]}, {"agent_type": "z"}]
    report = unclassified_report(tools, agents)
    assert [r["tool_name"] for r in report] == ["a", "c"]
    assert report[0]["agents"] == ["x", "y"] and report[0]["side_effect_suggested"] is True
    assert report[1]["agents"] == [] and report[1]["side_effect_suggested"] is None


# -- CLI ------------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("KURO_HOME", str(tmp_path))
    routes: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        found = routes.get((request.method, request.url.path))
        return found if found is not None else httpx.Response(404, json={"detail": "x"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli_main,
        "Client",
        lambda url, timeout, api_key=None, **kw: Client(url, timeout, transport=transport, api_key=api_key, **kw),
    )
    return routes


TOOLS = [
    {
        "tool_name": "grava",
        "kind": "api",
        "label": "Grava",
        "enabled": True,
        "is_seed": False,
        "side_effect": None,
        "side_effect_suggested": True,
    },
    {
        "tool_name": "le",
        "kind": "api",
        "label": "Lê",
        "enabled": True,
        "is_seed": False,
        "side_effect": False,
        "side_effect_suggested": False,
    },
]


def test_tools_list_marks_unclassified_with_the_suggestion(api):
    api[("GET", "/tools")] = httpx.Response(200, json=TOOLS)
    out = runner.invoke(cli_main.app, ["tools", "list"]).output
    assert "não classificada" in out and "sugestão: sim" in out


def test_tools_audit_json_and_strict(api):
    api[("GET", "/tools")] = httpx.Response(200, json=TOOLS)
    api[("GET", "/agents")] = httpx.Response(200, json=[{"agent_type": "suporte", "tools": ["grava", "le"]}])
    result = runner.invoke(cli_main.app, ["--json", "tools", "audit"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["unclassified"] == [
        {
            "tool_name": "grava",
            "kind": "api",
            "enabled": True,
            "side_effect_suggested": True,
            "agents": ["suporte"],
        }
    ]
    assert runner.invoke(cli_main.app, ["--json", "tools", "audit", "--strict"]).exit_code == 1


def test_tools_set_prints_the_warning_and_keeps_exit_zero(api):
    api[("PUT", "/tools/grava")] = httpx.Response(
        200, json={**TOOLS[0], "warnings": ["A tool 'grava' não está classificada"]}
    )
    result = runner.invoke(cli_main.app, ["tools", "set", "grava", "label=Grava"])
    assert result.exit_code == 0 and "não está classificada" in result.output
    as_json = runner.invoke(cli_main.app, ["--json", "tools", "set", "grava", "label=Grava"])
    assert json.loads(as_json.output)["warnings"]


# -- o que `null` faz em tempo de execução (documentado, não mudou) -------------------


def test_null_does_not_block_a_procedural_agent_but_true_does(nome):
    create_tool(ToolIn(tool_name=nome, kind="api", label="Grava", config=_config("POST")))
    # `null`: a trava do procedural só olha `side_effect is True`, então a tool passa em `tools`.
    _validate_procedure_tools([], [nome], set())
    update_tool(nome, ToolUpdateIn(side_effect=True))
    with pytest.raises(HTTPException) as refused:
        _validate_procedure_tools([], [nome], set())
    assert refused.value.status_code == 422
