"""Resolve agentes dinamicamente a partir de `agents/store.py`.

Agentes não são mais um dict hardcoded — qualquer linha em
`agent_definitions` (criada pela API `/agents` ou pela UI) vira um agente
utilizável em `/chat`/`/chat/stream` sem restart. O cache abaixo evita
reconstruir o `Agent` do Agno a cada request; a cada chamada uma consulta só
(`_snapshot`) traz a definição e os carimbos da nota de feedback e das tools,
então uma edição feita pela UI passa a valer na próxima requisição — em
qualquer processo, já que a comparação é com o banco.
"""

import copy
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agno.agent import Agent
from sqlalchemy import select

from agent_service.agents.base import build_agent
from agent_service.agents.procedural import PROCEDURAL_INSTRUCTIONS
from agent_service.agents.response_model import build_response_model
from agent_service.agents.store import agent_definitions, agent_feedback_notes, get_feedback_note, list_definitions
from agent_service.agents.versions import effective_config, ensure_version
from agent_service.db import get_db
from agent_service.documents.embedder import EmbedderError
from agent_service.models.provider import ProviderNotConfiguredError
from agent_service.tools.registry import resolve_tools_with_stamp
from agent_service.tools.store import tool_definitions

# (updated_at, version) da nota de feedback; None = sem nota (ou agente analista).
# A `version` entra porque o `updated_at` do SQLite tem resolução de segundo.
_NoteStamp = tuple[datetime, int] | None

_NOTE_UPDATED_AT = "_kuro_note_updated_at"
_NOTE_VERSION = "_kuro_note_version"
_TOOL_PREFIX = "_kuro_tool_"


@dataclass(frozen=True)
class _CacheEntry:
    agent: Agent
    definition: dict[str, Any]
    note_stamp: _NoteStamp
    tool_names: tuple[str, ...]
    tools_stamp: tuple[datetime | None, ...]
    version: tuple[int, str]  # (agent_version, config_hash)


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


def _snapshot(
    agent_type: str, tool_names: tuple[str, ...]
) -> tuple[dict[str, Any] | None, _NoteStamp, tuple[datetime | None, ...]]:
    """Uma consulta só: a linha da definição, o carimbo da nota de feedback e o
    `updated_at` de cada tool em `tool_names` (as do agente em cache).

    É o que roda em toda requisição quente. As tools vão como subconsulta escalar,
    uma coluna por nome, porque a lista vive num JSON da definição e expandi-la
    no SQL seria diferente no Postgres e no SQLite. Tool apagada volta `None`."""
    note = agent_feedback_notes
    columns: list[Any] = [
        agent_definitions,
        note.c.updated_at.label(_NOTE_UPDATED_AT),
        note.c.version.label(_NOTE_VERSION),
    ]
    for i, name in enumerate(tool_names):
        columns.append(
            select(tool_definitions.c.updated_at)
            .where(tool_definitions.c.tool_name == name)
            .scalar_subquery()
            .label(f"{_TOOL_PREFIX}{i}")
        )
    stmt = (
        select(*columns)
        .select_from(agent_definitions.outerjoin(note, note.c.agent_type == agent_definitions.c.agent_type))
        .where(agent_definitions.c.agent_type == agent_type)
    )
    with get_db().db_engine.connect() as conn:
        row = conn.execute(stmt).first()
    if row is None:
        return None, None, ()
    data = dict(row._mapping)
    tools_stamp = tuple(data.pop(f"{_TOOL_PREFIX}{i}") for i in range(len(tool_names)))
    note_updated_at, note_version = data.pop(_NOTE_UPDATED_AT), data.pop(_NOTE_VERSION)
    note_stamp = None if note_updated_at is None else (note_updated_at, note_version)
    return data, note_stamp, tools_stamp


def _note_stamp_for(definition: dict[str, Any], note_stamp: _NoteStamp) -> _NoteStamp:
    # Analista não usa a nota (`_build_from_definition`): mexer nela não remonta.
    return None if (definition.get("kind") or "conversational") == "analysis" else note_stamp


def get_agent_with_definition(agent_type: str) -> tuple[Agent, dict[str, Any]]:
    """Como `get_agent`, mas devolve também a definição usada — a observabilidade
    registra em cada trace o nome, a `prompt_version` e a versão da configuração
    inteira (`agent_version`/`config_hash`, ver `versions.py`) que responderam.

    Uma requisição quente faz uma consulta só (`_snapshot`). O cache é por
    processo; quem garante que outro worker ou réplica veja uma edição é essa
    consulta ao banco, nunca uma invalidação em memória."""
    cached = _cache.get(agent_type)
    definition, note_stamp, tools_stamp = _snapshot(agent_type, cached.tool_names if cached else ())
    if definition is None:
        _cache.pop(agent_type, None)
        raise UnknownAgentTypeError(
            f"Tipo de agente desconhecido: {agent_type!r}. Veja GET /agents para os disponíveis."
        )
    note_stamp = _note_stamp_for(definition, note_stamp)

    # A definição é comparada inteira, não só pelo `updated_at`: o carimbo do
    # SQLite tem resolução de segundo. Uma tool pode mudar sem a definição mudar —
    # o `Function` com o schema antigo está dentro do `Agent` já construído.
    if (
        cached is not None
        and cached.definition == definition
        and cached.note_stamp == note_stamp
        and cached.tools_stamp == tools_stamp
    ):
        return cached.agent, _with_version(cached.definition, cached.version)

    agent, built_tools_stamp = _build_from_definition(definition)
    # Remontar é o sinal de que algo mudou: é aqui que uma configuração nova vira versão.
    version = ensure_version(agent_type, effective_config(definition))
    # Definição e nota guardadas são as lidas antes de montar, e o carimbo das tools
    # é o das linhas usadas na montagem: se algo mudar no meio, a próxima requisição
    # remonta de novo (nunca o contrário). A cópia funda isola a definição em cache
    # das listas que foram parar dentro do `Agent` (as `instructions`, por exemplo).
    _cache[agent_type] = _CacheEntry(
        agent=agent,
        definition=copy.deepcopy(definition),
        note_stamp=note_stamp,
        tool_names=tuple(definition["tools"] or []),
        tools_stamp=built_tools_stamp,
        version=version,
    )
    return agent, _with_version(definition, version)


def _with_version(definition: dict[str, Any], version: tuple[int, str]) -> dict[str, Any]:
    # Cópia funda: a definição em cache não pode ser alterada por quem a recebe.
    return {**copy.deepcopy(definition), "agent_version": version[0], "config_hash": version[1]}


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
