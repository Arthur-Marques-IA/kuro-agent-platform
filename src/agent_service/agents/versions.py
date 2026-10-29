"""Versão da configuração inteira do agente ("revisão").

`prompt_version` só muda quando as `instructions` mudam. Trocar o modelo, uma
tool, o `response_schema` ou a nota de feedback também muda o comportamento, e
sem registrar isso uma regressão causada por essas trocas fica invisível no
trace. Aqui, tudo o que muda o comportamento vira um snapshot imutável em
`agent_versions`, identificado por um hash:

- a versão é criada quando o agente é (re)montado com uma configuração nova —
  `registry.py` já remonta o agente a cada mudança de definição, tool ou nota,
  então nenhum caminho de escrita precisa lembrar de chamar isto;
- a mesma configuração tem sempre o mesmo número: voltar para uma configuração
  anterior (rollback, promote) reaproveita a versão dela em vez de criar outra;
- cada run grava `agent_version` e `config_hash` — é o que responde "com que
  configuração esta decisão foi tomada?".

As tools entram pelo hash da config delas, não pela config em si: uma tool de
API pode ter um header de autenticação, e o snapshot é exposto pela API.
"""

import hashlib
import json
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, func, insert, select
from sqlalchemy.exc import IntegrityError

from agent_service.agents.store import get_feedback_note
from agent_service.config import get_settings
from agent_service.db import get_db
from agent_service.tools.store import get_tool

metadata = MetaData()

agent_versions = Table(
    "agent_versions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_type", String, nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("config_hash", String, nullable=False),
    Column("config", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

BEHAVIOR_FIELDS = (
    "instructions",
    "model_credential_id",
    "knowledge_collection",
    "dependency_fields",
    "memory_backend",
    "num_history_runs",
    "kind",
    "response_schema",
)
"""Campos da definição que mudam o comportamento. `name` fica de fora: é rótulo."""


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def effective_config(definition: dict[str, Any]) -> dict[str, Any]:
    """O que de fato roda: a definição, o modelo resolvido (o padrão do serviço
    quando o agente não fixa um), a config de cada tool e a nota de feedback."""
    settings = get_settings()
    config: dict[str, Any] = {field: definition.get(field) for field in BEHAVIOR_FIELDS}
    config["kind"] = config["kind"] or "conversational"
    config["model_provider"] = (definition.get("model_provider") or settings.default_model_provider).lower()
    config["model_id"] = definition.get("model_id") or settings.default_model_id
    # Só entram quando definidos: com a chave sempre presente, todo agente que já
    # existia ganharia uma versão nova sem ter mudado nada.
    if definition.get("model_params"):
        config["model_params"] = definition["model_params"]
    if definition.get("timeout_seconds"):
        config["timeout_seconds"] = definition["timeout_seconds"]
    if definition.get("session_summary"):
        config["session_summary"] = True
    if definition.get("plain_text"):
        config["plain_text"] = True
    if config["kind"] == "procedural":
        # Só aqui: com `stages` em todo agente, os que já existiam ganhariam versão nova.
        config["stages"] = definition.get("stages") or []
    if config.get("memory_backend") == "common":
        # `common` é o nome antigo de `agentic`: o mesmo comportamento, a mesma versão.
        config["memory_backend"] = "agentic"

    tools = []
    for name in definition.get("tools") or []:
        row = get_tool(name)
        if row is None:
            tools.append({"tool_name": name, "missing": True})
            continue
        tools.append(
            {
                "tool_name": name,
                "kind": row["kind"],
                "enabled": row["enabled"],
                "config_hash": _digest({"config": row["config"], "description": row.get("description")})[:12],
            }
        )
    config["tools"] = tools

    # A nota de feedback orienta quem redige a resposta: conversacional e procedural.
    note = get_feedback_note(definition["agent_type"]) if config["kind"] != "analysis" else None
    config["feedback_rules"] = [r["texto"] for r in note["rules"]] if note and note.get("rules") else []
    return config


def config_hash(config: dict[str, Any]) -> str:
    return _digest(config)[:12]


def ensure_version(agent_type: str, config: dict[str, Any]) -> tuple[int, str]:
    """Versão desta configuração — a existente, se já rodou antes, ou uma nova."""
    digest = config_hash(config)
    engine = get_db().db_engine
    for _ in range(3):
        try:
            with engine.begin() as conn:
                existing = conn.execute(
                    select(agent_versions.c.version).where(
                        agent_versions.c.agent_type == agent_type, agent_versions.c.config_hash == digest
                    )
                ).scalar()
                if existing is not None:
                    return existing, digest
                version = (
                    conn.execute(
                        select(func.max(agent_versions.c.version)).where(agent_versions.c.agent_type == agent_type)
                    ).scalar()
                    or 0
                ) + 1
                conn.execute(
                    insert(agent_versions).values(agent_type=agent_type, version=version, config_hash=digest, config=config)
                )
                return version, digest
        except IntegrityError:
            # Outro worker gravou ao mesmo tempo: a próxima volta lê o que ele gravou.
            continue
    raise RuntimeError(f"Não foi possível registrar a versão da configuração de {agent_type!r}")


def list_versions(agent_type: str) -> list[dict[str, Any]]:
    with get_db().db_engine.connect() as conn:
        rows = conn.execute(
            select(agent_versions)
            .where(agent_versions.c.agent_type == agent_type)
            .order_by(agent_versions.c.version.desc())
        ).all()
    return [dict(r._mapping) for r in rows]


def get_version(agent_type: str, version: int) -> dict[str, Any] | None:
    with get_db().db_engine.connect() as conn:
        row = conn.execute(
            select(agent_versions).where(agent_versions.c.agent_type == agent_type, agent_versions.c.version == version)
        ).first()
    return dict(row._mapping) if row else None
