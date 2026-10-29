"""Persistência das definições de agente e do histórico de versões de prompt.

Duas tabelas próprias do projeto (fora do namespace `agno_*` que o
`PostgresDb` já cria sozinho), no mesmo Postgres:

- `agent_definitions`: o estado atual de cada agente — o que roda em
  runtime. `agents/registry.py` lê daqui pra montar o `Agent` do Agno.
- `agent_prompt_versions`: histórico append-only, só pra auditoria/leitura
  na UI. Nunca é lido em runtime — só a versão "atual" em `agent_definitions`
  importa pra construir o agente.

O schema é criado e migrado pelo Alembic (`agent_service/migrations`); as
tabelas daqui descrevem o estado atual para as consultas.
"""

import re
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    delete,
    func,
    insert,
    select,
    update,
)

from agent_service.db import get_db

metadata = MetaData()

# Sentinela pra distinguir "não passou o argumento" (não mexe) de "passou
# `None`" (limpa o campo) em `update_definition` — só necessário pra
# `model_credential_id`, o único campo aqui que faz sentido voltar a vazio
# (agente que estava fixado numa credencial volta a usar a padrão do
# provedor). Os demais campos opcionais desta função não têm essa ação.
_UNSET: Any = object()

agent_definitions = Table(
    "agent_definitions",
    metadata,
    Column("agent_type", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("instructions", JSON, nullable=False),
    Column("tools", JSON, nullable=False, default=list),
    Column("model_provider", String, nullable=True),
    Column("model_id", String, nullable=True),
    # Credencial específica (`models/store.py`) que este agente usa — None
    # cai na credencial padrão do provedor (`resolve_default_credential`).
    Column("model_credential_id", String, nullable=True),
    # Collection de documentos que este agente pode consultar (`documents/store.py`);
    # None = agente sem base de conhecimento.
    Column("knowledge_collection", String, nullable=True),
    # Campos de `dependencies` que este agente espera no /chat — ver
    # `agents/dependency_fields.py`. Lista de {name, type, label, description,
    # required, default}; [] (default) = sem validação, qualquer dependencies passa.
    Column("dependency_fields", JSON, nullable=False, default=list),
    Column("memory_backend", String, nullable=False, default="none"),
    # Resumo incremental do que sai da janela de histórico (`memory/managers.py`).
    Column("session_summary", Boolean, nullable=False, default=False),
    # Resposta em texto puro: desliga o "Use markdown" que o Agno põe no prompt (WhatsApp).
    Column("plain_text", Boolean, nullable=False, default=False),
    Column("num_history_runs", Integer, nullable=False, default=10),
    # "conversational" (padrão, com histórico/memória) ou "analysis" (one-shot,
    # devolve `response_schema` validado em vez de texto — ver `agents/response_model.py`).
    Column("kind", String, nullable=False, default="conversational"),
    # Só usado quando kind="analysis": mesma forma de `dependency_fields`
    # ({name, type, label, description, required, default}), validada pela
    # mesma `dependency_fields.validate_field_specs`.
    Column("response_schema", JSON, nullable=False, default=list),
    # Só usado quando kind="procedural": as etapas do fluxo (`agents/procedural.py`).
    Column("stages", JSON, nullable=False, default=list),
    # Temperatura, top_p, max_tokens, thinking_budget (`models/params.py`);
    # None = padrão do provedor.
    Column("model_params", JSON, nullable=True),
    # Tempo limite de uma execução; None = RUN_TIMEOUT_SECONDS do serviço.
    Column("timeout_seconds", Integer, nullable=True),
    Column("is_seed", Boolean, nullable=False, default=False),
    Column("prompt_version", Integer, nullable=False, default=1),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    ),
)

agent_prompt_versions = Table(
    "agent_prompt_versions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_type", String, nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("instructions", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

# Nota de comportamento por agente, gerada pelo agente de merge de feedback
# (`agents/feedback.py`) — concatenada nas `instructions` em runtime por
# `registry.py`, sem virar uma `prompt_version` nova a cada ajuste.
#
# `rules` é a fonte da verdade: uma lista de `{"id", "texto"}`. `content` é o
# markdown derivado dela, que vai para o prompt e continua no corpo da resposta.
# A nota nasceu como markdown solto e o merge reescrevia o texto inteiro a cada
# feedback — daí vinham regra duplicada e regra que sumia sem ninguém ver. Com
# regras identificadas, o merge vira operação sobre id e a duplicata é
# detectável fora do modelo.
agent_feedback_notes = Table(
    "agent_feedback_notes",
    metadata,
    Column("agent_type", String, primary_key=True),
    Column("content", Text, nullable=False, default=""),
    Column("rules", JSON, nullable=False, default=list),
    Column("version", Integer, nullable=False, default=1),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    ),
)

# Histórico append-only da nota, no mesmo espírito de `agent_prompt_versions`:
# um merge ruim precisa ter volta sem passar por dentro do banco.
agent_feedback_versions = Table(
    "agent_feedback_versions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_type", String, nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("rules", JSON, nullable=False),
    Column("content", Text, nullable=False),
    # merge (veio de um feedback), manual (editada à mão) ou rollback.
    Column("origin", String, nullable=False, default="merge"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def list_definitions() -> list[dict[str, Any]]:
    with get_db().db_engine.connect() as conn:
        rows = conn.execute(select(agent_definitions).order_by(agent_definitions.c.agent_type)).all()
        return [_row_to_dict(r) for r in rows]


def get_definition(agent_type: str) -> dict[str, Any] | None:
    with get_db().db_engine.connect() as conn:
        row = conn.execute(
            select(agent_definitions).where(agent_definitions.c.agent_type == agent_type)
        ).first()
        return _row_to_dict(row) if row else None


def create_definition(
    *,
    agent_type: str,
    name: str,
    instructions: list[str],
    tools: list[str] | None = None,
    model_provider: str | None = None,
    model_id: str | None = None,
    model_credential_id: str | None = None,
    knowledge_collection: str | None = None,
    dependency_fields: list[dict[str, Any]] | None = None,
    memory_backend: str = "none",
    session_summary: bool = False,
    plain_text: bool = False,
    num_history_runs: int = 10,
    kind: str = "conversational",
    response_schema: list[dict[str, Any]] | None = None,
    stages: list[dict[str, Any]] | None = None,
    model_params: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
    is_seed: bool = False,
) -> dict[str, Any]:
    engine = get_db().db_engine
    with engine.begin() as conn:
        conn.execute(
            insert(agent_definitions).values(
                agent_type=agent_type,
                name=name,
                instructions=instructions,
                tools=tools or [],
                model_provider=model_provider,
                model_id=model_id,
                model_credential_id=model_credential_id,
                knowledge_collection=knowledge_collection,
                dependency_fields=dependency_fields or [],
                memory_backend=memory_backend,
                session_summary=session_summary,
                plain_text=plain_text,
                num_history_runs=num_history_runs,
                kind=kind,
                response_schema=response_schema or [],
                stages=stages or [],
                model_params=model_params or None,
                timeout_seconds=timeout_seconds,
                is_seed=is_seed,
                prompt_version=1,
            )
        )
        conn.execute(
            insert(agent_prompt_versions).values(
                agent_type=agent_type, version=1, instructions=instructions
            )
        )
    definition = get_definition(agent_type)
    assert definition is not None
    return definition


def create_definition_if_missing(**kwargs: Any) -> dict[str, Any] | None:
    """Usado pelo seed: não faz nada se `agent_type` já existir."""
    existing = get_definition(kwargs["agent_type"])
    if existing is not None:
        return None
    return create_definition(**kwargs)


class DefinitionNotFoundError(ValueError):
    pass


def update_definition(
    agent_type: str,
    *,
    name: str | None = None,
    instructions: list[str] | None = None,
    tools: list[str] | None = None,
    model_provider: str | None = None,
    model_id: str | None = None,
    model_credential_id: str | None = _UNSET,
    knowledge_collection: str | None = _UNSET,
    dependency_fields: list[dict[str, Any]] | None = None,
    memory_backend: str | None = None,
    session_summary: bool | None = None,
    plain_text: bool | None = None,
    num_history_runs: int | None = None,
    kind: str | None = None,
    response_schema: list[dict[str, Any]] | None = None,
    stages: list[dict[str, Any]] | None = None,
    model_params: dict[str, Any] | None = _UNSET,
    timeout_seconds: int | None = _UNSET,
) -> dict[str, Any]:
    current = get_definition(agent_type)
    if current is None:
        raise DefinitionNotFoundError(agent_type)

    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if tools is not None:
        values["tools"] = tools
    if model_provider is not None:
        values["model_provider"] = model_provider
    if model_id is not None:
        values["model_id"] = model_id
    if model_credential_id is not _UNSET:
        values["model_credential_id"] = model_credential_id
    if knowledge_collection is not _UNSET:
        values["knowledge_collection"] = knowledge_collection
    if dependency_fields is not None:
        values["dependency_fields"] = dependency_fields
    if memory_backend is not None:
        values["memory_backend"] = memory_backend
    if session_summary is not None:
        values["session_summary"] = session_summary
    if plain_text is not None:
        values["plain_text"] = plain_text
    if num_history_runs is not None:
        values["num_history_runs"] = num_history_runs
    if kind is not None:
        values["kind"] = kind
    if response_schema is not None:
        values["response_schema"] = response_schema
    if stages is not None:
        values["stages"] = stages
    if model_params is not _UNSET:
        values["model_params"] = model_params or None
    if timeout_seconds is not _UNSET:
        values["timeout_seconds"] = timeout_seconds

    prompt_changed = instructions is not None and instructions != current["instructions"]
    if prompt_changed:
        values["instructions"] = instructions
        values["prompt_version"] = current["prompt_version"] + 1

    engine = get_db().db_engine
    with engine.begin() as conn:
        if values:
            conn.execute(
                update(agent_definitions)
                .where(agent_definitions.c.agent_type == agent_type)
                .values(**values)
            )
        if prompt_changed:
            conn.execute(
                insert(agent_prompt_versions).values(
                    agent_type=agent_type,
                    version=values["prompt_version"],
                    instructions=instructions,
                )
            )

    updated = get_definition(agent_type)
    assert updated is not None
    return updated


def delete_definition(agent_type: str) -> None:
    engine = get_db().db_engine
    with engine.begin() as conn:
        conn.execute(delete(agent_definitions).where(agent_definitions.c.agent_type == agent_type))
        conn.execute(delete(agent_prompt_versions).where(agent_prompt_versions.c.agent_type == agent_type))
        conn.execute(delete(agent_feedback_notes).where(agent_feedback_notes.c.agent_type == agent_type))
        conn.execute(delete(agent_feedback_versions).where(agent_feedback_versions.c.agent_type == agent_type))


def list_prompt_versions(agent_type: str) -> list[dict[str, Any]]:
    with get_db().db_engine.connect() as conn:
        rows = conn.execute(
            select(agent_prompt_versions)
            .where(agent_prompt_versions.c.agent_type == agent_type)
            .order_by(agent_prompt_versions.c.version.desc())
        ).all()
        return [_row_to_dict(r) for r in rows]


def get_feedback_note(agent_type: str) -> dict[str, Any] | None:
    with get_db().db_engine.connect() as conn:
        row = conn.execute(
            select(agent_feedback_notes).where(agent_feedback_notes.c.agent_type == agent_type)
        ).first()
    if row is None:
        return None
    note = _row_to_dict(row)
    # Nota anterior à coluna `rules`: o markdown solto vira regras na leitura,
    # senão o primeiro merge sobre ela recomeçaria do zero.
    if not note.get("rules") and note["content"]:
        note["rules"] = rules_from_markdown(note["content"])
    return note


_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def rules_from_markdown(content: str) -> list[dict[str, str]]:
    """Quebra um markdown de bullets em regras com id — a migração das notas
    que existiam antes de `rules`."""
    regras = []
    for linha in content.splitlines():
        texto = _BULLET.sub("", linha).strip()
        if texto:
            regras.append({"id": uuid.uuid4().hex[:8], "texto": texto})
    return regras


def rules_to_markdown(rules: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {r['texto']}" for r in rules)


def save_feedback_note(agent_type: str, rules: list[dict[str, Any]], *, origin: str = "merge") -> dict[str, Any]:
    """Grava as regras, renderiza o markdown e guarda a versão anterior.

    Uma versão por gravação, inclusive a primeira: é o histórico que permite
    desfazer um merge ruim sem abrir o banco."""
    engine = get_db().db_engine
    content = rules_to_markdown(rules)
    with engine.begin() as conn:
        atual = conn.execute(
            select(agent_feedback_notes.c.version).where(agent_feedback_notes.c.agent_type == agent_type)
        ).first()
        version = (atual[0] + 1) if atual else 1
        if atual:
            conn.execute(
                update(agent_feedback_notes)
                .where(agent_feedback_notes.c.agent_type == agent_type)
                .values(content=content, rules=rules, version=version)
            )
        else:
            conn.execute(
                insert(agent_feedback_notes).values(
                    agent_type=agent_type, content=content, rules=rules, version=version
                )
            )
        conn.execute(
            insert(agent_feedback_versions).values(
                agent_type=agent_type, version=version, rules=rules, content=content, origin=origin
            )
        )
    note = get_feedback_note(agent_type)
    assert note is not None
    return note


def clear_feedback_note(agent_type: str) -> None:
    """Apaga a nota e o histórico dela — `registry.py` volta a usar só as
    instructions do agente na próxima chamada."""
    with get_db().db_engine.begin() as conn:
        conn.execute(delete(agent_feedback_notes).where(agent_feedback_notes.c.agent_type == agent_type))
        conn.execute(delete(agent_feedback_versions).where(agent_feedback_versions.c.agent_type == agent_type))


def list_feedback_versions(agent_type: str) -> list[dict[str, Any]]:
    with get_db().db_engine.connect() as conn:
        rows = conn.execute(
            select(agent_feedback_versions)
            .where(agent_feedback_versions.c.agent_type == agent_type)
            .order_by(agent_feedback_versions.c.version.desc())
        ).all()
    return [_row_to_dict(r) for r in rows]


def get_feedback_version(agent_type: str, version: int) -> dict[str, Any] | None:
    with get_db().db_engine.connect() as conn:
        row = conn.execute(
            select(agent_feedback_versions).where(
                agent_feedback_versions.c.agent_type == agent_type,
                agent_feedback_versions.c.version == version,
            )
        ).first()
    return _row_to_dict(row) if row else None
