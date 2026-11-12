"""O que o Regente (ou qualquer sistema integrado) precisa do Kuro: decisão
restrita por enum, parâmetros do modelo, tempo limite, limite de simultâneas,
correlação por metadata, referência do shadow, concordância, export e readiness.

O modelo é sempre falso: o agente devolvido pelo registry é trocado por um que
emite eventos prontos, então nada sai para a rede."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from agno.metrics import RunMetrics
from agno.run.agent import RunCompletedEvent, RunStartedEvent
from fastapi import HTTPException
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_service.agents.dependency_fields import (
    DependencyValidationError,
    validate_dependencies,
    validate_field_specs,
)
from agent_service.agents.response_model import build_response_model
from agent_service.api import auth, routes
from agent_service.api.agents_routes import (
    AgentDefinitionIn,
    AgentDefinitionUpdate,
    create_agent,
    delete_agent,
    get_agent_definition,
    update_agent,
)
from agent_service.api.observability_routes import (
    ReferenceIn,
    agreement,
    export_cases,
    get_run_trace,
    list_runs,
    list_sessions,
    save_reference,
)
from agent_service.api.routes import AnalyzeRequest, analyze, ready
from agent_service.cli import main as cli_main
from agent_service.cli.client import Client
from agent_service.config import get_settings
from agent_service.field_schema import FieldSchemaError, json_schema_for
from agent_service.models.params import ModelParamsError, provider_kwargs, validate_model_params
from agent_service.observability import trace_store, tracing

SCHEMA = [
    {"name": "acao", "type": "string", "required": True, "enum": ["responder", "transferir", "nada"]},
    {"name": "departamento", "type": "string"},
]


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    yield
    trace_store.set_trace_store(None)


@pytest.fixture
def r8():
    name = f"r8-{uuid4().hex[:6]}"
    create_agent(
        AgentDefinitionIn(agent_type=name, name="R8", instructions=["Decida."], kind="analysis", response_schema=SCHEMA)
    )
    yield name
    delete_agent(name)


def fake_agent(monkeypatch, decide, *, hang: bool = False):
    """Troca o agente do registry por um que decide com `decide(document)`."""
    real = routes.get_agent_with_definition

    def resolve(agent_type):
        _, definition = real(agent_type)
        output_model = build_response_model(agent_type, definition["response_schema"])

        class Agent:
            def arun(self, message, **kwargs):
                async def stream():
                    yield RunStartedEvent(run_id=kwargs.get("run_id"))
                    if hang:
                        await asyncio.Event().wait()
                    yield RunCompletedEvent(
                        content=output_model(**decide(message)),
                        metrics=RunMetrics(input_tokens=10, output_tokens=2, total_tokens=12),
                    )

                return stream()

        return Agent(), definition

    monkeypatch.setattr(routes, "get_agent_with_definition", resolve)


# -- enum ------------------------------------------------------------------------------


def test_enum_restricts_the_output_and_goes_to_the_provider_schema():
    model = build_response_model("x", validate_field_specs(SCHEMA))
    assert model(acao="transferir").acao == "transferir"
    with pytest.raises(ValidationError):
        model(acao="negociar")
    assert model.model_json_schema()["properties"]["acao"]["enum"] == ["responder", "transferir", "nada"]
    assert json_schema_for(validate_field_specs(SCHEMA)[0])["enum"] == ["responder", "transferir", "nada"]


def test_enum_is_validated_on_registration_and_on_dependencies():
    for bad in (
        [{"name": "n", "type": "integer", "enum": ["um"]}],
        [{"name": "b", "type": "boolean", "enum": [True]}],
        [{"name": "s", "type": "string", "enum": []}],
        [{"name": "s", "type": "string", "enum": ["a"], "default": "b"}],
    ):
        with pytest.raises(ValueError):
            validate_field_specs(bad)
    specs = validate_field_specs([{"name": "canal", "type": "string", "enum": ["whatsapp", "email"]}])
    assert validate_dependencies(specs, {"canal": "whatsapp"}) == {"canal": "whatsapp"}
    with pytest.raises(DependencyValidationError):
        validate_dependencies(specs, {"canal": "sms"})


def test_enum_in_array_items():
    from agent_service.field_schema import validate_fields

    [field] = validate_fields(
        [{"name": "tags", "type": "array", "items": {"type": "string", "enum": ["a", "b"]}}], path="x"
    )
    assert field["items"]["enum"] == ["a", "b"]
    with pytest.raises(FieldSchemaError):
        validate_fields([{"name": "t", "type": "array", "items": {"type": "integer", "enum": ["a"]}}], path="x")


def test_fields_without_enum_keep_the_same_shape():
    """Um `enum: null` em todo campo mudaria o hash de todo agente que já existia."""
    assert "enum" not in validate_field_specs([{"name": "x", "type": "string", "enum": None}])[0]


# -- parâmetros do modelo ---------------------------------------------------------------


def test_model_params_are_validated_and_translated_per_provider():
    params = validate_model_params({"temperature": 0.3, "max_tokens": 1024, "thinking_budget": 0}, "google")
    assert params == {"temperature": 0.3, "max_tokens": 1024, "thinking_budget": 0}
    assert provider_kwargs("google", params) == {"temperature": 0.3, "max_output_tokens": 1024, "thinking_budget": 0}
    assert provider_kwargs("openai", {"max_tokens": 5}) == {"max_completion_tokens": 5}
    assert provider_kwargs("anthropic", {"max_tokens": 5}) == {"max_tokens": 5}
    assert provider_kwargs("ollama", {"temperature": 0.1, "max_tokens": 5}) == {
        "options": {"temperature": 0.1, "num_predict": 5}
    }
    assert validate_model_params({}, None) is None
    for bad, provider in (
        ({"temperature": 3}, None),
        ({"top_k": 1}, None),
        ({"thinking_budget": 0}, "openai"),
        ({"max_tokens": 1.5}, None),
    ):
        with pytest.raises(ModelParamsError):
            validate_model_params(bad, provider)


def test_model_params_reach_the_model_and_count_as_a_new_version(r8):
    from agent_service.agents import registry
    from agent_service.agents.registry import get_agent_with_definition

    _, before = get_agent_with_definition(r8)
    update_agent(r8, AgentDefinitionUpdate(model_params={"temperature": 0.3}, timeout_seconds=20))
    registry._cache.pop(r8, None)  # SQLite dos testes: updated_at com resolução de segundo
    agent, after = get_agent_with_definition(r8)

    assert agent.model.temperature == 0.3
    assert after["agent_version"] == before["agent_version"] + 1
    assert get_agent_definition(r8)["timeout_seconds"] == 20
    with pytest.raises(HTTPException) as exc:
        update_agent(r8, AgentDefinitionUpdate(model_provider="openai", model_params={"thinking_budget": 0}))
    assert exc.value.status_code == 422


# -- tempo limite e simultâneas ------------------------------------------------------------


def test_run_over_the_timeout_is_504_and_recorded_as_error(monkeypatch, r8):
    update_agent(r8, AgentDefinitionUpdate(timeout_seconds=1))
    fake_agent(monkeypatch, lambda doc: {"acao": "nada"}, hang=True)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(analyze(AnalyzeRequest(agent_type=r8, document="doc", metadata={"conversation_id": "t1"})))

    assert exc.value.status_code == 504
    [run] = list_runs(agent_type=r8, meta=["conversation_id=t1"]).items
    assert run.status == "error" and "Tempo limite" in run.status_message


def test_no_free_slot_is_503_with_retry_after(monkeypatch, r8):
    fake_agent(monkeypatch, lambda doc: {"acao": "nada"})
    monkeypatch.setattr(routes, "_slots", asyncio.Semaphore(0))
    monkeypatch.setattr(get_settings(), "queue_max_wait_seconds", 0.05)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(analyze(AnalyzeRequest(agent_type=r8, document="doc")))

    assert exc.value.status_code == 503 and exc.value.headers["Retry-After"]
    assert routes._waiting == 0


def test_full_queue_is_503_without_waiting(monkeypatch, r8):
    fake_agent(monkeypatch, lambda doc: {"acao": "nada"})
    monkeypatch.setattr(routes, "_slots", asyncio.Semaphore(0))
    monkeypatch.setattr(get_settings(), "queue_max_size", 0)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(analyze(AnalyzeRequest(agent_type=r8, document="doc")))

    assert exc.value.status_code == 503


def test_call_over_the_limit_waits_for_a_slot_and_succeeds(monkeypatch, r8):
    fake_agent(monkeypatch, lambda doc: {"acao": "nada"})

    async def scenario():
        monkeypatch.setattr(routes, "_slots", asyncio.Semaphore(1))
        await routes._slots.acquire()  # a única vaga está ocupada
        task = asyncio.create_task(analyze(AnalyzeRequest(agent_type=r8, document="doc")))
        await asyncio.sleep(0.2)
        assert not task.done() and routes._waiting == 1
        routes._slots.release()
        return await asyncio.wait_for(task, 10)

    monkeypatch.setattr(get_settings(), "queue_max_wait_seconds", 5)
    assert asyncio.run(scenario()).result["acao"] == "nada"
    assert routes._waiting == 0


# -- correlação, referência, concordância e export ------------------------------------------


def test_metadata_and_session_tie_the_decision_to_the_conversation(monkeypatch, r8):
    fake_agent(monkeypatch, lambda doc: {"acao": "responder"})
    conversa = f"conv-{uuid4().hex[:6]}"
    for _ in range(2):
        asyncio.run(
            analyze(
                AnalyzeRequest(agent_type=r8, document="oi", session_id=conversa, metadata={"lead_id": 42, "vip": True})
            )
        )

    runs = list_runs(agent_type=r8, meta=["lead_id=42", "vip=true"]).items
    assert len(runs) == 2 and runs[0].metadata == {"lead_id": "42", "vip": "true"}
    # O trace completo também traz a metadata (o loop dos spans não pode sobrescrevê-la).
    assert get_run_trace(runs[0].run_id).run.metadata == {"lead_id": "42", "vip": "true"}
    assert list_runs(agent_type=r8, meta=["lead_id=43"]).items == []
    [session] = list_sessions(agent_type=r8).items
    assert session.session_id == conversa and session.run_count == 2


def test_metadata_is_bounded():
    with pytest.raises(ValidationError):
        AnalyzeRequest(agent_type="x", document="d", metadata={f"k{i}": i for i in range(21)})
    with pytest.raises(ValidationError):
        AnalyzeRequest(agent_type="x", document="d", metadata={"k": "v" * 300})


def test_shadow_reference_agreement_and_export(monkeypatch, r8):
    fake_agent(
        monkeypatch, lambda doc: {"acao": "transferir" if "acordo" in doc else "responder", "departamento": None}
    )
    legacy = {
        "quero um acordo": {"acao": "transferir", "departamento": None},
        "esqueci a senha": {"acao": "responder", "departamento": None},
        "boleto vencido": {"acao": "transferir", "departamento": None},  # aqui o Kuro discorda
    }
    run_ids = {}
    for message, decision in legacy.items():
        response = asyncio.run(analyze(AnalyzeRequest(agent_type=r8, document=message, dependencies={"canal": "wpp"})))
        save_reference(ReferenceIn(run_id=response.run_id, reference=decision))
        run_ids[message] = response.run_id

    result = agreement(agent_type=r8, since=None, until=None, agent_version=None, fields=None, tolerance=0.01)

    assert (result.runs, result.full_match) == (3, 2)
    assert {f.field: (f.compared, f.matched) for f in result.fields} == {"acao": (3, 2), "departamento": (3, 3)}
    assert result.by_version[0].runs == 3
    [disagreement] = result.disagreements
    assert disagreement.run_id == run_ids["boleto vencido"]
    assert disagreement.mismatches == [{"field": "acao", "expected": "transferir", "actual": "responder"}]

    cases = {c.id: c for c in export_cases(agent_type=r8, since=None, until=None, agent_version=None, limit=100)}
    case = cases[run_ids["quero um acordo"]]
    assert case.input == "quero um acordo" and case.dependencies == {"canal": "wpp"}
    assert case.expected == {"acao": "transferir", "departamento": None}


def test_reference_for_unknown_run_is_404():
    with pytest.raises(HTTPException) as exc:
        save_reference(ReferenceIn(run_id="nao-existe", reference={"acao": "nada"}))
    assert exc.value.status_code == 404


def test_runtime_key_reaches_references_and_ready_is_public():
    assert "/observability/references" in auth.RUNTIME_PATHS
    assert "/ready" in auth.PUBLIC_PATHS
    assert ready() == {"status": "ready"}


def test_ready_is_503_when_the_database_is_down(monkeypatch):
    class Broken:
        @property
        def db_engine(self):
            raise ConnectionError("sem banco")

    monkeypatch.setattr("agent_service.db.get_db", lambda: Broken())
    with pytest.raises(HTTPException) as exc:
        ready()
    assert exc.value.status_code == 503


# -- agentes como código ----------------------------------------------------------------------


def test_dry_run_validates_without_writing(r8):
    preview = update_agent(r8, AgentDefinitionUpdate(instructions=["Nova regra."]), dry_run=True)
    assert preview["instructions"] == ["Nova regra."] and preview["prompt_version"] == 2
    assert get_agent_definition(r8)["instructions"] == ["Decida."]

    novo = f"novo-{uuid4().hex[:6]}"
    created = create_agent(AgentDefinitionIn(agent_type=novo, name="N", instructions=["a"]), dry_run=True)
    assert created["agent_type"] == novo
    with pytest.raises(HTTPException):
        get_agent_definition(novo)
    with pytest.raises(HTTPException) as exc:
        update_agent(r8, AgentDefinitionUpdate(model_params={"temperature": 9}), dry_run=True)
    assert exc.value.status_code == 422


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("KURO_HOME", str(tmp_path))
    agents = {
        "r8": {
            "agent_type": "r8",
            "name": "R8",
            "instructions": ["Decida."],
            "tools": [],
            "kind": "analysis",
            "response_schema": SCHEMA,
            "is_seed": False,
            "prompt_version": 1,
            "model_params": None,
        },
        "seed": {
            "agent_type": "seed",
            "name": "S",
            "instructions": ["x"],
            "tools": [],
            "kind": "conversational",
            "response_schema": [],
            "is_seed": True,
            "prompt_version": 1,
        },
    }
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET" and request.url.path == "/agents":
            return httpx.Response(200, json=list(agents.values()))
        if request.method == "PUT":
            body = json.loads(request.content)
            return httpx.Response(200, json={**agents["r8"], **body, "prompt_version": 2})
        if request.method == "POST":
            return httpx.Response(201, json={**json.loads(request.content), "prompt_version": 1})
        return httpx.Response(404, json={"detail": "?"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli_main,
        "Client",
        lambda url, timeout, api_key=None, **kw: Client(url, timeout, transport=transport, api_key=api_key, **kw),
    )
    return calls


def test_export_then_apply_directory_is_a_noop_and_dry_run_sends_the_flag(api, tmp_path):
    runner = CliRunner()
    out = tmp_path / "agentes"
    exported = runner.invoke(cli_main.app, ["--json", "agents", "export", "-o", str(out)])
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.stdout)["agents"] == ["r8"]  # seed fica de fora

    noop = runner.invoke(cli_main.app, ["--json", "agents", "apply", "-f", str(out)])
    assert noop.exit_code == 0 and [i["action"] for i in json.loads(noop.stdout)["items"]] == ["unchanged"]
    assert not [c for c in api if c.method in ("PUT", "POST")]

    data = json.loads((out / "r8.json").read_text(encoding="utf-8"))
    data["instructions"] = ["Decida com cuidado."]
    (out / "r8.json").write_text(json.dumps(data), encoding="utf-8")
    (out / "r9.json").write_text(json.dumps({**data, "agent_type": "r9"}), encoding="utf-8")
    plan = runner.invoke(cli_main.app, ["--json", "agents", "apply", "-f", str(out), "--dry-run"])

    report = json.loads(plan.stdout)
    assert [(i["agent_type"], i["action"], i["changed"]) for i in report["items"]][0] == (
        "r8",
        "update",
        ["instructions"],
    )
    assert report["items"][1]["action"] == "create"
    writes = [c for c in api if c.method in ("PUT", "POST")]
    assert writes and all(c.url.params.get("dry_run") == "true" for c in writes)


def test_attachments_are_recorded_by_name_and_type_never_content(monkeypatch, r8):
    import base64

    from agent_service.agents.attachments import AttachmentIn

    fake_agent(monkeypatch, lambda doc: {"acao": "responder"})
    pdf = base64.b64encode(b"%PDF-1.4 conteudo secreto do aluno").decode()
    response = asyncio.run(
        analyze(
            AnalyzeRequest(
                agent_type=r8,
                document="veja o anexo",
                attachments=[AttachmentIn(content_base64=pdf, mime_type="application/pdf", filename="rg.pdf")],
            )
        )
    )

    trace = get_run_trace(response.run_id)
    [attachment] = trace.run.attachments
    assert attachment["filename"] == "rg.pdf" and attachment["kind"] == "file"
    assert attachment["mime_type"] == "application/pdf" and attachment["size_bytes"] > 0
    assert "conteudo secreto" not in json.dumps(trace.model_dump(mode="json"))
    assert pdf not in json.dumps(trace.model_dump(mode="json"))
