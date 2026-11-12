"""Versão da configuração inteira (`agents/versions.py`), promote draft → prod e
o custo estimado das execuções."""

import asyncio
from contextlib import aclosing
from uuid import uuid4

import pytest
from agno.metrics import ModelMetrics, RunMetrics
from agno.run.agent import RunCompletedEvent, RunStartedEvent
from fastapi import HTTPException

from agent_service.agents.registry import get_agent_with_definition
from agent_service.agents.store import create_definition, delete_definition, get_feedback_note, save_feedback_note, update_definition
from agent_service.api.agents_routes import PromoteIn, get_revisions, promote_agent
from agent_service.api.observability_routes import get_run_trace
from agent_service.models.pricing import estimate_cost
from agent_service.observability import trace_store, tracing
from agent_service.observability.tracing import RunContext

SCHEMA = [{"name": "acao", "type": "string", "required": True}]


@pytest.fixture
def agente():
    name = f"r8-{uuid4().hex[:6]}"
    create_definition(agent_type=name, name="R8", instructions=["Decida."], kind="analysis", response_schema=SCHEMA)
    yield name
    delete_definition(name)


def test_every_behavior_change_is_a_new_version_and_going_back_reuses_the_old_one(agente):
    _, first = get_agent_with_definition(agente)
    assert first["agent_version"] == 1 and len(first["config_hash"]) == 12

    # Só o modelo muda — o prompt_version não, mas a versão da configuração sim.
    update_definition(agente, model_id="gemini-2.5-pro")
    _, second = get_agent_with_definition(agente)
    assert second["prompt_version"] == first["prompt_version"]
    assert second["agent_version"] == 2 and second["config_hash"] != first["config_hash"]

    update_definition(agente, model_id="gemini-2.5-flash")
    _, back = get_agent_with_definition(agente)
    assert (back["agent_version"], back["config_hash"]) == (first["agent_version"], first["config_hash"])

    revisions = get_revisions(agente)
    assert [r["version"] for r in revisions] == [2, 1]
    assert [r["current"] for r in revisions] == [False, True]


def test_renaming_is_not_a_behavior_change(agente):
    _, before = get_agent_with_definition(agente)
    update_definition(agente, name="R8 — vendas")
    _, after = get_agent_with_definition(agente)
    assert after["agent_version"] == before["agent_version"]


def test_feedback_rules_are_part_of_the_version():
    name = f"conv-{uuid4().hex[:6]}"
    create_definition(agent_type=name, name="Conv", instructions=["Oi."])
    try:
        _, before = get_agent_with_definition(name)
        save_feedback_note(name, [{"id": "r1", "texto": "confirme o CPF"}])
        _, after = get_agent_with_definition(name)
        assert after["agent_version"] == before["agent_version"] + 1
    finally:
        delete_definition(name)


def test_promote_copies_the_behavior_and_keeps_the_target_name(agente):
    draft = f"{agente}-draft"
    created = promote_agent(agente, PromoteIn(to=draft))
    assert created["previous_version"] is None and not created["unchanged"]

    update_definition(draft, instructions=["Decida com cuidado."], model_id="gemini-2.5-pro")
    update_definition(agente, name="R8 produção")
    promoted = promote_agent(draft, PromoteIn(to=agente))

    assert promoted["agent"]["instructions"] == ["Decida com cuidado."]
    assert promoted["agent"]["model_id"] == "gemini-2.5-pro"
    assert promoted["agent"]["name"] == "R8 produção"
    assert promoted["previous_version"] == 1 and promoted["agent_version"] == 2

    again = promote_agent(draft, PromoteIn(to=agente))
    assert again["unchanged"] is True
    delete_definition(draft)


def test_promote_carries_feedback_rules_and_keeps_the_target_history():
    prod, draft = f"conv-{uuid4().hex[:6]}", f"conv-{uuid4().hex[:6]}"
    create_definition(agent_type=prod, name="Prod", instructions=["Oi."])
    save_feedback_note(prod, [{"id": "r1", "texto": "regra antiga"}])
    create_definition(agent_type=draft, name="Draft", instructions=["Oi."])
    try:
        promote_agent(draft, PromoteIn(to=prod))
        note = get_feedback_note(prod)
        assert note["rules"] == [] and note["version"] == 2  # a v1 continua para voltar
    finally:
        delete_definition(prod)
        delete_definition(draft)


def test_promote_rejects_same_agent_and_bad_names(agente):
    for target in (agente, "Nome Inválido"):
        with pytest.raises(HTTPException) as exc:
            promote_agent(agente, PromoteIn(to=target))
        assert exc.value.status_code == 422


def test_run_records_the_agent_version_and_an_estimated_cost(monkeypatch, agente):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    _, definition = get_agent_with_definition(agente)
    run = RunContext(
        endpoint="analyze",
        agent_type=agente,
        agent_name="R8",
        prompt_version=definition["prompt_version"],
        user_id="analysis",
        session_id="s",
        message="doc",
        agent_version=definition["agent_version"],
        config_hash=definition["config_hash"],
    )
    metrics = RunMetrics(
        input_tokens=1000,
        output_tokens=100,
        total_tokens=1100,
        details={"model": [ModelMetrics(id="gemini-2.5-flash", provider="Google", input_tokens=1000, output_tokens=100, total_tokens=1100)]},
    )

    class Agent:
        def arun(self, message, **kwargs):
            async def stream():
                yield RunStartedEvent(run_id=run.run_id)
                yield RunCompletedEvent(content="{}", metrics=metrics)

            return stream()

    async def consume():
        async with aclosing(tracing.traced_run_events(Agent(), run)) as events:
            return [e async for e in events]

    asyncio.run(consume())
    trace = get_run_trace(run.run_id)

    assert trace.run.agent_version == definition["agent_version"]
    assert trace.run.config_hash == definition["config_hash"]
    assert trace.run.cost_usd == pytest.approx((1000 * 0.30 + 100 * 2.50) / 1_000_000)


def test_cost_estimate_bills_gemini_thinking_and_knows_dated_models():
    assert estimate_cost(provider="Google", model_id="gemini-2.5-flash", input_tokens=0, output_tokens=0, reasoning_tokens=1_000_000) == 2.5
    assert estimate_cost(provider="OpenAI", model_id="gpt-4.1-mini-2025-04-14", input_tokens=1_000_000, output_tokens=0) == 0.4
    assert estimate_cost(provider="Google", model_id="gemini-3.5-flash", input_tokens=1_000_000, output_tokens=1_000_000) == 10.5
    assert estimate_cost(provider="Google", model_id="gemini-3.5-flash-lite", input_tokens=1_000_000, output_tokens=0) == 0.3
    assert estimate_cost(provider="OpenAI", model_id="modelo-sem-preco", input_tokens=10, output_tokens=10) is None
