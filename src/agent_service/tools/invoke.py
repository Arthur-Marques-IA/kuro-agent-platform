"""Executar uma tool fora de um run de agente.

Dois chamadores: o teste de tool (`POST /tools/{nome}/invoke`) e a etapa
`action` de um agente procedural (`agents/procedure_runner.py`), em que quem
chama a tool é o servidor, não o modelo. Os dois precisam do mesmo cuidado —
dependências e `dry_run` no contexto da tool, e a falha que a tool de API
devolve como texto reconhecida como falha (`tools/failures.py`) —, então isso
mora aqui, uma vez.
"""

import inspect
from typing import Any

from agent_service.config import get_settings
from agent_service.tools import failures, registry, store
from agent_service.tools.context import dependencies_scope
from agent_service.tools.registry import ToolBuildError


class ToolUnavailableError(Exception):
    """A tool não pode ser chamada (não existe, desativada, desligada, função
    inválida). `status` é o HTTP que a rota de teste devolve."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _filter_kwargs(fn: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Só os argumentos que a função declara — uma etapa `action` manda todos os
    dados coletados, e uma função Python ou de toolkit recusaria os que não conhece."""
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return arguments
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return arguments
    return {k: v for k, v in arguments.items() if k in parameters}


async def invoke_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    function_name: str | None = None,
    dependencies: dict[str, Any] | None = None,
    dry_run: bool = False,
    lenient_arguments: bool = False,
) -> dict[str, Any]:
    """`{ok, result, error, failure, http_status}` — o corpo de `ToolInvokeOut`.

    `lenient_arguments` descarta argumentos que a função não declara (a etapa
    `action` passa todos os dados coletados); o teste de tool, sem isso, mostra
    o erro de argumento a quem está testando."""
    row = store.get_tool(tool_name)
    if row is None:
        raise ToolUnavailableError(f"Tool {tool_name!r} não encontrada", 404)
    if not row["enabled"]:
        raise ToolUnavailableError(f"Tool {tool_name!r} está desativada", 409)
    if row["kind"] == "python" and not get_settings().custom_python_tools_enabled:
        raise ToolUnavailableError("Tools Python estão desligadas (CUSTOM_PYTHON_TOOLS_ENABLED=false)", 403)

    try:
        built = registry.build_fresh(row)
    except ToolBuildError as exc:
        return {"ok": False, "result": None, "error": str(exc), "failure": None, "http_status": None}

    try:
        failures.start_run()
        with dependencies_scope(dependencies, dry_run=dry_run):
            if row["kind"] == "api":
                result = await built.entrypoint(**arguments)
            elif row["kind"] == "python":
                result = built(**(_filter_kwargs(built, arguments) if lenient_arguments else arguments))
            else:  # builtin
                functions = getattr(built, "functions", {})
                if not function_name:
                    raise ToolUnavailableError(f"Informe function_name — uma de {sorted(functions.keys())}", 422)
                fn = functions.get(function_name)
                if fn is None:
                    raise ToolUnavailableError(f"Função {function_name!r} não existe em {tool_name!r}", 404)
                args = _filter_kwargs(fn.entrypoint, arguments) if lenient_arguments else arguments
                result = fn.entrypoint(**args)
                if inspect.isawaitable(result):  # algumas toolkits do Agno são async
                    result = await result
    except ToolUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - erro de execução da tool, não de quem chamou
        return {
            "ok": False,
            "result": None,
            "error": f"{type(exc).__name__}: {exc}",
            "failure": "exception",
            "http_status": None,
        }

    # A tool de API devolve o erro como texto: sem isto, um HTTP 500 aparecia como "Sucesso".
    failed = failures.lookup_failure(tool_name, result)
    if failed is not None:
        kind, http_status = failed
        if kind in failures.NOT_COUNTED:  # 404: resposta da API, não falha — fica só a indicação
            return {"ok": True, "result": result, "error": None, "failure": kind, "http_status": http_status}
        return {"ok": False, "result": result, "error": str(result), "failure": kind, "http_status": http_status}
    return {"ok": True, "result": result, "error": None, "failure": None, "http_status": None}
