"""Um backend de memória de longo prazo por vez (`agents/base.py::build_agent`)."""

from agent_service.agents.base import build_agent


def test_common_backend_uses_agno_memory():
    agent = build_agent(agent_id="x", name="x", instructions=["x"], memory_backend="common")
    assert agent.memory_manager is not None
    assert agent.enable_agentic_memory is True
    assert agent.add_memories_to_context is True
    assert not agent.pre_hooks and not agent.post_hooks


def test_mem0_is_gone_and_a_stored_mem0_agent_runs_without_memory():
    """O Mem0 saiu; uma definição antiga com `memory_backend="mem0"` no banco não pode
    derrubar o agente — roda sem memória de longo prazo."""
    agent = build_agent(agent_id="x", name="x", instructions=["x"], memory_backend="mem0")  # type: ignore[arg-type]
    assert agent.memory_manager is None
    assert agent.enable_agentic_memory is False
    assert agent.add_memories_to_context is False
    assert not agent.pre_hooks and not agent.post_hooks
    assert agent.add_history_to_context is True


def test_api_rejects_mem0():
    import pytest
    from pydantic import ValidationError

    from agent_service.api.agents_routes import AgentDefinitionIn

    with pytest.raises(ValidationError):
        AgentDefinitionIn(agent_type="x", name="x", instructions=["x"], memory_backend="mem0")


def test_analysis_has_no_memory():
    agent = build_agent(agent_id="x", name="x", instructions=["x"], memory_backend="agentic", kind="analysis")
    assert agent.memory_manager is None
    assert agent.enable_agentic_memory is False
    assert agent.add_memories_to_context is False
    assert not agent.pre_hooks and not agent.post_hooks
