"""CLI `kuro` contra uma API falsa (httpx.MockTransport) — sem serviço no ar."""

import base64
import json

import httpx
import pytest
from typer.testing import CliRunner

from agent_service.cli import main as cli_main
from agent_service.cli.client import Client

AGENT = {
    "agent_type": "suporte",
    "name": "Suporte",
    "instructions": ["Seja breve."],
    "tools": [],
    "model_provider": None,
    "model_id": None,
    "dependency_fields": [
        {"name": "cpf", "type": "string", "label": "CPF", "description": "", "required": True, "default": None}
    ],
    "memory_backend": "common",
    "num_history_runs": 10,
    "is_seed": False,
    "prompt_version": 1,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}

runner = CliRunner()


@pytest.fixture
def api(monkeypatch, tmp_path):
    """Registra as requisições feitas e responde por (método, caminho)."""
    monkeypatch.setenv("KURO_HOME", str(tmp_path))
    calls: list[httpx.Request] = []
    routes: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        response = routes.get((request.method, request.url.path))
        return response if response is not None else httpx.Response(404, json={"detail": "não encontrado"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli_main,
        "Client",
        # `**kw` absorve as opções novas do root (verify, etc.) sem o fixture
        # precisar ser reescrito a cada uma.
        lambda url, timeout, api_key=None, **kw: Client(url, timeout, transport=transport, api_key=api_key, **kw),
    )
    return routes, calls


def _run(*args: str, input: str | None = None):
    return runner.invoke(cli_main.app, list(args), input=input)


def test_agents_list_json(api):
    routes, _ = api
    routes[("GET", "/agents")] = httpx.Response(200, json=[AGENT])
    result = _run("--json", "agents", "list")
    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["agent_type"] == "suporte"


def test_agents_without_tty_lists_instead_of_prompting(api):
    routes, _ = api
    routes[("GET", "/agents")] = httpx.Response(200, json=[AGENT])
    assert _run("agents").exit_code == 0


def test_api_error_exit_code_and_json_error(api):
    result = _run("--json", "agents", "get", "nao-existe")
    assert result.exit_code == 1
    assert json.loads(result.stderr)["status"] == 404


def test_service_unavailable_exit_code(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("recusado", request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli_main,
        "Client",
        # `**kw` absorve as opções novas do root (verify, etc.) sem o fixture
        # precisar ser reescrito a cada uma.
        lambda url, timeout, api_key=None, **kw: Client(url, timeout, transport=transport, api_key=api_key, **kw),
    )
    assert _run("agents", "list").exit_code == 3


def test_set_sends_only_given_fields(api):
    routes, calls = api
    routes[("PUT", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    result = _run("agents", "set", "suporte", "num_history_runs=3", "instructions=Nova instrução")
    assert result.exit_code == 0
    assert json.loads(calls[-1].content) == {"num_history_runs": 3, "instructions": ["Nova instrução"]}


def test_set_rejects_non_editable_field(api):
    assert _run("agents", "set", "suporte", "is_seed=true").exit_code == 2


def test_apply_creates_or_updates(api, tmp_path):
    routes, calls = api
    spec = tmp_path / "agente.json"
    spec.write_text(json.dumps({"agent_type": "suporte", "name": "Novo nome"}), encoding="utf-8")

    routes[("GET", "/agents")] = httpx.Response(200, json=[])
    routes[("POST", "/agents")] = httpx.Response(201, json=AGENT)
    assert _run("agents", "apply", "-f", str(spec)).exit_code == 0
    assert calls[-1].method == "POST"

    routes[("GET", "/agents")] = httpx.Response(200, json=[AGENT])
    routes[("PUT", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    assert _run("agents", "apply", "-f", str(spec)).exit_code == 0
    assert calls[-1].method == "PUT" and json.loads(calls[-1].content) == {"name": "Novo nome"}


def test_delete_requires_yes_without_tty(api):
    assert _run("agents", "delete", "suporte").exit_code == 2


def _sse(*events: tuple[str, dict]) -> bytes:
    return "".join(f"event: {e}\r\ndata: {json.dumps(d)}\r\n\r\n" for e, d in events).encode()


def test_chat_missing_required_dependency_fails_without_prompt(api):
    routes, _ = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    result = _run("--json", "chat", "suporte", "-m", "oi")
    assert result.exit_code == 2
    assert json.loads(result.stderr)["missing"] == ["cpf"]


def test_chat_streams_and_coerces_dependencies(api):
    routes, calls = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    routes[("POST", "/chat/stream")] = httpx.Response(
        200,
        content=_sse(("run", {"run_id": "r1", "trace_id": "t1"}), ("message", {"content": "ol"}),
                     ("message", {"content": "á"}), ("done", {})),
        headers={"content-type": "text/event-stream"},
    )
    result = _run("--json", "chat", "suporte", "-m", "oi", "-d", "cpf=123")
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert (out["content"], out["run_id"]) == ("olá", "r1")
    body = json.loads(calls[-1].content)
    assert body["dependencies"] == {"cpf": "123"}  # string, como o agente declara

    # a sessão é reaproveitada na próxima mensagem
    _run("--json", "chat", "suporte", "-m", "de novo", "-d", "cpf=123")
    assert json.loads(calls[-1].content)["session_id"] == body["session_id"]


def test_chat_with_procedural_agent_returns_and_prints_the_stage(api):
    routes, _ = api
    routes[("GET", "/agents/abertura")] = httpx.Response(200, json={**AGENT, "agent_type": "abertura", "dependency_fields": []})
    state = {"stage": "problema", "stage_index": 1, "stages_total": 4, "done": False,
             "missing": [{"name": "descricao"}], "invalid": {}, "collected": {"cpf": "1"}}
    routes[("POST", "/chat/stream")] = httpx.Response(
        200,
        content=_sse(("run", {"run_id": "r1"}), ("message", {"content": "E o problema?"}), ("state", state), ("done", {})),
        headers={"content-type": "text/event-stream"},
    )
    out = json.loads(_run("--json", "chat", "abertura", "-m", "oi").stdout)
    assert out["state"]["stage"] == "problema"
    result = _run("chat", "abertura", "-m", "oi")
    assert "[2/4 problema] faltando: descricao" in result.stderr


def test_chat_stream_error_event_exits_nonzero(api):
    routes, _ = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "dependency_fields": []})
    routes[("POST", "/chat/stream")] = httpx.Response(
        200, content=_sse(("run", {"run_id": "r1"}), ("error", {"message": "modelo caiu"}), ("done", {}))
    )
    assert _run("chat", "suporte", "-m", "oi").exit_code == 1


def test_tool_invoke_ok_false_exits_nonzero(api):
    routes, _ = api
    routes[("POST", "/tools/calc/invoke")] = httpx.Response(200, json={"ok": False, "result": None, "error": "boom"})
    assert _run("tools", "invoke", "calc", "-a", "x=1").exit_code == 1


ANALYST = {**AGENT, "agent_type": "extrator", "kind": "analysis", "dependency_fields": [],
           "response_schema": [{"name": "valor", "type": "number", "label": "Valor", "description": "",
                                 "required": True, "default": None}]}


def test_analyze_rejects_non_analysis_agent(api):
    routes, _ = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    result = _run("analyze", "suporte", "-f", "-", input="doc")
    assert result.exit_code == 2


def test_analyze_sends_document_and_prints_result(api, tmp_path):
    routes, calls = api
    routes[("GET", "/agents/extrator")] = httpx.Response(200, json=ANALYST)
    routes[("POST", "/analyze")] = httpx.Response(
        200, json={"agent_type": "extrator", "result": {"valor": 42}, "run_id": "r1", "trace_id": None}
    )
    doc = tmp_path / "doc.txt"
    doc.write_text("contrato de 42 reais", encoding="utf-8")

    result = _run("--json", "analyze", "extrator", "-f", str(doc))
    assert result.exit_code == 0
    assert json.loads(result.stdout)["result"] == {"valor": 42}
    body = json.loads(calls[-1].content)
    assert body["document"] == "contrato de 42 reais"


def test_chat_attach_sends_base64_attachment(api, tmp_path):
    routes, calls = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "dependency_fields": []})
    routes[("POST", "/chat/stream")] = httpx.Response(
        200,
        content=_sse(("run", {"run_id": "r1"}), ("message", {"content": "ok"}), ("done", {})),
        headers={"content-type": "text/event-stream"},
    )
    image = tmp_path / "foto.png"
    image.write_bytes(b"\x89PNGdados")

    assert _run("--json", "chat", "suporte", "-m", "o que é isso?", "-a", str(image)).exit_code == 0
    [attachment] = json.loads(calls[-1].content)["attachments"]
    assert attachment["mime_type"] == "image/png"
    assert attachment["filename"] == "foto.png"
    assert base64.b64decode(attachment["content_base64"]) == b"\x89PNGdados"


def test_chat_without_attach_omits_attachments_key(api):
    routes, calls = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "dependency_fields": []})
    routes[("POST", "/chat/stream")] = httpx.Response(
        200, content=_sse(("run", {"run_id": "r1"}), ("done", {})), headers={"content-type": "text/event-stream"}
    )
    assert _run("--json", "chat", "suporte", "-m", "oi").exit_code == 0
    assert "attachments" not in json.loads(calls[-1].content)


def test_chat_attach_missing_file_fails(api):
    routes, _ = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "dependency_fields": []})
    assert _run("--json", "chat", "suporte", "-m", "oi", "-a", "nao-existe.png").exit_code == 2


def test_analyze_with_only_attachment_uses_default_prompt(api, tmp_path):
    routes, calls = api
    routes[("GET", "/agents/extrator")] = httpx.Response(200, json=ANALYST)
    routes[("POST", "/analyze")] = httpx.Response(
        200, json={"agent_type": "extrator", "result": {"valor": 1}, "run_id": "r1", "trace_id": None}
    )
    pdf = tmp_path / "contrato.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    assert _run("--json", "analyze", "extrator", "-a", str(pdf)).exit_code == 0
    body = json.loads(calls[-1].content)
    assert body["document"] == "Analise o(s) anexo(s)."
    assert body["attachments"][0]["mime_type"] == "application/pdf"


def test_feedback_show_prints_current_note(api):
    routes, _ = api
    routes[("GET", "/agents/suporte/feedback")] = httpx.Response(
        200, json={"agent_type": "suporte", "content": "- seja breve", "updated_at": "2026-01-01T00:00:00Z"}
    )
    result = _run("--json", "agents", "feedback", "suporte", "--show")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["content"] == "- seja breve"


def test_feedback_without_saved_session_fails(api):
    result = _run("agents", "feedback", "suporte", "-m", "seja mais direto")
    assert result.exit_code == 2


def test_chat_mostra_as_fontes_consultadas(api):
    routes, calls = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "dependency_fields": []})
    fontes = [
        {"title": "Trocas", "source_url": "https://docs.ex.com/troca", "content_id": "pg-1", "score": 0.91,
         "chunk_ids": ["c1"]},
        {"title": "Manual de garantia", "source_url": None, "content_id": "doc-1", "score": 0.72, "chunk_ids": ["c3"]},
        {"title": "Busca [beta]", "source_url": "https://ex.com/?filtro[]=1", "content_id": "pg-2", "score": None,
         "chunk_ids": ["c7"]},
    ]
    routes[("POST", "/chat/stream")] = httpx.Response(
        200,
        content=_sse(("run", {"run_id": "r1"}), ("message", {"content": "12 meses"}),
                     ("sources", {"sources": fontes}), ("done", {})),
        headers={"content-type": "text/event-stream"},
    )
    humano = _run("chat", "suporte", "-m", "garantia?")
    assert humano.exit_code == 0
    assert "fontes:" in humano.stderr
    assert "[1] Trocas — https://docs.ex.com/troca" in humano.stderr
    assert "[2] Manual de garantia" in humano.stderr
    assert "[3] Busca [beta] — https://ex.com/?filtro[]=1" in humano.stderr  # colchetes não viram markup

    out = json.loads(_run("--json", "chat", "suporte", "-m", "garantia?").stdout)
    assert out["sources"] == fontes


def test_chat_sem_fontes_devolve_lista_vazia(api):
    routes, _ = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "dependency_fields": []})
    routes[("POST", "/chat/stream")] = httpx.Response(
        200, content=_sse(("run", {"run_id": "r1"}), ("message", {"content": "oi"}), ("done", {})),
        headers={"content-type": "text/event-stream"},
    )
    result = _run("chat", "suporte", "-m", "oi")
    assert result.exit_code == 0 and "fontes:" not in result.stderr
    assert json.loads(_run("--json", "chat", "suporte", "-m", "oi").stdout)["sources"] == []


def test_feedback_uses_saved_chat_session(api):
    routes, calls = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "dependency_fields": []})
    routes[("POST", "/chat/stream")] = httpx.Response(
        200,
        content=_sse(("run", {"run_id": "r1", "trace_id": "t1"}), ("message", {"content": "oi"}), ("done", {})),
        headers={"content-type": "text/event-stream"},
    )
    assert _run("--json", "chat", "suporte", "-m", "oi").exit_code == 0
    session_id = json.loads(calls[-1].content)["session_id"]

    routes[("POST", "/agents/suporte/feedback")] = httpx.Response(
        200, json={"agent_type": "suporte", "content": "- seja mais direto", "updated_at": "2026-01-01T00:00:00Z"}
    )
    result = _run("agents", "feedback", "suporte", "-m", "seja mais direto")
    assert result.exit_code == 0
    body = json.loads(calls[-1].content)
    assert body == {"session_id": session_id, "feedback": "seja mais direto"}


def test_global_flags_accepted_after_subcommand(api):
    routes, _ = api
    routes[("GET", "/agents")] = httpx.Response(200, json=[AGENT])
    assert cli_main.hoist_global_flags(["agents", "list", "--json"]) == ["--json", "agents", "list"]
    assert cli_main.hoist_global_flags(["chat", "x", "--", "--json"]) == ["chat", "x", "--", "--json"]
    result = _run(*cli_main.hoist_global_flags(["agents", "list", "--json"]))
    assert json.loads(result.stdout)[0]["agent_type"] == "suporte"


def test_editable_includes_model_credential(api):
    routes, _ = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "model_credential_id": "cred-1"})
    result = _run("--json", "agents", "get", "suporte", "--editable")
    assert json.loads(result.stdout)["model_credential_id"] == "cred-1"


def test_credential_test_failure_exits_nonzero(api):
    routes, _ = api
    routes[("POST", "/model-credentials/c1/test")] = httpx.Response(
        200, json={"ok": False, "message": "chave inválida", "tested_at": "2026-01-01T00:00:00Z"}
    )
    assert _run("credentials", "test", "c1").exit_code == 1


def test_edit_opens_editor_and_sends_only_changes(api, monkeypatch):
    from agent_service.cli import agents as agents_cli
    from agent_service.cli.common import State

    routes, calls = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    routes[("PUT", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "prompt_version": 2})
    monkeypatch.setattr(State, "interactive", property(lambda self: True))

    def fake_editor(text, extension):
        assert extension == ".json"
        data = json.loads(text)
        data["instructions"] = ["Seja muito breve."]
        return json.dumps(data)

    monkeypatch.setattr(agents_cli.click, "edit", fake_editor)
    result = _run("agents", "edit", "suporte")
    assert result.exit_code == 0, result.output
    assert json.loads(calls[-1].content) == {"instructions": ["Seja muito breve."]}


def test_chat_repl_rejects_unknown_slash_command(api, monkeypatch):
    """No REPL do chat, /providers não pode virar mensagem para o agente."""
    from agent_service.cli import chat as chat_cli
    from agent_service.cli.common import State

    routes, calls = api
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "dependency_fields": []})
    monkeypatch.setattr(State, "interactive", property(lambda self: True))
    monkeypatch.setattr(chat_cli, "stdin_is_tty", lambda: True)
    lines = iter(["/providers", "/sair"])
    monkeypatch.setattr("rich.console.Console.input", lambda self, *a, **k: next(lines))

    result = _run("chat", "suporte")
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert [c.url.path for c in calls] == ["/agents/suporte"]  # nada foi enviado ao agente


CREDENTIAL = {
    "id": "c1", "provider": "google", "provider_label": "Google", "label": "Prod",
    "configured": True, "key_hint": "…7890", "base_url": None, "enabled": True,
    "last_tested_at": None, "last_test_ok": None, "last_test_message": None,
    "agents_using": [], "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
}


def test_credential_add_reads_key_from_env_never_from_argv(api, monkeypatch):
    routes, calls = api
    routes[("POST", "/model-credentials")] = httpx.Response(201, json=CREDENTIAL)
    monkeypatch.setenv("MINHA_CHAVE", "sk-secreta")

    result = _run("credentials", "add", "-p", "google", "-l", "Prod", "--api-key-env", "MINHA_CHAVE")
    assert result.exit_code == 0, result.output
    assert json.loads(calls[-1].content)["api_key"] == "sk-secreta"
    # A CLI não expõe uma opção que colocaria a chave no histórico do shell.
    assert "--api-key " not in _run("credentials", "add", "--help").output


def test_credential_add_without_key_source_fails_when_not_interactive(api):
    result = _run("credentials", "add", "-p", "google", "-l", "Prod")
    assert result.exit_code == 2
    assert "--api-key-env" in result.output + (result.stderr or "")


def test_credential_edit_sends_only_what_changed(api):
    routes, calls = api
    routes[("PUT", "/model-credentials/c1")] = httpx.Response(200, json=CREDENTIAL)
    assert _run("credentials", "edit", "c1", "--disable").exit_code == 0
    assert json.loads(calls[-1].content) == {"enabled": False}


def test_credential_edit_without_changes_is_usage_error(api):
    assert _run("credentials", "edit", "c1").exit_code == 2


# -- tools: criar, editar e remover pela CLI (antes só existia na API/UI) -------

TOOL = {
    "tool_name": "calc", "kind": "api", "label": "Calculadora", "description": None,
    "config": {"method": "GET", "url": "https://exemplo/x", "parameters": []},
    "enabled": True, "is_seed": False,
    "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
}


def test_tools_apply_creates_then_updates(api, tmp_path):
    routes, calls = api
    spec = tmp_path / "tool.json"
    spec.write_text(json.dumps({"tool_name": "calc", "kind": "api", "label": "Calculadora"}), encoding="utf-8")

    routes[("GET", "/tools")] = httpx.Response(200, json=[])
    routes[("POST", "/tools")] = httpx.Response(201, json=TOOL)
    assert _run("tools", "apply", "-f", str(spec)).exit_code == 0
    assert calls[-1].method == "POST"

    routes[("GET", "/tools")] = httpx.Response(200, json=[TOOL])
    routes[("PUT", "/tools/calc")] = httpx.Response(200, json=TOOL)
    assert _run("tools", "apply", "-f", str(spec)).exit_code == 0
    # `kind` não é editável: sai do corpo do PUT em vez de virar erro.
    assert calls[-1].method == "PUT" and json.loads(calls[-1].content) == {"label": "Calculadora"}


def test_tools_apply_new_without_kind_is_usage_error(api, tmp_path):
    routes, _ = api
    spec = tmp_path / "tool.json"
    spec.write_text(json.dumps({"tool_name": "calc", "label": "X"}), encoding="utf-8")
    routes[("GET", "/tools")] = httpx.Response(200, json=[])
    assert _run("tools", "apply", "-f", str(spec)).exit_code == 2


def test_tools_set_sends_only_given_fields(api):
    routes, calls = api
    routes[("PUT", "/tools/calc")] = httpx.Response(200, json=TOOL)
    assert _run("tools", "set", "calc", "enabled=false").exit_code == 0
    assert json.loads(calls[-1].content) == {"enabled": False}


def test_tools_set_rejects_non_editable_field(api):
    assert _run("tools", "set", "calc", "kind=python").exit_code == 2


def test_tools_delete_requires_yes_without_tty(api):
    assert _run("tools", "delete", "calc").exit_code == 2


def test_tools_delete_in_use_reports_api_conflict(api):
    routes, _ = api
    routes[("DELETE", "/tools/calc")] = httpx.Response(409, json={"detail": "Tool em uso por: suporte"})
    result = _run("--json", "tools", "delete", "calc", "--yes")
    assert result.exit_code == 1
    assert json.loads(result.stderr)["status"] == 409


# -- sessions: conversas guardadas (rotas do AgentOS, sem depender do Langfuse) --

SESSION = {
    "session_id": "cli-abc", "session_name": "meu wifi caiu", "agent_id": "suporte",
    "user_id": "cli", "total_tokens": 120,
    "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:10:00Z",
}


def test_sessions_list_scopes_by_user_and_agent(api):
    routes, calls = api
    routes[("GET", "/sessions")] = httpx.Response(200, json={"data": [SESSION], "meta": {"page": 1, "total_pages": 1}})
    result = _run("--json", "sessions", "list", "--agent", "suporte")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"][0]["session_id"] == "cli-abc"
    params = calls[-1].url.params
    # sem user_id a listagem traria as conversas de todo mundo, inclusive as do console
    assert (params["user_id"], params["component_id"], params["type"]) == ("cli", "suporte", "agent")


def test_env_file_key_reads_admin_key_from_the_local_env(env_da_pasta_atual, tmp_path):
    from agent_service.cli import connection

    env_file_key = env_da_pasta_atual
    assert env_file_key("http://127.0.0.1:58000", tmp_path) is None  # sem .env
    (tmp_path / ".env").write_text(
        '# comentário\nRUNTIME_API_KEY=rt\nADMIN_API_KEY="nova-123,antiga-456"\n', encoding="utf-8"
    )
    # Rotação `nova,antiga`: vai a nova, sem as aspas.
    assert env_file_key("http://127.0.0.1:58000", tmp_path) == "nova-123"
    assert env_file_key("http://localhost:8000", tmp_path) == "nova-123"
    # O .env é do serviço desta máquina: a chave dele não vai para outro endereço.
    assert env_file_key("https://kuro.empresa.com", tmp_path) is None
    assert connection.resolve_api_key("explicita", "http://127.0.0.1:58000") == "explicita"


def test_cli_without_kuro_api_key_uses_admin_key_from_env_file(api, env_da_pasta_atual, monkeypatch, tmp_path):
    routes, calls = api
    routes[("GET", "/agents")] = httpx.Response(200, json=[AGENT])
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ADMIN_API_KEY=chave-do-env\n")
    monkeypatch.delenv("KURO_API_KEY", raising=False)

    assert _run("--json", "agents", "list").exit_code == 0
    assert calls[-1].headers["authorization"] == "Bearer chave-do-env"

    # KURO_API_KEY explícita sempre vence o .env.
    monkeypatch.setenv("KURO_API_KEY", "chave-explicita")
    assert _run("--json", "agents", "list").exit_code == 0
    assert calls[-1].headers["authorization"] == "Bearer chave-explicita"


def test_unauthorized_says_where_the_key_comes_from(api, monkeypatch):
    routes, _ = api
    routes[("GET", "/agents")] = httpx.Response(401, json={"detail": "Esta rota exige uma chave de API."})
    monkeypatch.delenv("KURO_API_KEY", raising=False)

    result = _run("--json", "agents", "list")

    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["status"] == 401
    assert "ADMIN_API_KEY" in error["error"] and "docker compose exec agent-service kuro" in error["error"]


def test_mcp_config_prints_a_ready_command_with_the_key_masked(api, monkeypatch):
    monkeypatch.setenv("KURO_API_KEY", "chave-admin-1234567890abcdef")
    result = _run("mcp-config", "--url", "https://kuro.empresa.com/")
    assert result.exit_code == 0
    # `[mcp]` não pode sumir (o Rich o leria como marcação)
    assert 'uvx --from "agent-service[mcp] @ git+https://github.com/' in result.stdout
    assert "KURO_API_URL=https://kuro.empresa.com " in result.stdout
    assert "chave-admin-1234567890abcdef" not in result.stdout
    assert "--show-key" in result.stdout

    data = json.loads(_run("--json", "mcp-config", "--url", "https://kuro.empresa.com", "--show-key").stdout)
    assert "KURO_API_KEY=chave-admin-1234567890abcdef" in data["claude_command"]
    server = data["mcp_json"]["mcpServers"]["kuro"]
    assert server["command"] == "uvx" and server["args"][-1] == "kuro-mcp"
    # No .mcp.json a chave vem do ambiente: o arquivo pode ir para o git.
    assert server["env"] == {"KURO_API_URL": "https://kuro.empresa.com", "KURO_API_KEY": "${KURO_API_KEY}"}
    assert data["warnings"] == []


def test_mcp_config_without_domain_or_ip_profile_says_how_to_expose(api, monkeypatch, tmp_path):
    monkeypatch.delenv("KURO_API_KEY", raising=False)
    monkeypatch.setenv("KURO_API_DOMAIN", "localhost")
    monkeypatch.setenv("KURO_PUBLIC_DIR", str(tmp_path / "vazio"))
    monkeypatch.setenv("AGENT_SERVICE_BIND", "127.0.0.1:58123")
    data = json.loads(_run("--json", "--url", "http://localhost:8000", "mcp-config").stdout)
    # O endereço interno do container não serve lá fora: sai o do host, com o caminho para expor.
    assert data["url"] == "http://127.0.0.1:58123" and data["url_source"] == "local"
    assert len(data["warnings"]) == 2
    assert "--profile ip" in data["warnings"][0] and "401" in data["warnings"][1]
    # Sem endereço público, o caminho é o SSH, e não há URL para o MCP por HTTP.
    assert "kuro connect" in data["warnings"][0]
    assert data["ssh_command"].endswith("kuro connect usuario@servidor") and data["http_command"] is None
    assert "KURO_API_KEY" not in data["mcp_json"]["mcpServers"]["kuro"]["env"]
    assert data["setup"] is None


CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIBozCCAUmgAwIBAgIQ\n-----END CERTIFICATE-----"


def _ip_profile(tmp_path, url="https://69.62.89.141:58443"):
    public = tmp_path / "kuro-public"
    public.mkdir()
    (public / "url").write_text(url + "\n", encoding="utf-8")
    (public / "ca.crt").write_text(CA_PEM + "\n", encoding="utf-8")
    return public


def test_mcp_config_with_ip_profile_hands_over_the_ca(api, monkeypatch, tmp_path):
    monkeypatch.setenv("KURO_API_KEY", "chave-admin-1234567890abcdef")
    monkeypatch.setenv("KURO_API_DOMAIN", "localhost")
    monkeypatch.setenv("KURO_PUBLIC_DIR", str(_ip_profile(tmp_path)))
    data = json.loads(_run("--json", "--url", "http://localhost:8000", "mcp-config").stdout)
    assert data["url"] == "https://69.62.89.141:58443" and data["url_source"] == "ip"
    assert data["warnings"] == []
    assert data["ca_pem"] == CA_PEM and data["ca_file"] == "~/.kuro/kuro-ca-69-62-89-141.pem"
    # O certificado vai inteiro nos dois jeitos de salvar, e o comando aponta para ele.
    assert CA_PEM in data["setup"]["bash"] and CA_PEM in data["setup"]["powershell"]
    assert "\n'@ | Set-Content" in data["setup"]["powershell"]  # o `'@` precisa começar a linha
    assert '--env KURO_CA_BUNDLE="$HOME/.kuro/kuro-ca-69-62-89-141.pem"' in data["claude_command"]
    assert '--env KURO_CA_BUNDLE="$HOME\\.kuro\\kuro-ca-69-62-89-141.pem"' in data["claude_command_powershell"]
    assert data["mcp_json"]["mcpServers"]["kuro"]["env"]["KURO_CA_BUNDLE"] == "${KURO_CA_BUNDLE}"
    assert data["ssh_command"].endswith("kuro connect usuario@69.62.89.141")
    # O Claude Code (Node) não confia na CA própria: sem MCP por URL no profile `ip`.
    assert data["http_command"] is None
    texto = _run("--url", "http://localhost:8000", "mcp-config").stdout
    assert "PowerShell" in texto and 'agent-service[mcp] @ git+' in texto


def test_mcp_config_domain_wins_over_ip_profile(api, monkeypatch, tmp_path):
    monkeypatch.setenv("KURO_API_DOMAIN", "kuro.empresa.com")
    monkeypatch.setenv("KURO_PUBLIC_DIR", str(_ip_profile(tmp_path)))
    data = json.loads(_run("--json", "--url", "http://localhost:8000", "mcp-config").stdout)
    assert data["url"] == "https://kuro.empresa.com" and data["setup"] is None


def test_mcp_config_uses_the_tls_domain_from_env(api, monkeypatch):
    monkeypatch.setenv("KURO_API_KEY", "chave-admin-1234567890abcdef")
    monkeypatch.setenv("KURO_API_DOMAIN", "kuro.empresa.com")
    data = json.loads(_run("--json", "--url", "http://localhost:8000", "mcp-config").stdout)
    assert data["url"] == "https://kuro.empresa.com" and data["url_source"] == "domain"
    assert data["warnings"] == []
    assert data["http_command"] == (
        'claude mcp add --transport http kuro https://kuro.empresa.com/mcp -s user --header "Authorization: Bearer chav…cdef"'
    )
    assert 'agent-service[mcp] @ git+' in data["ssh_command"] and data["ssh_command"].endswith("usuario@kuro.empresa.com")
    # --url continua mandando
    data = json.loads(_run("--json", "mcp-config", "--url", "https://outro.example.com").stdout)
    assert data["url"] == "https://outro.example.com" and data["url_source"] == "explicit"


def test_sessions_list_title_drops_the_dependencies_block(api):
    """O AgentOS usa a 1ª mensagem como título, e o Agno junta a ela as dependencies:
    o CPF do cliente aparecia no título da conversa."""
    routes, _ = api
    vazado = {**SESSION, "session_name": 'meu wifi caiu\n\n<additional context>\n{"cpf": "12345678900"}\n</additional context>'}
    so_contexto = {**SESSION, "session_id": "cli-2", "session_name": '<additional context>{"cpf": "1"}</additional context>'}
    routes[("GET", "/sessions")] = httpx.Response(200, json={"data": [vazado, so_contexto], "meta": {}})
    data = json.loads(_run("--json", "sessions", "list").stdout)["data"]
    assert data[0]["session_name"] == "meu wifi caiu"
    assert data[1]["session_name"] is None
    assert "12345678900" not in _run("sessions", "list").stdout


def test_sessions_list_uses_kuro_user_id_env(api, monkeypatch):
    routes, calls = api
    monkeypatch.setenv("KURO_USER_ID", "joana")
    routes[("GET", "/sessions")] = httpx.Response(200, json={"data": [], "meta": {}})
    assert _run("--json", "sessions", "list").exit_code == 0
    assert calls[-1].url.params["user_id"] == "joana"


def test_sessions_show_prints_transcript(api):
    routes, _ = api
    routes[("GET", "/sessions/cli-abc/runs")] = httpx.Response(
        200,
        json=[{"run_id": "r1", "run_input": "meu wifi caiu", "content": "vamos verificar",
               "metrics": {"total_tokens": 120}}],
    )
    result = _run("sessions", "show", "cli-abc")
    assert result.exit_code == 0
    assert "vamos verificar" in result.output


def test_sessions_delete_requires_yes_without_tty(api):
    assert _run("sessions", "delete", "cli-abc").exit_code == 2


def test_sessions_delete_scopes_by_user(api):
    routes, calls = api
    routes[("DELETE", "/sessions/cli-abc")] = httpx.Response(204)
    assert _run("--json", "sessions", "delete", "cli-abc", "--yes").exit_code == 0
    assert calls[-1].url.params["user_id"] == "cli"


# -- runs stats / tail -----------------------------------------------------------

STATS = {
    "buckets": [{"date": "2026-01-01", "runs": 3, "errors": 1, "total_tokens": 900, "cost_usd": 0.01}],
    "status_counts": {"success": 2, "error": 1}, "total_runs": 3, "total_tokens": 900,
    "total_cost_usd": 0.01, "avg_latency_ms": 1500.0, "scanned": 3,
}


def test_runs_stats_renders_and_filters(api):
    routes, calls = api
    routes[("GET", "/observability/stats")] = httpx.Response(200, json=STATS)
    result = _run("runs", "stats", "--agent", "suporte")
    assert result.exit_code == 0
    assert "2026-01-01" in result.output
    assert calls[-1].url.params["agent_type"] == "suporte"


def test_runs_stats_without_langfuse_exits_one(api):
    routes, _ = api
    routes[("GET", "/observability/stats")] = httpx.Response(503, json={"detail": "Langfuse não está configurado."})
    result = _run("--json", "runs", "stats")
    assert result.exit_code == 1
    assert json.loads(result.stderr)["status"] == 503


def _run_row(run_id: str) -> dict:
    return {"run_id": run_id, "trace_id": "t", "agent_type": "suporte", "started_at": "2026-01-01T00:00:00Z",
            "status": "success", "total_tokens": 10, "latency_ms": 1200.0}


def _json_objects(text: str) -> list[dict]:
    """Separa os objetos JSON que o `tail` emite em sequência no stdout."""
    decoder, objetos, idx = json.JSONDecoder(), [], 0
    while idx < len(text):
        if text[idx].isspace():
            idx += 1
            continue
        objeto, idx = decoder.raw_decode(text, idx)
        objetos.append(objeto)
    return objetos


def test_runs_tail_prints_each_run_once(api, monkeypatch):
    from agent_service.cli import runs as runs_cli

    pages = iter([{"items": [_run_row("r1")]}, {"items": [_run_row("r2"), _run_row("r1")]}])
    monkeypatch.setattr(Client, "list_runs", lambda self, **kw: next(pages))
    consultas = []

    def parar_na_segunda(_seconds):
        consultas.append(1)
        if len(consultas) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(runs_cli.time, "sleep", parar_na_segunda)
    result = _run("--json", "runs", "tail")
    assert result.exit_code == 0
    assert [r["run_id"] for r in _json_objects(result.stdout)] == ["r1", "r2"]  # r1 não repete


# -- agents rollback --------------------------------------------------------------

VERSIONS = [
    {"version": 2, "instructions": ["Seja breve."], "created_at": "2026-01-02T00:00:00Z"},
    {"version": 1, "instructions": ["Texto antigo."], "created_at": "2026-01-01T00:00:00Z"},
]


def test_agents_rollback_reapplies_old_instructions(api):
    routes, calls = api
    routes[("GET", "/agents/suporte/versions")] = httpx.Response(200, json=VERSIONS)
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    routes[("PUT", "/agents/suporte")] = httpx.Response(200, json={**AGENT, "prompt_version": 3})
    result = _run("--json", "agents", "rollback", "suporte", "1", "--yes")
    assert result.exit_code == 0
    assert json.loads(calls[-1].content) == {"instructions": ["Texto antigo."]}


def test_agents_rollback_to_current_text_does_not_create_a_version(api):
    routes, calls = api
    routes[("GET", "/agents/suporte/versions")] = httpx.Response(200, json=VERSIONS)
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    result = _run("--json", "agents", "rollback", "suporte", "2", "--yes")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["unchanged"] is True
    assert calls[-1].method == "GET"


def test_agents_rollback_unknown_version_is_usage_error(api):
    routes, _ = api
    routes[("GET", "/agents/suporte/versions")] = httpx.Response(200, json=VERSIONS)
    assert _run("agents", "rollback", "suporte", "9", "--yes").exit_code == 2


def test_agents_rollback_requires_yes_without_tty(api):
    routes, _ = api
    routes[("GET", "/agents/suporte/versions")] = httpx.Response(200, json=VERSIONS)
    routes[("GET", "/agents/suporte")] = httpx.Response(200, json=AGENT)
    assert _run("agents", "rollback", "suporte", "1").exit_code == 2


def test_tools_apply_refuses_to_change_kind(api, tmp_path):
    """Trocar o kind seria outra tool: falha em vez de ignorar a edição em silêncio."""
    routes, _ = api
    spec = tmp_path / "tool.json"
    spec.write_text(json.dumps({"tool_name": "calc", "kind": "python", "label": "X"}), encoding="utf-8")
    routes[("GET", "/tools")] = httpx.Response(200, json=[TOOL])
    result = _run("tools", "apply", "-f", str(spec))
    assert result.exit_code == 2
    assert "kind" in result.output + (result.stderr or "")


def test_runs_tail_exits_one_when_langfuse_goes_away(api, monkeypatch):
    from agent_service.cli import runs as runs_cli
    from agent_service.cli.client import ApiError

    respostas = iter([{"items": [_run_row("r1")]}, ApiError(503, "Langfuse não está configurado.")])

    def list_runs(self, **kw):
        resposta = next(respostas)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    monkeypatch.setattr(Client, "list_runs", list_runs)
    monkeypatch.setattr(runs_cli.time, "sleep", lambda _s: None)
    result = _run("--json", "runs", "tail")
    assert result.exit_code == 1
    assert json.loads((result.stderr or "").strip().splitlines()[-1])["status"] == 503


# -- analyze aceita texto direto, não só arquivo --------------------------------

ANALYST_NESTED = {
    **AGENT, "agent_type": "classificador", "kind": "analysis", "dependency_fields": [],
    "response_schema": [
        {"name": "mensagens", "type": "array", "label": "Mensagens", "description": "", "required": True,
         "default": None,
         "items": {"type": "object", "fields": [
             {"name": "autor", "type": "string", "label": "Autor", "description": "",
              "required": True, "default": None}]}}
    ],
}


def test_analyze_aceita_o_documento_como_texto(api):
    """Um agente operando a CLI manda o JSON direto; escrever um arquivo temporário
    só para passar uma string é a fricção que o `-m` do chat já evita."""
    routes, calls = api
    routes[("GET", "/agents/classificador")] = httpx.Response(200, json=ANALYST_NESTED)
    routes[("POST", "/analyze")] = httpx.Response(
        200, json={"agent_type": "classificador", "result": {"mensagens": []}, "run_id": "r1", "trace_id": None}
    )
    result = _run("--json", "analyze", "classificador", "-m", '{"mensagens": []}')
    assert result.exit_code == 0, result.output
    assert json.loads(calls[-1].content)["document"] == '{"mensagens": []}'


def test_analyze_com_texto_e_arquivo_juntos_e_erro_de_uso(api, tmp_path):
    routes, _ = api
    routes[("GET", "/agents/classificador")] = httpx.Response(200, json=ANALYST_NESTED)
    doc = tmp_path / "d.json"
    doc.write_text("{}", encoding="utf-8")
    assert _run("analyze", "classificador", "-m", "{}", "-f", str(doc)).exit_code == 2


# -- TLS: erro de certificado não é "o serviço caiu" -----------------------------


def test_certificado_recusado_nao_manda_olhar_o_container(monkeypatch):
    """O serviço está de pé; o que falta é confiar na CA. Sugerir
    `docker compose up -d` aqui manda quem opera depurar o lugar errado."""
    def handler(request):
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate",
            request=request,
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli_main, "Client", lambda url, timeout, **kw: Client(url, timeout, transport=transport))
    result = _run("--json", "agents", "list")
    assert result.exit_code == 3
    erro = json.loads(result.stderr)["error"]
    assert "certificado" in erro and "KURO_CA_BUNDLE" in erro
    assert "docker compose" not in erro


def test_porta_sem_resposta_aponta_o_firewall_e_nao_o_container(monkeypatch):
    """Recusa volta na hora; ficar sem resposta até o timeout é firewall descartando
    pacotes (o do painel do provedor, que nem roda na VPS). Sem nova tentativa: seriam
    mais 20 s esperando a mesma coisa."""
    tentativas = []

    def handler(request):
        tentativas.append(request)
        raise httpx.ConnectTimeout("timed out", request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli_main, "Client", lambda url, timeout, **kw: Client(url, timeout, transport=transport))
    result = _run("--json", "agents", "list")
    assert result.exit_code == 3
    erro = json.loads(result.stderr)["error"]
    assert "firewall" in erro and "kuro connect" in erro
    assert "docker compose" not in erro
    assert len(tentativas) == 1


def test_ca_que_nao_existe_vira_erro_de_certificado_e_nao_traceback(monkeypatch, tmp_path):
    """Caminho errado em KURO_CA_BUNDLE (ou o bloco de setup que nunca rodou): o httpx
    estourava FileNotFoundError ao montar o cliente, com a pilha inteira na tela."""
    falta = tmp_path / "kuro-ca.pem"
    monkeypatch.setenv("KURO_CA_BUNDLE", str(falta))
    result = _run("--json", "agents", "list")
    assert result.exit_code == 3
    erro = json.loads(result.stderr)["error"]
    assert str(falta) in erro and "KURO_CA_BUNDLE" in erro and "mcp-config" in erro


def test_conexao_recusada_continua_sugerindo_o_container(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli_main, "Client", lambda url, timeout, **kw: Client(url, timeout, transport=transport))
    result = _run("--json", "agents", "list")
    assert result.exit_code == 3
    assert "docker compose" in json.loads(result.stderr)["error"]


def test_insecure_avisa_no_stderr_sem_sujar_o_json(api):
    """Flag cujo propósito é ser insegura não pode passar despercebida — mas o
    aviso não pode entrar no stdout, que é a saída de dados."""
    routes, _ = api
    routes[("GET", "/agents")] = httpx.Response(200, json=[AGENT])
    result = _run("--json", "--insecure", "agents", "list")
    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["agent_type"] == "suporte"
    assert "--insecure" in (result.stderr or "")


def test_sem_insecure_nao_ha_aviso(api):
    routes, _ = api
    routes[("GET", "/agents")] = httpx.Response(200, json=[AGENT])
    result = _run("--json", "agents", "list")
    assert "--insecure" not in (result.stderr or "")


def test_runs_chunk_feedback_marca_e_exporta(api, tmp_path):
    routes, calls = api
    routes[("POST", "/observability/chunk-feedback")] = httpx.Response(
        201, json={"id": "f1", "label": "wrong", "document": "Tabela", "chunk_id": "c1"}
    )
    out = _run("--json", "runs", "chunk-feedback", "r1", "c1", "wrong", "--comment", "preço antigo", "--by", "ana")
    assert out.exit_code == 0, out.output
    assert json.loads(calls[-1].content) == {
        "run_id": "r1", "chunk_id": "c1", "label": "wrong", "span_id": None, "comment": "preço antigo", "created_by": "ana"
    }
    assert _run("--json", "runs", "chunk-feedback", "r1", "c1", "talvez").exit_code == 2

    routes[("GET", "/observability/chunk-feedback/export")] = httpx.Response(
        200, json=[{"query": "garantia", "collection": "m", "relevant_chunk_ids": ["c1"], "relevant_content_ids": ["d"],
                    "run_ids": ["r1"]}]
    )
    destino = tmp_path / "gabarito.jsonl"
    out = _run("--json", "runs", "chunk-export", "-c", "m", "-o", str(destino))
    assert out.exit_code == 0, out.output
    assert calls[-1].url.params["collection"] == "m"
    assert json.loads(destino.read_text(encoding="utf-8"))["query"] == "garantia"
