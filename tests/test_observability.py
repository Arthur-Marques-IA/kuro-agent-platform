"""Testes da instrumentação com Langfuse.

O cliente do Langfuse aqui é real, mas aponta para um `InMemorySpanExporter`:
os spans são inspecionados no processo, sem rede e sem Langfuse rodando.
"""

import asyncio
from contextlib import aclosing
from uuid import uuid4

import pytest
from agno.metrics import RunMetrics
from agno.run.agent import (
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
    RunStartedEvent,
)
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from agent_service.api.observability_routes import ScoreIn, create_score, observability_config, run_trace
from agent_service.observability import tracing
from agent_service.observability.tracing import RunContext, trace_id_for_run

USER_ID = "user.id"
SESSION_ID = "session.id"
LEVEL = "langfuse.observation.level"
STATUS_MESSAGE = "langfuse.observation.status_message"
OUTPUT = "langfuse.observation.output"
VERSION = "langfuse.version"


class FakeAgent:
    """Mesma interface que a instrumentação usa do Agno: `arun(..., stream=True)`."""

    def __init__(self, *events) -> None:
        self.events = events
        self.calls: list[dict] = []

    def arun(self, message, **kwargs):
        self.calls.append({"message": message, **kwargs})

        async def stream():
            for event in self.events:
                yield event

        return stream()


class HangingAgent(FakeAgent):
    def arun(self, message, **kwargs):
        self.calls.append({"message": message, **kwargs})

        async def stream():
            yield RunStartedEvent(run_id=kwargs.get("run_id"))
            yield RunContentEvent(content="metade ")
            await asyncio.Event().wait()  # nunca termina: simula um run em andamento

        return stream()


def make_run(**overrides) -> RunContext:
    fields = {
        "endpoint": "chat.stream",
        "agent_type": "conversational",
        "agent_name": "Agente Conversacional",
        "prompt_version": 3,
        "user_id": "u1",
        "session_id": "s1",
        "message": "oi",
        "dependencies": {"nome": "Maria"},
    }
    return RunContext(**{**fields, **overrides})


def completed_run(run_id: str) -> FakeAgent:
    return FakeAgent(
        RunStartedEvent(run_id=run_id, model="gemini-2.5-flash"),
        RunContentEvent(content="olá"),
        RunCompletedEvent(
            content="olá",
            metrics=RunMetrics(input_tokens=5, output_tokens=1, total_tokens=6),
        ),
    )


@pytest.fixture
def langfuse(monkeypatch):
    """Cliente do Langfuse exportando para a memória (chave nova por teste: o SDK
    mantém um recurso singleton por public_key)."""
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-test-{uuid4()}",
        secret_key="sk-lf-test",
        base_url="http://langfuse.invalid",
        environment="test",
        tracer_provider=TracerProvider(),
        span_exporter=exporter,
    )
    monkeypatch.setattr(tracing, "_client", client)
    yield client, exporter
    client.shutdown()


def drain(agent, run: RunContext) -> list:
    async def consume():
        return [event async for event in tracing.traced_run_events(agent, run)]

    return asyncio.run(consume())


def root_span(client: Langfuse, exporter: InMemorySpanExporter, name: str = "chat.stream"):
    client.flush()
    return next(span for span in exporter.get_finished_spans() if span.name == name)


def test_run_id_is_generated_before_the_run_and_passed_to_agno(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    run = make_run()
    agent = completed_run(run.run_id)

    events = drain(agent, run)

    assert [event.event for event in events] == ["RunStarted", "RunContent", "RunCompleted"]
    assert agent.calls[0]["run_id"] == run.run_id
    assert agent.calls[0]["stream"] is True and agent.calls[0]["stream_events"] is True
    assert agent.calls[0]["add_dependencies_to_context"] is True


def test_trace_id_is_derived_from_the_run_id():
    run = make_run()

    # Determinístico: o console reencontra o trace de um run antigo só com o run_id.
    assert run.trace_id == trace_id_for_run(run.run_id) == Langfuse.create_trace_id(seed=run.run_id)
    assert run.trace_id != trace_id_for_run("outro-run")


def test_run_becomes_a_langfuse_trace_with_user_session_and_prompt_version(langfuse):
    client, exporter = langfuse
    run = make_run()

    drain(completed_run(run.run_id), run)

    span = root_span(client, exporter)
    assert f"{span.context.trace_id:032x}" == run.trace_id
    assert span.attributes[USER_ID] == "u1"
    assert span.attributes[SESSION_ID] == "s1"
    assert span.attributes[VERSION] == "prompt-v3"
    assert span.attributes[OUTPUT] == "olá"


def test_failed_run_is_marked_as_error(langfuse):
    client, exporter = langfuse
    run = make_run()
    agent = FakeAgent(
        RunStartedEvent(run_id=run.run_id),
        RunErrorEvent(content="quota excedida", error_type="ModelProviderError"),
    )

    drain(agent, run)

    span = root_span(client, exporter)
    assert span.attributes[LEVEL] == "ERROR"
    assert span.attributes[STATUS_MESSAGE] == "quota excedida"


def test_client_disconnecting_closes_the_trace_as_interrupted(langfuse):
    client, exporter = langfuse
    run = make_run()

    async def stop_after_first_event():
        async with aclosing(tracing.traced_run_events(HangingAgent(), run)) as events:
            async for _ in events:
                break
        await asyncio.sleep(0.05)  # deixa a task do run tratar o cancelamento

    asyncio.run(stop_after_first_event())

    span = root_span(client, exporter)
    assert span.attributes[LEVEL] == "WARNING"
    assert span.attributes[STATUS_MESSAGE] == tracing.RUN_INTERRUPTED


def test_feedback_score_lands_on_the_trace_of_the_run(monkeypatch):
    scores: list[dict] = []
    monkeypatch.setattr(
        tracing, "_client", type("FakeClient", (), {"create_score": lambda self, **kw: scores.append(kw)})()
    )

    first = create_score(ScoreIn(run_id="run-42", value=1, user_id="u1", comment="mandou bem"))
    create_score(ScoreIn(run_id="run-42", value=0, user_id="u1"))

    assert first.trace_id == trace_id_for_run("run-42")
    assert [score["value"] for score in scores] == [1, 0]
    assert scores[0]["trace_id"] == first.trace_id
    assert scores[0]["data_type"] == "BOOLEAN"
    assert scores[0]["comment"] == "mandou bem"
    # Mesmo id de score: o voto novo do usuário substitui o anterior no trace.
    assert scores[0]["score_id"] == scores[1]["score_id"]


def test_named_scores_are_numeric(monkeypatch):
    scores: list[dict] = []
    monkeypatch.setattr(
        tracing, "_client", type("FakeClient", (), {"create_score": lambda self, **kw: scores.append(kw)})()
    )

    create_score(ScoreIn(run_id="run-42", name="relevancia", value=0.8))

    assert scores[0]["data_type"] == "NUMERIC"
    assert scores[0]["score_id"] is None


def test_feedback_only_accepts_0_or_1():
    with pytest.raises(ValidationError):
        ScoreIn(run_id="run-42", value=0.5)


def test_scores_work_without_langfuse(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)

    score = create_score(ScoreIn(run_id="run-sem-langfuse", value=1, user_id="u1"))

    assert score.trace_id == trace_id_for_run("run-sem-langfuse")


def test_console_endpoints_degrade_without_langfuse(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)

    config = observability_config()
    link = run_trace("run-42")

    # Sem Langfuse as execuções continuam registradas, no trace store local.
    assert config.enabled is True and config.langfuse is False and config.project_url is None
    assert link.trace_id == trace_id_for_run("run-42") and link.trace_url is None


def test_run_trace_links_to_the_langfuse_ui(monkeypatch):
    monkeypatch.setattr(tracing, "project_url", lambda: "http://localhost:3100/project/agent-service")

    link = run_trace("run-42")

    assert link.trace_url == f"http://localhost:3100/project/agent-service/traces/{link.trace_id}"
