from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, update

from agent_service.agents import registry
from agent_service.agents.registry import UnknownAgentTypeError, get_agent, get_agent_with_definition, list_agent_types
from agent_service.agents.store import create_definition, delete_definition, save_feedback_note, update_definition
from agent_service.db import get_db
from agent_service.tools import store as tool_store
from agent_service.tools.registry import ToolBuildError, UnknownToolError


@contextmanager
def contando_consultas():
    """Cada SQL que o engine manda ao banco durante o bloco."""
    engine = get_db().db_engine
    emitidas: list[str] = []

    def anota(conn, cursor, statement, parameters, context, executemany):
        emitidas.append(statement)

    event.listen(engine, "before_cursor_execute", anota)
    try:
        yield emitidas
    finally:
        event.remove(engine, "before_cursor_execute", anota)


def _carimba_tool(tool_name: str) -> None:
    """O `updated_at` do SQLite dos testes tem resolução de segundo: uma edição no
    mesmo segundo não muda o carimbo. Força um carimbo novo, como faria uma edição
    de verdade (inclusive vinda de outro processo) no Postgres."""
    tabela = tool_store.tool_definitions
    with get_db().db_engine.begin() as conn:
        conn.execute(
            update(tabela)
            .where(tabela.c.tool_name == tool_name)
            .values(updated_at=datetime.now() + timedelta(minutes=1))
        )


@pytest.fixture
def agente_com_tools():
    sufixo = uuid4().hex[:6]
    tools = [f"t1_{sufixo}", f"t2_{sufixo}", f"t3_{sufixo}"]
    for nome in tools:
        tool_store.create_tool(
            tool_name=nome,
            kind="builtin",
            label=nome,
            description=None,
            config={"builtin_id": "calculator", "params": {}},
        )
    agent_type = f"cache-{sufixo}"
    create_definition(agent_type=agent_type, name="Cache", instructions=["Seja breve."], tools=tools)
    yield agent_type, tools
    delete_definition(agent_type)
    for nome in tools:
        tool_store.delete_tool(nome)


def test_warm_call_is_a_single_query_whatever_the_number_of_tools(agente_com_tools):
    agent_type, tools = agente_com_tools
    primeiro, _ = get_agent_with_definition(agent_type)  # frio: monta e registra a versão

    with contando_consultas() as emitidas:
        agente, definition = get_agent_with_definition(agent_type)

    assert agente is primeiro
    assert definition["tools"] == tools and definition["agent_version"] >= 1
    assert len(emitidas) == 1, emitidas


def test_returned_definition_is_a_copy_of_the_cached_one(agente_com_tools):
    agent_type, _ = agente_com_tools
    agente, definition = get_agent_with_definition(agent_type)
    definition["instructions"].append("mexido por quem chamou")
    # Nem o que roda dentro do Agent (a lista de instructions foi parar nele) pode
    # alterar a definição em cache: se alterasse, toda requisição remontaria o agente.
    if isinstance(agente.instructions, list):
        agente.instructions.append("mexido durante um run")
    with contando_consultas() as emitidas:
        mesmo, de_novo = get_agent_with_definition(agent_type)
    assert de_novo["instructions"] == ["Seja breve."]
    assert mesmo is agente and len(emitidas) == 1


def test_definition_change_rebuilds(agente_com_tools):
    agent_type, _ = agente_com_tools
    antes, _ = get_agent_with_definition(agent_type)
    update_definition(agent_type, instructions=["Seja detalhado."])
    depois, definition = get_agent_with_definition(agent_type)
    assert depois is not antes
    assert "Seja detalhado." in depois.instructions and definition["instructions"] == ["Seja detalhado."]


def test_tool_change_rebuilds(agente_com_tools):
    agent_type, tools = agente_com_tools
    antes, _ = get_agent_with_definition(agent_type)
    tool_store.update_tool(tools[1], label="outro rótulo")
    _carimba_tool(tools[1])
    depois, _ = get_agent_with_definition(agent_type)
    assert depois is not antes
    assert get_agent_with_definition(agent_type)[0] is depois  # e volta a ficar quente


def test_disabled_tool_is_noticed(agente_com_tools):
    agent_type, tools = agente_com_tools
    get_agent_with_definition(agent_type)
    tool_store.update_tool(tools[0], enabled=False)
    _carimba_tool(tools[0])
    with pytest.raises(ToolBuildError, match="desativada"):
        get_agent_with_definition(agent_type)


def test_removed_tool_is_noticed(agente_com_tools):
    agent_type, tools = agente_com_tools
    get_agent_with_definition(agent_type)
    tool_store.delete_tool(tools[2])
    with pytest.raises(UnknownToolError):
        get_agent_with_definition(agent_type)


def test_added_tool_rebuilds(agente_com_tools):
    agent_type, tools = agente_com_tools
    get_agent_with_definition(agent_type)
    update_definition(agent_type, tools=[*tools, "calculator"])
    agente, definition = get_agent_with_definition(agent_type)
    assert definition["tools"][-1] == "calculator"
    assert registry._cache[agent_type].tool_names == (*tools, "calculator")
    with contando_consultas() as emitidas:
        assert get_agent_with_definition(agent_type)[0] is agente
    assert len(emitidas) == 1


def test_feedback_note_edit_rebuilds_even_in_the_same_second(agente_com_tools):
    agent_type, _ = agente_com_tools
    save_feedback_note(agent_type, [{"id": "r1", "texto": "cumprimente pelo nome"}])
    antes, _ = get_agent_with_definition(agent_type)
    save_feedback_note(agent_type, [{"id": "r1", "texto": "nunca prometa prazo"}])
    depois, _ = get_agent_with_definition(agent_type)
    assert depois is not antes
    assert any("nunca prometa prazo" in i for i in depois.instructions)


def test_removed_agent_is_unknown_and_leaves_the_cache(agente_com_tools):
    agent_type, _ = agente_com_tools
    get_agent_with_definition(agent_type)
    delete_definition(agent_type)
    with pytest.raises(UnknownAgentTypeError):
        get_agent_with_definition(agent_type)
    assert agent_type not in registry._cache


def test_list_agent_types_includes_conversational():
    assert "conversational" in list_agent_types()


def test_get_agent_returns_same_cached_instance():
    agent_a = get_agent("conversational")
    agent_b = get_agent("conversational")
    assert agent_a is agent_b


def test_get_agent_unknown_type_raises():
    with pytest.raises(UnknownAgentTypeError):
        get_agent("does-not-exist")


def test_analysis_agent_gets_output_schema():
    create_definition(
        agent_type="teste-analista",
        name="Teste analista",
        instructions=["Extraia os campos."],
        kind="analysis",
        response_schema=[{"name": "valor", "type": "number", "required": True}],
    )
    try:
        agent, definition = get_agent_with_definition("teste-analista")
        assert definition["kind"] == "analysis"
        assert agent.output_schema is not None
        assert "valor" in agent.output_schema.model_fields
        assert agent.add_history_to_context is False
    finally:
        delete_definition("teste-analista")


def test_feedback_note_change_invalidates_cache():
    create_definition(agent_type="teste-feedback", name="Teste feedback", instructions=["Seja breve."])
    try:
        agent_before, _ = get_agent_with_definition("teste-feedback")
        save_feedback_note("teste-feedback", [{"id": "r1", "texto": "sempre cumprimente o cliente pelo nome"}])
        agent_after, _ = get_agent_with_definition("teste-feedback")
        assert agent_before is not agent_after
        assert any("sempre cumprimente" in i for i in agent_after.instructions)
    finally:
        delete_definition("teste-feedback")
