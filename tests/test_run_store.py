"""Testes do trace store local (`run_store.py`): as execuções ficam no banco do
serviço e são lidas de volta pelas rotas `/observability/*` — sem Langfuse.

Cada teste usa um `agent_type` próprio, porque o banco é compartilhado pela suíte.
"""

import asyncio
from contextlib import aclosing
from uuid import uuid4

import pytest
from agno.metrics import ModelMetrics, RunMetrics, ToolCallMetrics
from agno.models.response import ToolExecution
from agno.run.agent import (
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
    RunStartedEvent,
    ToolCallCompletedEvent,
    ToolCallErrorEvent,
)

from agent_service.api.observability_routes import (
    ScoreIn,
    create_score,
    get_run_trace,
    list_agent_runs,
    list_runs,
    list_sessions,
    run_stats,
)
from agent_service.observability import trace_store, tracing
from agent_service.observability.tracing import RunContext, trace_id_for_run


class FakeAgent:
    def __init__(self, *events) -> None:
        self.events = events

    def arun(self, message, **kwargs):
        async def stream():
            for event in self.events:
                yield event

        return stream()


class HangingAgent:
    def arun(self, message, **kwargs):
        async def stream():
            yield RunStartedEvent(run_id=kwargs.get("run_id"), model="gemini-2.5-flash")
            yield RunContentEvent(content="metade ")
            await asyncio.Event().wait()

        return stream()


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    """Sem Langfuse e lendo do banco — o modo padrão do serviço."""
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    yield
    trace_store.set_trace_store(None)


@pytest.fixture
def agent_type() -> str:
    return f"agente-{uuid4().hex[:8]}"


def make_run(agent_type: str, **overrides) -> RunContext:
    fields = {
        "endpoint": "chat",
        "agent_type": agent_type,
        "agent_name": "Agente de teste",
        "prompt_version": 4,
        "user_id": "u1",
        "session_id": f"s-{uuid4().hex[:6]}",
        "message": "meu wifi caiu",
        "dependencies": {"cpf": "12345678900"},
    }
    return RunContext(**{**fields, **overrides})


def completed(run_id: str) -> FakeAgent:
    tool = ToolExecution(
        tool_name="consulta_contrato",
        tool_args={"assunto": "internet"},
        result='{"plano": "300MB"}',
        metrics=ToolCallMetrics(start_time=1_790_000_000.0, end_time=1_790_000_000.5, duration=0.5),
    )
    metrics = RunMetrics(
        input_tokens=120,
        output_tokens=30,
        total_tokens=150,
        cost=0.002,
        details={
            "model": [
                ModelMetrics(
                    id="gemini-2.5-flash",
                    provider="Google",
                    input_tokens=120,
                    output_tokens=30,
                    total_tokens=150,
                    cost=0.002,
                )
            ]
        },
    )
    return FakeAgent(
        RunStartedEvent(run_id=run_id, model="gemini-2.5-flash"),
        ToolCallCompletedEvent(tool=tool),
        RunContentEvent(content="Seu plano é 300MB."),
        RunCompletedEvent(content="Seu plano é 300MB.", metrics=metrics),
    )


def drain(agent, run: RunContext) -> list:
    async def consume():
        async with aclosing(tracing.traced_run_events(agent, run)) as events:
            return [event async for event in events]

    return asyncio.run(consume())


def test_completed_run_is_recorded_with_tokens_model_and_spans(agent_type):
    run = make_run(agent_type)
    drain(completed(run.run_id), run)

    trace = get_run_trace(run.run_id)

    assert trace.run.status == "success"
    assert trace.run.trace_id == trace_id_for_run(run.run_id)
    assert trace.run.prompt_version == 4
    assert trace.run.message == "meu wifi caiu"
    assert trace.run.output == "Seu plano é 300MB."
    assert trace.run.model == "gemini-2.5-flash"
    assert (trace.run.input_tokens, trace.run.output_tokens, trace.run.total_tokens) == (120, 30, 150)
    assert trace.run.cost_usd == pytest.approx(0.002)
    assert trace.run.latency_ms is not None

    root, *children = trace.spans
    assert root.parent_id is None and root.name == "chat"
    assert root.input["dependencies"] == {"cpf": "12345678900"}
    assert {c.parent_id for c in children} == {root.id}
    tool = next(c for c in children if c.type == "TOOL")
    assert tool.name == "consulta_contrato"
    assert tool.input == {"assunto": "internet"} and tool.latency_ms == 500.0
    generation = next(c for c in children if c.type == "GENERATION")
    assert generation.model == "gemini-2.5-flash" and generation.total_tokens == 150


def test_run_error_event_marks_the_run_as_error(agent_type):
    run = make_run(agent_type)
    drain(FakeAgent(RunStartedEvent(run_id=run.run_id), RunErrorEvent(content="cota esgotada")), run)

    trace = get_run_trace(run.run_id)

    assert trace.run.status == "error"
    assert trace.run.status_message == "cota esgotada"


def test_failed_tool_call_becomes_an_error_span(agent_type):
    run = make_run(agent_type)
    tool = ToolExecution(tool_name="cep", tool_args={"cep": "000"}, tool_call_error=True)
    drain(
        FakeAgent(
            RunStartedEvent(run_id=run.run_id),
            ToolCallErrorEvent(tool=tool, error="CEP inválido"),
            RunCompletedEvent(content="Não achei o CEP."),
        ),
        run,
    )

    span = next(s for s in get_run_trace(run.run_id).spans if s.type == "TOOL")

    assert span.level == "ERROR" and span.status_message == "CEP inválido"


def test_client_disconnecting_records_the_run_as_interrupted(agent_type):
    run = make_run(agent_type)

    async def consume_first_chunk_and_leave():
        async with aclosing(tracing.traced_run_events(HangingAgent(), run)) as events:
            async for event in events:
                if isinstance(event, RunContentEvent):
                    return

    asyncio.run(consume_first_chunk_and_leave())

    trace = get_run_trace(run.run_id)
    assert trace.run.status == "interrupted"
    assert trace.run.output == "metade "


def test_exception_in_the_run_is_recorded_as_error(agent_type):
    class BrokenAgent:
        def arun(self, message, **kwargs):
            async def stream():
                yield RunStartedEvent(run_id=kwargs.get("run_id"))
                raise RuntimeError("provedor fora do ar")

            return stream()

    run = make_run(agent_type)
    with pytest.raises(RuntimeError):
        drain(BrokenAgent(), run)

    trace = get_run_trace(run.run_id)
    assert trace.run.status == "error" and trace.run.status_message == "provedor fora do ar"


def test_list_runs_paginates_newest_first_and_counts_feedback(agent_type):
    runs = [make_run(agent_type) for _ in range(3)]
    for run in runs:
        drain(completed(run.run_id), run)
    create_score(ScoreIn(run_id=runs[-1].run_id, value=1, user_id="u1"))
    create_score(ScoreIn(run_id=runs[-1].run_id, value=0, user_id="u2"))
    # Votar de novo substitui o voto do mesmo usuário, não soma.
    create_score(ScoreIn(run_id=runs[-1].run_id, value=1, user_id="u2"))

    first = list_agent_runs(agent_type, limit=2)
    second = list_agent_runs(agent_type, limit=2, cursor=first.next_cursor)

    ids = [r.run_id for r in first.items + second.items]
    assert ids == [r.run_id for r in reversed(runs)]
    assert first.next_cursor is not None and second.next_cursor is None
    assert (first.items[0].feedback_up, first.items[0].feedback_down) == (2, 0)


def test_sessions_and_stats_aggregate_the_whole_history(agent_type):
    session = f"s-{uuid4().hex[:6]}"
    ok = make_run(agent_type, session_id=session)
    failed = make_run(agent_type, session_id=session)
    drain(completed(ok.run_id), ok)
    drain(FakeAgent(RunStartedEvent(run_id=failed.run_id), RunErrorEvent(content="falhou")), failed)

    sessions = list_sessions(agent_type=agent_type)
    stats = run_stats(agent_type=agent_type)

    [summary] = sessions.items
    assert summary.session_id == session and summary.run_count == 2
    assert summary.error_count == 1 and summary.total_tokens == 150
    assert summary.agent_types == [agent_type]
    assert stats.total_runs == 2 and stats.status_counts == {"success": 1, "error": 1}
    assert stats.total_tokens == 150
    assert [b.runs for b in stats.buckets] == [2]


def test_search_finds_sessions_by_a_piece_of_the_id_and_ignores_wildcards(agent_type):
    wanted = make_run(agent_type, session_id="busca-alvo-123")
    other = make_run(agent_type, session_id="busca-outra-999")
    drain(completed(wanted.run_id), wanted)
    drain(completed(other.run_id), other)

    [hit] = list_sessions(agent_type=agent_type, search="alvo").items
    assert hit.session_id == "busca-alvo-123"
    assert len(list_sessions(agent_type=agent_type, search="BUSCA").items) == 2  # sem diferenciar maiúsculas
    assert list_sessions(agent_type=agent_type, search="%").items == []  # % não vira curinga
    assert len(list_sessions(agent_type=agent_type, search="  ").items) == 2  # só espaços = sem filtro


def test_search_finds_a_run_by_its_whole_run_id_or_trace_id(agent_type):
    wanted = make_run(agent_type, session_id="busca-id-1")
    other = make_run(agent_type, session_id="busca-id-2")
    drain(completed(wanted.run_id), wanted)
    drain(completed(other.run_id), other)
    trace_id = trace_id_for_run(wanted.run_id)

    assert [r.run_id for r in list_runs(agent_type=agent_type, search=wanted.run_id).items] == [wanted.run_id]
    assert [r.run_id for r in list_runs(agent_type=agent_type, search=trace_id).items] == [wanted.run_id]
    assert list_runs(agent_type=agent_type, search=wanted.run_id[:8]).items == []  # trecho do id não acha


def test_stats_can_leave_the_dry_runs_out(agent_type):
    real = make_run(agent_type)
    test = make_run(agent_type, metadata={"dry_run": "true"})
    drain(completed(real.run_id), real)
    drain(completed(test.run_id), test)

    assert run_stats(agent_type=agent_type).total_runs == 2
    assert run_stats(agent_type=agent_type, include_dry_run=False).total_runs == 1


def test_a_failing_write_never_breaks_the_run(agent_type, monkeypatch, caplog):
    from agent_service.observability import run_store

    def explode(record):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(run_store, "_write_run", explode)
    run = make_run(agent_type)

    events = drain(completed(run.run_id), run)

    assert events[-1].content == "Seu plano é 300MB."
    assert "Falha ao gravar o run" in caplog.text
