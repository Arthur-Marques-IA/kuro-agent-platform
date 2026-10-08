"""Memória de longo prazo por modo, resumo incremental da sessão e raciocínio por nível."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_service.agents.base import build_agent
from agent_service.memory import managers
from agent_service.models.params import ModelParamsError, provider_kwargs, validate_model_params
from agent_service.observability.tracing import _tool_output


def _agent(memory_backend: str, **kw):
    return build_agent(agent_id=f"a-{uuid4().hex[:6]}", name="A", instructions=["oi"], memory_backend=memory_backend, **kw)


def test_memory_off_adds_no_tool_no_prompt_block_no_manager():
    agent = _agent("none")
    assert agent.memory_manager is None
    assert not agent.enable_agentic_memory and not agent.update_memory_on_run and not agent.add_memories_to_context


def test_memory_auto_extracts_in_parallel_without_giving_the_model_a_tool():
    agent = _agent("auto")
    assert isinstance(agent.memory_manager, managers.LimitedMemoryManager)
    assert agent.update_memory_on_run and not agent.enable_agentic_memory and agent.add_memories_to_context


@pytest.mark.parametrize("mode", ["agentic", "common"])
def test_memory_agentic_and_its_legacy_name_give_the_model_the_tool(mode):
    agent = _agent(mode)
    assert agent.enable_agentic_memory and not agent.update_memory_on_run


def test_analysis_never_has_memory_or_summary():
    agent = _agent("agentic", kind="analysis", session_summary=True)
    assert agent.memory_manager is None and not agent.enable_session_summaries


def test_aux_model_is_used_for_memory_and_summary(monkeypatch):
    from agent_service.config import get_settings

    monkeypatch.setattr(get_settings(), "aux_model_id", "gemini-flash-lite-latest")
    agent = _agent("auto", session_summary=True, num_history_runs=6)
    assert agent.memory_manager.model.id == "gemini-flash-lite-latest"
    assert agent.session_summary_manager.model.id == "gemini-flash-lite-latest"
    assert agent.enable_session_summaries and agent.add_session_summary_to_context


def test_only_the_latest_memories_go_to_the_prompt(monkeypatch):
    manager = managers.LimitedMemoryManager(limit=2)
    memories = [SimpleNamespace(memory=f"m{i}", updated_at=i) for i in (3, 1, 4, 2)]
    monkeypatch.setattr(managers.MemoryManager, "get_user_memories", lambda self, user_id=None: memories)
    assert [m.memory for m in manager.get_user_memories("u")] == ["m3", "m4"]


def test_summary_runs_once_per_history_window_and_carries_the_previous_summary(monkeypatch):
    manager = managers.IncrementalSummaryManager(model=None, every=3)
    calls = []
    monkeypatch.setattr(managers.SessionSummaryManager, "create_session_summary", lambda self, session, run_metrics=None: calls.append(len(session.runs)))

    def session(n):
        return SimpleNamespace(runs=[SimpleNamespace(parent_run_id=None) for _ in range(n)], summary=SimpleNamespace(summary="Lead Arthur, quer curso técnico."))

    for n in range(1, 10):
        manager.create_session_summary(session(n))
    # Dentro da primeira janela o histórico já leva tudo: nenhum resumo. Depois, um por janela.
    assert calls == [6, 9]

    from agno.models.message import Message

    message = manager.get_system_message([Message(role="user", content="oi")], {"type": "json_object"})
    assert message.content.startswith("Resumo anterior:\nLead Arthur, quer curso técnico.")


@pytest.mark.parametrize(
    ("provider", "model", "level", "expected"),
    [
        ("google", "gemini-3.5-flash", "low", {"thinking_level": "low"}),
        ("google", "gemini-flash-latest", "off", {"thinking_level": "minimal"}),
        ("google", "gemini-2.5-flash", "medium", {"thinking_budget": 4096}),
        ("google", "gemini-2.5-flash", "off", {"thinking_budget": 0}),
        ("google", "gemini-2.5-pro", "off", {"thinking_budget": 128}),
        ("openai", "gpt-5", "off", {"reasoning_effort": "minimal"}),
        ("openai", "o4-mini", "high", {"reasoning_effort": "high"}),
        ("openai", "gpt-4.1-mini", "high", {}),
    ],
)
def test_reasoning_level_is_translated_per_model(provider, model, level, expected):
    params = validate_model_params({"reasoning": level}, provider)
    assert provider_kwargs(provider, params, model) == expected


def test_reasoning_validation_and_explicit_budget_wins():
    assert validate_model_params({"reasoning": "auto"}, "google") is None
    for bad, provider in (({"reasoning": "máximo"}, "google"), ({"reasoning": "low"}, "anthropic")):
        with pytest.raises(ModelParamsError):
            validate_model_params(bad, provider)
    params = validate_model_params({"reasoning": "high", "thinking_budget": 0}, "google")
    assert provider_kwargs("google", params, "gemini-2.5-flash") == {"thinking_budget": 0}


def test_gemini3_drops_sampling_and_thinking_budget():
    params = validate_model_params(
        {"temperature": 0.3, "top_p": 0.9, "max_tokens": 512, "thinking_budget": 0, "reasoning": "high"}, "google"
    )
    assert provider_kwargs("google", params, "gemini-3.5-flash") == {"max_output_tokens": 512, "thinking_level": "high"}
    assert provider_kwargs("google", params, "gemini-flash-latest") == {"max_output_tokens": 512, "thinking_level": "high"}
    # Gemini 2.x segue aceitando os três
    assert provider_kwargs("google", params, "gemini-2.5-flash") == {
        "temperature": 0.3, "top_p": 0.9, "max_output_tokens": 512, "thinking_budget": 0,
    }


def test_memory_tool_success_is_not_shown_as_an_error():
    ok = SimpleNamespace(tool_name="update_user_memory", result="No response from model")
    other = SimpleNamespace(tool_name="consulta", result="No response from model")
    assert _tool_output(ok) == managers.MEMORY_TOOL_OUTPUT
    assert _tool_output(other) == "No response from model"
