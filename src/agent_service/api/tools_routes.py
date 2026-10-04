"""CRUD de tools + catálogo de builtins + um jeito de testar uma tool sem
precisar montar um agente inteiro em volta dela.

Três tipos (`kind`), a mesma tabela (`tools/store.py`) e o mesmo contrato:

- `builtin`: uma toolkit padrão do Agno (`GET /tools/catalog` lista as
  disponíveis e os parâmetros de cada uma).
- `api`: chama uma API HTTP existente, descrita em JSON — sem código
  (`agent_service/tools/api_tool.py` documenta o formato de `config`).
- `python`: uma função Python enviada pelo usuário, rodada num namespace
  restrito — best-effort, não uma sandbox forte (ver
  `agent_service/tools/python_tool.py`); desligada por padrão
  (`CUSTOM_PYTHON_TOOLS_ENABLED`).

Depois de criada, uma tool entra na lista de um agente pelo nome
(`AgentDefinitionIn.tools`), do mesmo jeito de sempre.
"""

import copy
import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from agent_service.agents.store import list_definitions as list_agent_definitions
from agent_service.config import get_settings
from agent_service.tools import registry, store
from agent_service.tools.api_tool import ApiToolConfigError, required_dependencies, validate_api_config
from agent_service.tools.catalog import (
    BuiltinConfigError,
    get_builtin_spec,
    is_available,
    list_builtin_catalog,
    unavailable_reason,
    validate_builtin_config,
)
from agent_service.tools.invoke import ToolUnavailableError
from agent_service.tools.invoke import invoke_tool as run_tool
from agent_service.tools.python_tool import PythonToolConfigError, validate_python_config
from agent_service.tools.registry import ToolBuildError

router = APIRouter(prefix="/tools", tags=["tools"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SECRET_MASK = "••••••••"
ToolKind = Literal["builtin", "api", "python"]


# -- validação/normalização por kind, e mascaramento de segredos -----------


def _validate_config(kind: str, config: dict[str, Any], *, check_available: bool = True) -> dict[str, Any]:
    try:
        if kind == "builtin":
            return validate_builtin_config(config, check_available=check_available)
        if kind == "api":
            return validate_api_config(config)
        if kind == "python":
            return validate_python_config(config)
    except (BuiltinConfigError, ApiToolConfigError, PythonToolConfigError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=f"kind desconhecido: {kind!r}")


def _mask_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    masked = copy.deepcopy(config)
    if kind == "api":
        auth = masked.get("auth") or {}
        for field_name in ("token", "value", "password"):
            if auth.get(field_name):
                auth[field_name] = _SECRET_MASK
    elif kind == "builtin":
        from agent_service.tools.catalog import get_builtin_spec

        spec = get_builtin_spec(masked.get("builtin_id", ""))
        if spec is not None:
            params = masked.get("params") or {}
            for p in spec.params:
                if p.secret and params.get(p.name):
                    params[p.name] = _SECRET_MASK
    return masked


def _unmask_config(kind: str, incoming: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    """Se o campo secreto veio de volta como a máscara (a UI ecoou o valor
    mascarado sem editá-lo), restaura o valor real guardado."""
    merged = copy.deepcopy(incoming)
    if kind == "api":
        auth = merged.get("auth") or {}
        existing_auth = existing.get("auth") or {}
        for field_name in ("token", "value", "password"):
            if auth.get(field_name) == _SECRET_MASK:
                auth[field_name] = existing_auth.get(field_name)
    elif kind == "builtin":
        from agent_service.tools.catalog import get_builtin_spec

        spec = get_builtin_spec(merged.get("builtin_id", ""))
        if spec is not None:
            params = merged.get("params") or {}
            existing_params = existing.get("params") or {}
            for p in spec.params:
                if p.secret and params.get(p.name) == _SECRET_MASK:
                    params[p.name] = existing_params.get(p.name)
    return merged


def _row_out(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "config": _mask_config(row["kind"], row["config"])}


# -- schemas -----------------------------------------------------------


class ToolIn(BaseModel):
    tool_name: str = Field(..., description="Slug estável, usado em AgentDefinition.tools")
    kind: ToolKind
    label: str
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    side_effect: bool | None = Field(
        default=None,
        description="A tool muda algo no sistema chamado (grava, cobra, transfere, envia)? "
        "Vazio = ainda não classificada. Serve para a fila de revisão dos Logs.",
    )

    @model_validator(mode="after")
    def _validate_slug(self) -> "ToolIn":
        if not _SLUG_RE.match(self.tool_name):
            raise ValueError("tool_name deve ser um slug: letras minúsculas, números, '-' ou '_'")
        return self


class ToolUpdateIn(BaseModel):
    label: str | None = None
    description: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    side_effect: bool | None = Field(
        default=None, description="Mandar `null` explícito volta para 'não classificada'; omitir não mexe."
    )


class ToolOut(BaseModel):
    tool_name: str
    kind: ToolKind
    label: str
    description: str | None
    config: dict[str, Any]
    enabled: bool
    is_seed: bool
    side_effect: bool | None = None
    """`None` = ainda não classificada."""
    created_at: datetime
    updated_at: datetime


class ToolDetailOut(ToolOut):
    """Só na leitura de uma tool: introspecção best-effort do que ela expõe."""

    functions: list[str] | None = None
    """Nomes das funções expostas — só pra `kind="builtin"` (uma toolkit tem várias)."""
    build_error: str | None = None
    """Por que a tool não pôde ser construída agora (ex.: dependência opcional ausente)."""


class BuiltinParamOut(BaseModel):
    name: str
    type: str
    label: str
    description: str
    required: bool
    secret: bool
    default: Any
    choices: list[str] = Field(default_factory=list)
    """Valores aceitos; vazio = texto livre."""
    url: bool = False
    """Endereço que a toolkit chama — precisa ser público."""


class BuiltinCatalogEntryOut(BaseModel):
    builtin_id: str
    label: str
    description: str
    category: str
    available: bool
    """Os pacotes da toolkit estão instalados neste serviço."""
    unavailable_reason: str | None = None
    side_effect: bool
    """Tem funções que agem fora do serviço (enviar, criar, agendar) — simuladas em dry_run."""
    params: list[BuiltinParamOut]


class ToolInvokeIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    dependencies: dict[str, Any] = Field(
        default_factory=dict,
        description="Simula o `dependencies` do /chat para parâmetros com source='dependency'.",
    )
    function_name: str | None = Field(
        default=None, description="Obrigatório para kind='builtin' — qual função da toolkit chamar."
    )
    dry_run: bool = Field(
        default=False,
        description="Como o `dry_run` do /chat: a chamada leva `X-Kuro-Dry-Run: true` e "
        "`dependencies.dry_run` vale true — o destino decide o que simular.",
    )


class ToolInvokeOut(BaseModel):
    ok: bool
    """`false` também quando a API da tool respondeu erro (HTTP 4xx/5xx, rede): o
    texto que o modelo leria vem em `result`, e o tipo em `failure`."""
    result: Any = None
    error: str | None = None
    failure: str | None = None
    """invalid_arguments | not_found | auth | unavailable | config | exception (`tools/failures.py`)."""
    http_status: int | None = None


# -- config ---------------------------------------------------------------


class PythonToolsConfigOut(BaseModel):
    enabled: bool
    """`CUSTOM_PYTHON_TOOLS_ENABLED` — tools kind="python" podem ser criadas mesmo
    desligado (pra deixar prontas), mas só rodam (num agente ou em /invoke) com isto ligado."""


@router.get("/python-config", response_model=PythonToolsConfigOut)
def get_python_tools_config() -> dict[str, Any]:
    return {"enabled": get_settings().custom_python_tools_enabled}


# -- catálogo de builtins ------------------------------------------------


@router.get("/catalog", response_model=list[BuiltinCatalogEntryOut])
def get_builtin_catalog() -> list[dict[str, Any]]:
    return [
        {
            "builtin_id": spec.builtin_id,
            "label": spec.label,
            "description": spec.description,
            "category": spec.category,
            "available": is_available(spec.builtin_id),
            "unavailable_reason": None if is_available(spec.builtin_id) else unavailable_reason(spec),
            "side_effect": spec.side_effect,
            "params": [
                {
                    "name": p.name,
                    "type": p.type,
                    "label": p.label,
                    "description": p.description,
                    "required": p.required,
                    "secret": p.secret,
                    "default": p.default,
                    "choices": list(p.choices),
                    "url": p.url,
                }
                for p in spec.all_params
            ],
        }
        for spec in list_builtin_catalog()
    ]


# -- CRUD ----------------------------------------------------------------


@router.get("", response_model=list[ToolOut])
def list_tools() -> list[dict[str, Any]]:
    return [_row_out(row) for row in store.list_tools()]


@router.post("", response_model=ToolOut, status_code=201)
def create_tool(body: ToolIn) -> dict[str, Any]:
    if store.get_tool(body.tool_name) is not None:
        raise HTTPException(status_code=409, detail=f"Tool {body.tool_name!r} já existe")
    config = _validate_config(body.kind, body.config)
    side_effect = body.side_effect
    if side_effect is None and body.kind == "builtin" and (spec := get_builtin_spec(config["builtin_id"])):
        # A toolkit sabe se age fora do serviço; somente leitura não age.
        side_effect = spec.side_effect and not config["params"].get("read_only", False)
    created = store.create_tool(
        tool_name=body.tool_name,
        kind=body.kind,
        label=body.label,
        description=body.description,
        config=config,
        enabled=body.enabled,
        side_effect=side_effect,
    )
    return _row_out(created)


@router.get("/{tool_name}", response_model=ToolDetailOut)
def get_tool(tool_name: str) -> dict[str, Any]:
    row = store.get_tool(tool_name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Tool {tool_name!r} não encontrada")
    out = _row_out(row)
    if row["kind"] == "builtin" and row["enabled"]:
        try:
            built = registry.build_fresh(row)
            out["functions"] = sorted(getattr(built, "functions", {}).keys())
        except ToolBuildError as exc:
            out["build_error"] = str(exc)
    return out


@router.put("/{tool_name}", response_model=ToolOut)
def update_tool(tool_name: str, body: ToolUpdateIn) -> dict[str, Any]:
    current = store.get_tool(tool_name)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Tool {tool_name!r} não encontrada")

    config = None
    if body.config is not None:
        merged = _unmask_config(current["kind"], body.config, current["config"])
        # Editar uma tool que já existe não depende do pacote dela; trocar de toolkit, sim.
        same_toolkit = merged.get("builtin_id") == (current["config"] or {}).get("builtin_id")
        config = _validate_config(current["kind"], merged, check_available=not same_toolkit)
        if current["kind"] == "api":
            _check_agents_declare_dependencies(tool_name, config)

    updated = store.update_tool(
        tool_name,
        label=body.label,
        description=body.description,
        config=config,
        enabled=body.enabled,
        **({"side_effect": body.side_effect} if "side_effect" in body.model_fields_set else {}),
    )
    return _row_out(updated)


def _check_agents_declare_dependencies(tool_name: str, config: dict[str, Any]) -> None:
    """Uma dependência nova e obrigatória quebraria, em silêncio, todo agente que
    já usa esta tool sem declarar o campo — mesma ideia da trava do delete."""
    exigidas = set(required_dependencies(config))
    if not exigidas:
        return
    faltando = {
        d["agent_type"]: sorted(exigidas - {f["name"] for f in d["dependency_fields"] or []})
        for d in list_agent_definitions()
        if tool_name in (d["tools"] or [])
    }
    faltando = {agente: campos for agente, campos in faltando.items() if campos}
    if faltando:
        detalhe = "; ".join(f"{agente} não declara {', '.join(campos)}" for agente, campos in faltando.items())
        raise HTTPException(
            status_code=409,
            detail=f"Esta mudança exige dependencies que agentes em uso não declaram — {detalhe}",
        )


@router.delete("/{tool_name}", status_code=204)
def delete_tool(tool_name: str) -> None:
    row = store.get_tool(tool_name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Tool {tool_name!r} não encontrada")
    if row["is_seed"]:
        raise HTTPException(status_code=403, detail="Tool semeada pelo sistema não pode ser removida")

    using = [d["agent_type"] for d in list_agent_definitions() if tool_name in (d["tools"] or [])]
    if using:
        raise HTTPException(
            status_code=409,
            detail=f"Tool {tool_name!r} está em uso por: {', '.join(using)}. Remova-a desses agentes primeiro.",
        )
    store.delete_tool(tool_name)


# -- testar sem precisar de um agente ------------------------------------


@router.post("/{tool_name}/invoke", response_model=ToolInvokeOut)
async def invoke_tool(tool_name: str, body: ToolInvokeIn) -> dict[str, Any]:
    """Testa a tool isoladamente. `async` porque o entrypoint de uma tool
    `kind="api"` é uma corrotina (ver `tools/api_tool.py`) — chamá-lo de uma
    rota síncrona devolveria a corrotina sem executá-la."""
    try:
        result = await run_tool(
            tool_name,
            body.arguments,
            function_name=body.function_name,
            dependencies=body.dependencies,
            dry_run=body.dry_run,
        )
    except ToolUnavailableError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return ToolInvokeOut(**result).model_dump()
