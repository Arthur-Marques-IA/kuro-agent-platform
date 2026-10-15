"""Configuração comum a todos os tipos de agente.

Cada tipo de agente (conversacional, orquestrador, analista) chama
`build_agent` em vez de instanciar `agno.agent.Agent` diretamente, garantindo
que todos compartilhem a mesma forma de resolver modelo, memória, persistência
e histórico — sem duplicar essa configuração em cada agente.
"""

from typing import Any, Literal

from agno.agent import Agent
from pydantic import BaseModel

from agent_service.db import get_db
from agent_service.documents.collections import get_collection
from agent_service.documents.retrieval import make_retriever
from agent_service.models.provider import get_model

MemoryBackendName = Literal["none", "auto", "agentic", "common", "mem0"]
"""`none` = sem memória de longo prazo; `auto` = extraída em paralelo, com o
modelo auxiliar; `agentic` = o modelo decide quando gravar (`common` é o nome
antigo dele); `mem0` = Mem0. Ver `memory/managers.py`."""
AgentKind = Literal["conversational", "analysis", "procedural"]


def build_agent(
    *,
    agent_id: str,
    name: str,
    instructions: list[str],
    tools: list[Any] | None = None,  # Toolkit/Function/callable já resolvidos, ver tools/registry.py
    model_provider: str | None = None,
    model_id: str | None = None,
    model_credential_id: str | None = None,
    model_params: dict[str, Any] | None = None,
    knowledge_collection: str | None = None,
    num_history_runs: int = 10,
    memory_backend: MemoryBackendName = "none",
    session_summary: bool = False,
    kind: AgentKind = "conversational",
    output_schema: type[BaseModel] | None = None,
) -> Agent:
    # Agentes "analysis" são one-shot: sem sessão contínua, sem histórico nem
    # memória — só o documento de entrada e o `output_schema` de saída.
    is_analysis = kind == "analysis"
    # Um backend de memória de longo prazo por vez: com "mem0", o Mem0 substitui
    # a memória do Agno (sem memory_manager, sem a tool `update_user_memory` e sem
    # as memórias do Agno no prompt). O histórico da sessão continua valendo.
    from agent_service.memory.managers import aux_model, memory_kwargs, summary_manager

    helper_model = aux_model(model_provider, model_id, model_credential_id)
    memory = memory_kwargs("none" if is_analysis else memory_backend, helper_model)
    summary = (
        {
            "enable_session_summaries": True,
            "add_session_summary_to_context": True,
            "session_summary_manager": summary_manager(helper_model, num_history_runs),
        }
        if session_summary and not is_analysis
        else {}
    )
    # `search_knowledge=True` dá ao agente uma tool de busca na collection (RAG
    # agêntico): ele decide quando consultar, em vez de injetarmos tudo no prompt.
    # O retriever faz a mesma busca e guarda quais trechos voltaram, para o trace
    # (`documents/retrieval.py`).
    knowledge = get_collection(knowledge_collection) if knowledge_collection else None
    retriever = make_retriever(knowledge_collection, knowledge) if knowledge is not None else None

    pre_hooks = []
    post_hooks = []
    add_dependencies_to_context = False

    if memory_backend == "mem0" and not is_analysis:
        from agent_service.memory.mem0_hooks import mem0_post_hook, mem0_pre_hook

        pre_hooks = [mem0_pre_hook]
        post_hooks = [mem0_post_hook]
        add_dependencies_to_context = True
        instructions = [
            *instructions,
            "Se houver memórias em `dependencies.mem0_memories`, use-as para "
            "personalizar sua resposta ao usuário.",
        ]

    return Agent(
        id=agent_id,
        name=name,
        model=get_model(
            provider=model_provider, model_id=model_id, credential_id=model_credential_id, params=model_params
        ),
        # Sem `db` em analysis: ele é one-shot e cada chamada criaria uma linha de
        # sessão `analyze-<uuid>` no Postgres, com o documento inteiro dentro, que
        # ninguém lê depois — o trace já registra tudo. O Agno guarda todo acesso a
        # sessão com `if agent.db is not None`, e a busca na collection não usa
        # `agent.db` (a collection tem a própria), então o RAG continua valendo.
        db=None if is_analysis else get_db(),
        **memory,
        **summary,
        knowledge=knowledge,
        search_knowledge=knowledge is not None,
        knowledge_retriever=retriever,
        instructions=instructions,
        tools=tools or [],
        add_history_to_context=not is_analysis,
        num_history_runs=num_history_runs,
        pre_hooks=pre_hooks or None,
        post_hooks=post_hooks or None,
        add_dependencies_to_context=add_dependencies_to_context,
        output_schema=output_schema,
        markdown=output_schema is None,
    )
