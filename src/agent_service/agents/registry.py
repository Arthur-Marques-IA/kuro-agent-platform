"""Resolve agentes dinamicamente a partir de `agents/store.py`.

Agentes não são mais um dict hardcoded — qualquer linha em
`agent_definitions` (criada pela API `/agents` ou pela UI) vira um agente
utilizável em `/chat`/`/chat/stream` sem restart. O cache abaixo evita
reconstruir o `Agent` do Agno a cada request; é invalidado comparando
`updated_at`, então uma edição feita pela UI passa a valer na próxima
requisição.
"""

import logging
from datetime import datetime
from typing import Any

from agno.agent import Agent

from agent_service.agents.base import build_agent
from agent_service.agents.procedural import PROCEDURAL_INSTRUCTIONS
from agent_service.agents.response_model import build_response_model
from agent_service.agents.store import get_definition, get_feedback_note, list_definitions
from agent_service.agents.versions import effective_config, ensure_version
from agent_service.documents.collections import EmbedderError
from agent_service.models.provider import ProviderNotConfiguredError
from agent_service.tools.store import get_tool as get_tool_row
from agent_service.tools.registry import resolve_tools_with_stamp

# (agent, definition.updated_at, tools_stamp, feedback_note.updated_at, (agent_version, config_hash))
_CacheEntry = tuple[Agent, datetime, tuple[datetime, ...], datetime | None, tuple[int, str]]
_cache: dict[str, _CacheEntry] = {}

_FEEDBACK_HEADER = "Ajustes aprendidos com feedback de conversas anteriores:\n"


logger = logging.getLogger(__name__)


class UnknownAgentTypeError(ValueError):
    pass


def _build_from_definition(definition: dict[str, Any]) -> tuple[Agent, tuple[datetime, ...]]:
    tools, tools_stamp = resolve_tools_with_stamp(definition["tools"] or [])
    kind = definition.get("kind") or "conversational"
    instructions = definition["instructions"]
    output_schema = None
    if kind == "analysis":
        output_schema = build_response_model(definition["agent_type"], definition.get("response_schema") or [])
    else:
        if kind == "procedural":
            # Fixo, então pode ir no agente em cache: a etapa de cada conversa entra
            # por execução, em `dependencies.procedimento` (`agents/procedure_runner.py`).
            instructions = [*instructions, PROCEDURAL_INSTRUCTIONS]
        note = get_feedback_note(definition["agent_type"])
        if note and note["content"]:
            instructions = [*instructions, _FEEDBACK_HEADER + note["content"]]
    return build_agent(
        agent_id=definition["agent_type"],
        name=definition["name"],
        instructions=instructions,
        tools=tools,
        model_provider=definition["model_provider"],
        model_id=definition["model_id"],
        model_credential_id=definition.get("model_credential_id"),
        model_params=definition.get("model_params"),
        knowledge_collection=definition.get("knowledge_collection"),
        num_history_runs=definition["num_history_runs"],
        memory_backend=definition["memory_backend"],
        session_summary=bool(definition.get("session_summary")),
        plain_text=bool(definition.get("plain_text")),
        kind=kind,
        output_schema=output_schema,
    ), tools_stamp


def _feedback_stamp(definition: dict[str, Any]) -> datetime | None:
    if (definition.get("kind") or "conversational") == "analysis":
        return None
    note = get_feedback_note(definition["agent_type"])
    return note["updated_at"] if note else None


def get_agent_with_definition(agent_type: str) -> tuple[Agent, dict[str, Any]]:
    """Como `get_agent`, mas devolve também a definição usada — a observabilidade
    registra em cada trace o nome, a `prompt_version` e a versão da configuração
    inteira (`agent_version`/`config_hash`, ver `versions.py`) que responderam."""
    definition = get_definition(agent_type)
    if definition is None:
        raise UnknownAgentTypeError(
            f"Tipo de agente desconhecido: {agent_type!r}. Veja GET /agents para os disponíveis."
        )

    cached = _cache.get(agent_type)
    if cached is not None and cached[1] == definition["updated_at"] and cached[3] == _feedback_stamp(definition):
        # A definição não mudou, mas uma tool dela pode ter mudado: o `Function`
        # com o schema antigo está dentro do `Agent` já construído.
        if cached[2] == _tools_stamp(definition):
            return cached[0], _with_version(definition, cached[4])

    agent, tools_stamp = _build_from_definition(definition)
    # Remontar é o sinal de que algo mudou: é aqui que uma configuração nova vira versão.
    version = ensure_version(agent_type, effective_config(definition))
    _cache[agent_type] = (agent, definition["updated_at"], tools_stamp, _feedback_stamp(definition), version)
    return agent, _with_version(definition, version)


def _with_version(definition: dict[str, Any], version: tuple[int, str]) -> dict[str, Any]:
    return {**definition, "agent_version": version[0], "config_hash": version[1]}


def _tools_stamp(definition: dict[str, Any]) -> tuple[datetime, ...]:
    rows = (get_tool_row(name) for name in definition["tools"] or [])
    return tuple(row["updated_at"] for row in rows if row is not None)


def get_agent(agent_type: str) -> Agent:
    return get_agent_with_definition(agent_type)[0]


def list_agent_types() -> list[str]:
    return [d["agent_type"] for d in list_definitions()]


def all_agents() -> list[Agent]:
    """Snapshot no momento da chamada — usado só pra registrar as rotas
    nativas do AgentOS/playground no startup. Agentes criados depois do
    boot funcionam normalmente via `get_agent` (usado por `/chat`), só não
    aparecem no playground do AgentOS até o próximo restart.
    """
    montados = []
    for d in list_definitions():
        try:
            montados.append(_build_from_definition(d)[0])
        except EmbedderError:
            # Um agente apontando para uma collection cujo embedder perdeu a
            # credencial não pode impedir o serviço de subir: os outros sobem, e
            # quem chamar este recebe 502 com o motivo (`api/routes.py`).
            logger.warning(
                "Agente %r ficou fora do AgentOS: embedder da collection não configurado",
                d["agent_type"],
                exc_info=True,
            )
        except ProviderNotConfiguredError as exc:
            # Mesma lógica: sem a chave do provedor (ou sem como decifrá-la), este
            # agente fica fora do playground, e o /chat dele responde 503 com o motivo.
            logger.warning("Agente %r ficou fora do AgentOS: %s", d["agent_type"], exc)
    return montados
