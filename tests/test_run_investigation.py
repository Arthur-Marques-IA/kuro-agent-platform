"""O que a tela do run precisa para investigar agentes que não são conversacionais:
o estado de cada turno de um procedural, a linha do tempo dos runs ligados por uma
chave de correlação e o 👎 de um analista virando referência (caso de eval)."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from agno.run.agent import RunCompletedEvent, RunContentEvent
from fastapi import HTTPException

from agent_service.agents import procedure_runner
from agent_service.api.agents_routes import AgentDefinitionIn, create_agent, delete_agent
from agent_service.api.observability_routes import (
    RunFeedbackIn,
    export_cases,
    get_run_trace,
    observability_config,
    related_runs,
    run_feedback,
)
from agent_service.api.routes import ChatRequest, chat
from agent_service.config import get_settings
from agent_service.observability import run_store, trace_store, tracing
from agent_service.observability.tracing import record_run_without_model


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    yield
    trace_store.set_trace_store(None)


def _record(agent_type, *, metadata=None, message="doc", output='{"acao": "nada"}', at=None, endpoint="analyze"):
    run_id = f"run-{uuid4().hex[:10]}"
    run_store._write_run(
        run_store.RunRecord(
            run_id=run_id,
            trace_id=uuid4().hex,
            agent_type=agent_type,
            agent_name=None,
            prompt_version=1,
            endpoint=endpoint,
            user_id="regente",
            session_id="grupo-18",
            message=message,
            output=output,
            started_at=at or datetime.now(timezone.utc),
            input={"message": message, "dependencies": {"grupo": 18}},
            metadata=metadata or {},
        )
    )
    return run_id


# -- estado procedural por turno ---------------------------------------------------------

STAGES = [
    {
        "id": "identificacao",
        "goal": "Obter o CPF e o nome",
        "fields": [
            {"name": "cpf", "type": "string", "label": "CPF", "required": True, "pattern": r"^\d{11}$"},
            {"name": "nome", "type": "string", "label": "Nome", "required": True},
        ],
    },
    {"id": "confirmacao", "type": "confirm"},
]


@pytest.fixture
def procedural_agent():
    name = f"proc-{uuid4().hex[:6]}"
    create_agent(
        AgentDefinitionIn(agent_type=name, name="Proc", instructions=["Colete."], kind="procedural", stages=STAGES)
    )
    yield name
    delete_agent(name)


def test_each_procedural_turn_records_its_own_state(procedural_agent, monkeypatch):
    extracoes = [{"nome": "Ana"}, {"cpf": "123"}, {"cpf": "12345678901"}]

    async def extract(definition, state, message, last_reply):
        return extracoes.pop(0), []

    def reply(agent, run):
        # A resposta do modelo, falsa — mas gravada como o tracing gravaria, com os extra_spans do turno.
        record_run_without_model(run, "resposta do modelo")

        async def stream():
            yield RunContentEvent(content="resposta do modelo")
            yield RunCompletedEvent(run_id=run.run_id, content="resposta do modelo")

        return stream()

    monkeypatch.setattr(procedure_runner, "extract", extract)
    monkeypatch.setattr(procedure_runner, "traced_run_events", reply)
    session = f"s-{uuid4().hex[:6]}"

    def turn(message):
        return asyncio.run(chat(ChatRequest(agent_type=procedural_agent, user_id="u", session_id=session, message=message)))

    first, second, third = turn("sou a Ana"), turn("cpf 123"), turn("cpf 12345678901")

    states = [get_run_trace(r.run_id).procedure_state for r in (first, second, third)]
    assert states[0]["stage"] == "identificacao" and [f["name"] for f in states[0]["missing"]] == ["cpf"]
    assert states[0]["collected"]["nome"] == "Ana" and states[0]["done"] is False
    assert "cpf" in states[1]["invalid"]
    # O terceiro é a confirmação, montada sem o modelo (record_run_without_model do próprio runner).
    assert states[2]["stage"] == "confirmacao" and states[2]["collected"]["cpf"] == "12345678901"
    assert states[2]["missing"] == []
    # É o mesmo state que o /chat devolveu.
    assert states[2] == third.state
    # O span de estado não conta como tool.
    assert get_run_trace(third.run_id).run.tool_calls in (None, 0)


def test_run_that_is_not_procedural_has_no_state():
    run_id = _record("qualquer")
    assert get_run_trace(run_id).procedure_state is None


# -- linha do tempo por correlação ----------------------------------------------------


def test_related_runs_share_the_correlation_key_across_agents_in_order():
    ciclo = uuid4().hex[:8]
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    triagem = _record("moderador", metadata={"ciclo_id": ciclo, "fase": "triagem"}, at=t0)
    base = _record("base-frequencia", metadata={"ciclo_id": ciclo}, at=t0 + timedelta(seconds=5), endpoint="/chat")
    redacao = _record("moderador", metadata={"ciclo_id": ciclo, "fase": "redacao"}, at=t0 + timedelta(seconds=9))
    _record("moderador", metadata={"ciclo_id": "outro-" + ciclo}, at=t0)

    related = related_runs(redacao)

    assert related.key == "ciclo_id" and related.value == ciclo
    assert [r.run_id for r in related.items] == [triagem, base, redacao]
    assert related.items[0].metadata["fase"] == "triagem"


def test_run_without_a_correlation_key_has_no_timeline():
    related = related_runs(_record("moderador", metadata={"grupo_id": "18"}))
    assert related.key is None and related.items == []


def test_correlation_keys_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("CORRELATION_METADATA_KEYS", "conversation_id, ciclo_id")
    get_settings.cache_clear()
    try:
        assert observability_config().correlation_keys == ["conversation_id", "ciclo_id"]
        conversa = uuid4().hex[:8]
        a = _record("r8", metadata={"conversation_id": conversa, "ciclo_id": "x"})
        b = _record("r8", metadata={"conversation_id": conversa})
        related = related_runs(b)
        assert related.key == "conversation_id" and {r.run_id for r in related.items} == {a, b}
    finally:
        get_settings.cache_clear()


def test_related_of_unknown_run_is_404():
    with pytest.raises(HTTPException) as exc:
        related_runs("nao-existe")
    assert exc.value.status_code == 404


# -- feedback de analista vira referência ----------------------------------------------


def test_analysis_feedback_saves_score_and_reference_that_exports_as_eval_case():
    agent = f"auditor-{uuid4().hex[:6]}"
    run_id = _record(agent, message="linha 1\nlinha 2", output='{"nota": 3, "falhas": ["a"]}')

    out = run_feedback(
        run_id,
        RunFeedbackIn(value=0, comment="a nota é 5", user_id="ana", reference={"nota": 5, "falhas": []}),
    )

    assert out.reference_saved is True
    trace = get_run_trace(run_id)
    assert trace.reference == {"nota": 5, "falhas": []}
    [score] = trace.scores
    assert (score.name, score.value, score.comment) == ("feedback", False, "a nota é 5")
    assert trace.run.feedback_down == 1
    [case] = export_cases(agent_type=agent, since=None, until=None, agent_version=None, limit=10)
    assert case.id == run_id and case.input == "linha 1\nlinha 2"
    assert case.expected == {"nota": 5, "falhas": []} and case.dependencies == {"grupo": 18}


def test_positive_feedback_records_only_the_score():
    agent = f"auditor-{uuid4().hex[:6]}"
    run_id = _record(agent)
    assert run_feedback(run_id, RunFeedbackIn(value=1)).reference_saved is False
    assert get_run_trace(run_id).reference is None
    assert export_cases(agent_type=agent, since=None, until=None, agent_version=None, limit=10) == []


def test_corrected_output_only_goes_with_a_negative_vote():
    run_id = _record("auditor")
    with pytest.raises(HTTPException) as exc:
        run_feedback(run_id, RunFeedbackIn(value=1, reference={"nota": 5}))
    assert exc.value.status_code == 422


def test_feedback_on_unknown_run_is_404():
    with pytest.raises(HTTPException) as exc:
        run_feedback("nao-existe", RunFeedbackIn(value=0))
    assert exc.value.status_code == 404
