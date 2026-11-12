"""Persistência das definições de tool.

Uma tabela só (`tool_definitions`) pros três tipos de tool — o `kind` decide
como `registry.py` constrói o objeto real do Agno a partir de `config`:

- `builtin`: uma toolkit padrão do Agno (`tools/catalog.py`), instanciada com
  os parâmetros que o usuário informou (`config = {"builtin_id", "params"}`).
- `api`: uma chamada HTTP genérica, descrita sem código
  (`tools/api_tool.py`; `config` tem método, URL, parâmetros, auth).
- `python`: uma função Python que o usuário escreveu, `exec`ada num
  namespace restrito (`tools/python_tool.py`; `config = {"code", "entrypoint"}`).

Mesmo estilo de `agents/store.py`; o schema é do Alembic (`agent_service/migrations`).

Os segredos de uma tool `api` (`auth.token/value/password` e headers de
credencial) são gravados cifrados (`tools/crypto.py`) e lidos como estão: quem
decifra é a chamada da tool, na hora de montar a requisição.
"""

from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    JSON,
    MetaData,
    String,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy import Table as SATable

from agent_service.db import get_db
from agent_service.tools.crypto import encrypt_config

metadata = MetaData()

tool_definitions = SATable(
    "tool_definitions",
    metadata,
    Column("tool_name", String, primary_key=True),
    Column("kind", String, nullable=False),  # "builtin" | "api" | "python"
    Column("label", String, nullable=False),
    Column("description", String, nullable=True),
    Column("config", JSON, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("is_seed", Boolean, nullable=False, default=False),
    # A tool muda algo no sistema chamado (grava, cobra, transfere, envia)? Fora do
    # hash da configuração do agente de propósito: classificar não cria versão nova.
    # `None` = ainda não classificada (o console avisa). Ver a fila de revisão nos Logs.
    Column("side_effect", Boolean, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    ),
)


_UNSET: Any = object()


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def list_tools() -> list[dict[str, Any]]:
    with get_db().db_engine.connect() as conn:
        rows = conn.execute(select(tool_definitions).order_by(tool_definitions.c.tool_name)).all()
        return [_row_to_dict(r) for r in rows]


def get_tool(tool_name: str) -> dict[str, Any] | None:
    with get_db().db_engine.connect() as conn:
        row = conn.execute(select(tool_definitions).where(tool_definitions.c.tool_name == tool_name)).first()
        return _row_to_dict(row) if row else None


class ToolNotFoundError(ValueError):
    pass


def create_tool(
    *,
    tool_name: str,
    kind: str,
    label: str,
    description: str | None,
    config: dict[str, Any],
    enabled: bool = True,
    is_seed: bool = False,
    side_effect: bool | None = None,
) -> dict[str, Any]:
    engine = get_db().db_engine
    with engine.begin() as conn:
        conn.execute(
            insert(tool_definitions).values(
                tool_name=tool_name,
                kind=kind,
                label=label,
                description=description,
                config=encrypt_config(config, tool_name=tool_name) if kind == "api" else config,
                enabled=enabled,
                is_seed=is_seed,
                side_effect=side_effect,
            )
        )
    tool = get_tool(tool_name)
    assert tool is not None
    return tool


def create_tool_if_missing(**kwargs: Any) -> dict[str, Any] | None:
    """Usado pelo seed: não faz nada se `tool_name` já existir."""
    if get_tool(kwargs["tool_name"]) is not None:
        return None
    return create_tool(**kwargs)


def update_tool(
    tool_name: str,
    *,
    label: str | None = None,
    description: str | None = None,
    config: dict[str, Any] | None = None,
    enabled: bool | None = None,
    side_effect: Any = _UNSET,
) -> dict[str, Any]:
    """`side_effect`: omitido = não mexe; `None` volta para "não classificada"."""
    current = get_tool(tool_name)
    if current is None:
        raise ToolNotFoundError(tool_name)

    values: dict[str, Any] = {}
    if label is not None:
        values["label"] = label
    if description is not None:
        values["description"] = description
    if config is not None:
        values["config"] = encrypt_config(config, tool_name=tool_name) if current["kind"] == "api" else config
    if enabled is not None:
        values["enabled"] = enabled
    if side_effect is not _UNSET:
        values["side_effect"] = side_effect

    if values:
        engine = get_db().db_engine
        with engine.begin() as conn:
            conn.execute(update(tool_definitions).where(tool_definitions.c.tool_name == tool_name).values(**values))

    updated = get_tool(tool_name)
    assert updated is not None
    return updated


def delete_tool(tool_name: str) -> None:
    engine = get_db().db_engine
    with engine.begin() as conn:
        conn.execute(delete(tool_definitions).where(tool_definitions.c.tool_name == tool_name))
