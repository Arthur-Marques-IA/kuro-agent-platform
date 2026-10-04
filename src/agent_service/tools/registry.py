"""Resolve nomes de tool (guardados em `agent_definitions.tools`) pros objetos
reais que o Agno espera em `Agent(tools=[...])` — instâncias de `Toolkit`,
`Function` ou callables simples.

As definições ficam em `tool_definitions` (`tools/store.py`), criadas pela
API/UI (`api/tools_routes.py`), nunca hardcoded aqui — isso é o que permite
compor tools sem tocar em código: uma toolkit padrão do Agno (`kind=
"builtin"`, catálogo em `tools/catalog.py`), uma chamada de API descrita em
JSON (`kind="api"`, `tools/api_tool.py`) ou uma função Python enviada pelo
usuário (`kind="python"`, `tools/python_tool.py`).

`resolve_tools` cacheia o objeto construído por tool, invalidado comparando
`updated_at` — mesmo padrão de `agents/registry.py` pro `Agent` em si.
"""

from datetime import datetime
from typing import Any

from agent_service.config import get_settings
from agent_service.tools import store
from agent_service.tools.api_tool import ApiToolConfigError, build_api_function
from agent_service.tools.catalog import build_builtin, get_builtin_spec
from agent_service.tools.python_tool import PythonToolConfigError, PythonToolDisabledError, compile_python_tool

_cache: dict[str, tuple[Any, datetime]] = {}


class UnknownToolError(ValueError):
    pass


class ToolBuildError(RuntimeError):
    """A tool existe, mas não pôde ser construída (config inválida, dependência
    ausente, kind Python desligado...). Erro do operador, não de quem chama —
    result num 502 nas rotas de agente/chat, não num 422."""


def _build(row: dict[str, Any]) -> Any:
    kind = row["kind"]
    config = row["config"] or {}
    try:
        if kind == "builtin":
            spec = get_builtin_spec(config.get("builtin_id", ""))
            if spec is None:
                raise ToolBuildError(f"Tool {row['tool_name']!r}: builtin_id {config.get('builtin_id')!r} não existe mais no catálogo.")
            return build_builtin(spec, config.get("params", {}), row["tool_name"])
        if kind == "api":
            return build_api_function(tool_name=row["tool_name"], description=row.get("description"), config=config)
        if kind == "python":
            return compile_python_tool(
                tool_name=row["tool_name"], config=config, enabled=get_settings().custom_python_tools_enabled
            )
    except (ApiToolConfigError, PythonToolConfigError) as exc:
        raise ToolBuildError(f"Tool {row['tool_name']!r}: {exc}") from exc
    except PythonToolDisabledError as exc:
        raise ToolBuildError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - ex.: dependência opcional (ddgs) ausente
        raise ToolBuildError(f"Tool {row['tool_name']!r} ({kind}): {exc}") from exc
    raise ToolBuildError(f"Tool {row['tool_name']!r}: kind desconhecido {kind!r}")


def _resolve_one(name: str) -> tuple[Any, datetime]:
    row = store.get_tool(name)
    if row is None:
        raise UnknownToolError(f"Tool desconhecida: {name!r}")
    if not row["enabled"]:
        raise ToolBuildError(f"Tool {name!r} está desativada.")

    cached = _cache.get(name)
    if cached is not None and cached[1] == row["updated_at"]:
        return cached[0], row["updated_at"]

    built = _build(row)
    _cache[name] = (built, row["updated_at"])
    return built, row["updated_at"]


def resolve_tools(names: list[str]) -> list[Any]:
    return [built for built, _ in (_resolve_one(name) for name in names)]


def resolve_tools_with_stamp(names: list[str]) -> tuple[list[Any], tuple[datetime, ...]]:
    """Como `resolve_tools`, mas devolve também o `updated_at` de cada tool: é o
    que permite ao cache de agentes perceber que uma tool mudou (o schema que o
    modelo vê vive dentro do `Function`, que ficou preso no `Agent` construído)."""
    resolved = [_resolve_one(name) for name in names]
    return [built for built, _ in resolved], tuple(stamp for _, stamp in resolved)


def tool_exists(name: str) -> bool:
    return store.get_tool(name) is not None


def build_fresh(row: dict[str, Any]) -> Any:
    """Constrói sem usar nem alimentar o cache — para a rota de teste
    (`POST /tools/{name}/invoke`), que deve refletir a config mais recente
    mesmo antes dela ser salva num agente."""
    return _build(row)
