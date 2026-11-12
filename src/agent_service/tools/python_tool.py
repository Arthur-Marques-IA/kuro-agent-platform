"""Tools `kind="python"`: uma função Python escrita pelo usuário (API/UI),
`exec`ada num namespace restrito. Existe pra casos que uma chamada de API
simples (`kind="api"`) não cobre — lógica própria, combinar mais de uma
chamada, transformar dados.

**Isto NÃO é uma sandbox.** É redução de superfície, em camadas:

- checagem estática da árvore (`ast`): imports só de uma lista de módulos, e
  `from x import y` só dos nomes públicos permitidos de cada um; nenhum acesso
  a atributo que comece com `_` (o que inclui os dunders `__globals__`,
  `__subclasses__`... e os privados como `random._os` ou `collections._sys`);
  nenhum nome perigoso (`os`, `subprocess`, `eval`...);
- na execução, `import x` não entrega o módulo real: entrega uma cópia nova,
  por import, só com os nomes públicos permitidos (`_module_proxy`). Então
  `random._os` não existe nem em runtime, e uma tool que reatribui
  `random.choice` não mexe no `random` do processo nem no das outras tools;
- `builtins` restritos.

Isso pega erro e abuso acidental e fecha os desvios conhecidos, mas não é
feito para conter um autor mal-intencionado determinado (isolamento de
verdade precisaria de processo/container separado, fora do escopo deste
serviço). Por isso:

- desligado por padrão (`CUSTOM_PYTHON_TOOLS_ENABLED=false`);
- só ligue se quem tem a chave de escopo `admin` já for confiável: é ela que
  permite criar uma tool Python. Sem `ADMIN_API_KEY` configurada, o serviço
  fica aberto e isso vira qualquer um que alcance a porta (ver `api/auth.py`);
- código pode importar `httpx`: tools Python alcançam a rede de propósito,
  mas pelo `httpx` guardado deste módulo, que passa todo destino pelo mesmo
  controle de `tools/egress.py` usado pelas tools `kind="api"`. Sem isso,
  uma tool Python seria o desvio óbvio daquela trava.

Limites que a barreira **não** cobre, e por que ficam documentados em vez de
remendados (isolamento de verdade é um projeto à parte — ver "Fora de propósito" no ROADMAP):

- **O timeout não cancela nada.** Python não interrompe uma thread de fora.
  Passado o limite, quem chamou recebe `TimeoutError`, mas um `while True:`
  segue rodando até o processo reiniciar. Por isso cada chamada roda na sua
  própria thread daemon e há um teto de execuções simultâneas: uma tool
  travada não impede as outras de rodarem, que era o que acontecia quando
  todas dividiam um pool de 8 workers.
- **Sem limite de memória ou de CPU.** `[0] * 10**10` derruba o container.
- **`str.format` alcança atributos** (`"{0.__class__}".format(x)`), o que a
  checagem estática não vê porque o caminho está dentro de uma string. Isso
  vaza informação sobre os objetos, mas só produz texto — não chama nada.
  (`string.Formatter` fica de fora da lista justamente porque o
  `get_field` dele devolve o objeto, não o texto.)
"""

import ast
import contextvars
import functools
import inspect
import threading
from typing import Any, Callable

from agent_service.tools.egress import guard_request

_ALLOWED_MODULES = {
    "base64",
    "collections",
    "datetime",
    "decimal",
    "functools",
    "hashlib",
    "httpx",
    "itertools",
    "json",
    "math",
    "random",
    "re",
    "statistics",
    "string",
    "textwrap",
    "time",
    "uuid",
}

# Submódulos que podem ser importados pelo nome pontuado (`import collections.abc`,
# `from collections.abc import Mapping`). Qualquer outro `import a.b` é recusado.
_ALLOWED_SUBMODULES = {"collections.abc"}

# Nomes públicos que mesmo assim ficam de fora da cópia do módulo.
_WITHHELD_NAMES: dict[str, set[str]] = {
    # `Formatter().get_field("0.__class__...", ...)` percorre atributos (dunders
    # inclusive) e devolve o OBJETO — o desvio da checagem de atributos.
    "string": {"Formatter"},
    # Leem `sys.argv`/arquivos: não servem para nada dentro de uma tool.
    "uuid": {"main"},
    "base64": {"main"},
}

# O que o `httpx` guardado expõe (ver `_guarded_httpx`).
_HTTPX_NAMES = frozenset(
    {
        "request", "get", "post", "put", "patch", "delete", "head",
        "Client", "AsyncClient",
        "Response", "HTTPError", "RequestError", "TimeoutException", "HTTPStatusError",
    }
)  # fmt: skip

_BANNED_NAMES = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "open",
    "input",
    "exit",
    "quit",
    "breakpoint",
    "help",
    "os",
    "sys",
    "subprocess",
    "socket",
    "shutil",
    "pathlib",
    "importlib",
    "ctypes",
    "pickle",
    "marshal",
    "signal",
    "multiprocessing",
    "threading",
}

_ALLOWED_TOP_LEVEL = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef)
_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_TIMEOUT_SECONDS = 30.0
_MAX_CODE_CHARS = 20_000

_MAX_CONCURRENT_RUNS = 32
_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_RUNS)


class PythonToolConfigError(ValueError):
    """Código ou config inválidos — reportado como 422 na criação/edição da tool."""


class PythonToolDisabledError(RuntimeError):
    """`CUSTOM_PYTHON_TOOLS_ENABLED=false` — ver o docstring deste módulo."""


def validate_python_config(config: dict[str, Any]) -> dict[str, Any]:
    code = config.get("code")
    entrypoint_name = config.get("entrypoint")
    if not isinstance(code, str) or not code.strip():
        raise PythonToolConfigError("code é obrigatório")
    if len(code) > _MAX_CODE_CHARS:
        raise PythonToolConfigError(f"code excede o limite de {_MAX_CODE_CHARS} caracteres")
    if not isinstance(entrypoint_name, str) or not entrypoint_name.isidentifier():
        raise PythonToolConfigError("entrypoint deve ser o nome de uma função definida em code")
    timeout = config.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or not (0 < timeout <= _MAX_TIMEOUT_SECONDS):
        raise PythonToolConfigError(f"timeout_seconds deve ser > 0 e <= {_MAX_TIMEOUT_SECONDS}")

    _check_ast(code, entrypoint_name)
    return {"code": code, "entrypoint": entrypoint_name, "timeout_seconds": float(timeout)}


def _root_module(dotted: str) -> str:
    return dotted.split(".", 1)[0]


def _check_ast(code: str, entrypoint_name: str) -> None:
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise PythonToolConfigError(f"código inválido: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module_name(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                raise PythonToolConfigError("import relativo não é permitido")
            _check_module_name(node.module)
            allowed = _public_names(node.module)
            for alias in node.names:
                if alias.name not in allowed:
                    raise PythonToolConfigError(
                        f"nome não permitido em 'from {node.module} import': {alias.name!r} "
                        f"(liberados: {sorted(allowed)})"
                    )
        elif isinstance(node, ast.ClassDef):
            raise PythonToolConfigError("definição de classe não é permitida — escreva funções")
        elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            raise PythonToolConfigError(f"identificador não permitido: {node.id!r}")
        elif isinstance(node, ast.Attribute):
            # Qualquer `_` no início: dunders (`__globals__`) e privados de
            # módulo (`random._os`, `collections._sys`) são o caminho de fuga.
            if node.attr in _BANNED_NAMES or node.attr.startswith("_"):
                raise PythonToolConfigError(f"acesso a atributo não permitido: {node.attr!r}")

    top_level_defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in tree.body:
        is_docstring = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        is_simple_constant = isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_literal(getattr(node, "value", None))
        if not (isinstance(node, _ALLOWED_TOP_LEVEL) or is_docstring or is_simple_constant):
            raise PythonToolConfigError(
                f"só são permitidos no nível do módulo: import, def, docstring e constantes literais "
                f"(encontrado: {type(node).__name__})"
            )

    if not any(n.name == entrypoint_name for n in top_level_defs):
        raise PythonToolConfigError(f"nenhuma função chamada {entrypoint_name!r} foi definida em code")


def _check_module_name(name: str) -> None:
    if name in _ALLOWED_MODULES or name in _ALLOWED_SUBMODULES:
        return
    raise PythonToolConfigError(
        f"import não permitido: {name!r} (módulos liberados: {sorted(_ALLOWED_MODULES | _ALLOWED_SUBMODULES)})"
    )


@functools.lru_cache(maxsize=None)
def _public_names(module_name: str) -> frozenset[str]:
    """Os nomes que a tool enxerga em `module_name`: o `__all__` do módulo (ou,
    sem ele, o que não começa com `_`), menos submódulos — `uuid.os`,
    `statistics.sys` —, nomes perigosos e os de `_WITHHELD_NAMES`. Os
    submódulos de `_ALLOWED_SUBMODULES` entram como atributo do pai."""
    if module_name == "httpx":
        return _HTTPX_NAMES
    import importlib
    import types

    module = importlib.import_module(module_name)
    declared = getattr(module, "__all__", None)
    candidates = declared if declared is not None else dir(module)
    withheld = _WITHHELD_NAMES.get(module_name, set())
    names = {
        name
        for name in candidates
        if not name.startswith("_")
        and name not in _BANNED_NAMES
        and name not in withheld
        and hasattr(module, name)
        and not isinstance(getattr(module, name), types.ModuleType)
    }
    names |= {sub.rsplit(".", 1)[1] for sub in _ALLOWED_SUBMODULES if sub.rsplit(".", 1)[0] == module_name}
    return frozenset(names)


def _module_proxy(module_name: str) -> Any:
    """Cópia nova de `module_name` com só os nomes de `_public_names` — o que a
    tool recebe no lugar do módulo real. Nova a cada import, para uma tool não
    conseguir trocar uma função que outra tool (ou o serviço) usa."""
    import importlib
    import types

    if module_name == "httpx":
        source: Any = _guarded_httpx()
    else:
        source = importlib.import_module(module_name)
    proxy = types.ModuleType(module_name)
    for name in _public_names(module_name):
        dotted = f"{module_name}.{name}"
        if dotted in _ALLOWED_SUBMODULES:
            setattr(proxy, name, _module_proxy(dotted))
        else:
            setattr(proxy, name, getattr(source, name))
    return proxy


def _is_literal(node: ast.AST | None) -> bool:
    if node is None:
        return False
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError):
        return False


_guarded_httpx_client: Any = None


def _guarded_httpx() -> Any:
    """O `httpx` que a tool recebe: as funções de módulo, passando todo destino
    por `tools/egress.py`.

    `Client`/`AsyncClient` ficam de fora de propósito. Dar o cliente real
    devolveria um objeto cujo `event_hooks` a própria tool poderia limpar numa
    linha, e aí a trava seria enfeite. Com só as funções de módulo, o caminho
    para a rede é um que este módulo controla inteiro.
    """
    import types

    import httpx

    def _blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(
            "httpx.Client/AsyncClient não estão disponíveis em tools Python — use httpx.get, "
            "httpx.post, httpx.request etc., que passam pelo controle de destino do serviço."
        )

    def _proxy(method_name: str) -> Callable[..., Any]:
        # Resolve o cliente na hora da chamada, não aqui: o shim é montado quando
        # a tool executa `import httpx`, e prender o cliente agora deixaria a tool
        # presa a uma instância que pode ter sido fechada ou trocada depois.
        def call(*args: Any, **kwargs: Any) -> Any:
            return getattr(_http_client(), method_name)(*args, **kwargs)

        call.__name__ = method_name
        return call

    return types.SimpleNamespace(
        **{name: _proxy(name) for name in ("request", "get", "post", "put", "patch", "delete", "head")},
        Client=_blocked,
        AsyncClient=_blocked,
        # O autor da tool precisa conseguir tratar as falhas que pode causar.
        Response=httpx.Response,
        HTTPError=httpx.HTTPError,
        RequestError=httpx.RequestError,
        TimeoutException=httpx.TimeoutException,
        HTTPStatusError=httpx.HTTPStatusError,
    )


def _http_client() -> Any:
    """Cliente único das tools Python, com o controle de destino no event hook."""
    import httpx

    global _guarded_httpx_client
    if _guarded_httpx_client is None:
        # O hook roda a cada envio, inclusive em cada salto de redirect.
        _guarded_httpx_client = httpx.Client(
            follow_redirects=False, event_hooks={"request": [guard_request, _mark_dry_run]}, timeout=10.0
        )
    return _guarded_httpx_client


def _mark_dry_run(request: Any) -> None:
    """Mesmo aviso das tools `api`: num run com `dry_run`, o destino recebe
    `X-Kuro-Dry-Run: true` e decide o que simular."""
    from agent_service.tools.context import DRY_RUN_HEADER, is_dry_run

    if is_dry_run():
        request.headers[DRY_RUN_HEADER] = "true"


def _safe_import(
    name: str, _globals: Any = None, _locals: Any = None, fromlist: Any = (), level: int = 0
) -> Any:
    """O `__import__` da tool: nunca o módulo real, sempre `_module_proxy`.

    Segue a semântica do `__import__`: `import a.b` devolve `a` (com `b` como
    atributo) e `from a.b import x` devolve `a.b`."""
    if level or (name not in _ALLOWED_MODULES and name not in _ALLOWED_SUBMODULES):
        raise ImportError(f"import não permitido em tool Python: {name!r}")
    if fromlist:
        module = _module_proxy(name)
        for item in fromlist:
            if item not in _public_names(name):
                raise ImportError(f"nome não permitido em 'from {name} import': {item!r}")
        return module
    return _module_proxy(_root_module(name))


_SAFE_BUILTIN_NAMES = (
    "abs all any bool dict enumerate filter float format frozenset int isinstance len list map max min "
    "next print range repr reversed round set slice sorted str sum tuple zip "
    "True False None "
    "Exception ValueError TypeError KeyError IndexError StopIteration RuntimeError ArithmeticError "
    "ZeroDivisionError AttributeError"
).split()


def _safe_builtins() -> dict[str, Any]:
    import builtins

    safe = {name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES if hasattr(builtins, name)}
    safe["__import__"] = _safe_import
    return safe


def _run_with_timeout(fn: Callable[..., Any], timeout: float, *args: Any, **kwargs: Any) -> Any:
    """Roda numa thread própria e desiste depois de `timeout`.

    Uma thread por chamada, não um pool: como o timeout não interrompe o código
    (ver o docstring do módulo), uma tool travada num pool compartilhado ia
    consumindo workers até nenhuma outra tool Python conseguir rodar. Assim ela
    só queima um slot do teto, e as demais seguem. O contexto é copiado para a
    thread porque `ContextVar` não é herdado automaticamente — é o que mantém
    as `dependencies` da requisição visíveis lá dentro.
    """
    if not _slots.acquire(blocking=False):
        raise RuntimeError(
            f"{_MAX_CONCURRENT_RUNS} tools Python já estão em execução — pode haver alguma travada "
            "(o timeout não interrompe o código). Tente de novo ou reinicie o serviço."
        )
    context = contextvars.copy_context()
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = context.run(fn, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - repropagado no chamador
            box["error"] = exc
        finally:
            _slots.release()

    thread = threading.Thread(target=target, daemon=True, name="python-tool")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"A tool excedeu o limite de {timeout:.0f}s.")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def compile_python_tool(*, tool_name: str, config: dict[str, Any], enabled: bool) -> Callable[..., Any]:
    """`config` já validado (`validate_python_config`). Levanta
    `PythonToolDisabledError` se `enabled=False` — o caller decide a origem
    (flag global `settings.custom_python_tools_enabled` ou a própria tool)."""
    if not enabled:
        raise PythonToolDisabledError(
            "Tools Python estão desligadas (CUSTOM_PYTHON_TOOLS_ENABLED=false) — ver o docstring de "
            "agent_service.tools.python_tool."
        )

    namespace: dict[str, Any] = {"__builtins__": _safe_builtins(), "__name__": f"agent_service.tools.python.{tool_name}"}
    try:
        exec(compile(config["code"], filename=f"<tool:{tool_name}>", mode="exec"), namespace)
    except Exception as exc:  # noqa: BLE001 - reportado como erro de configuração da tool
        raise PythonToolConfigError(f"falha ao carregar o código: {exc}") from exc

    original = namespace.get(config["entrypoint"])
    if not callable(original):
        raise PythonToolConfigError(f"{config['entrypoint']!r} não é uma função em code")

    timeout = config["timeout_seconds"]

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return _run_with_timeout(original, timeout, *args, **kwargs)

    # `functools.wraps` já copia `__wrapped__`/`__doc__`/`__name__`; fixar
    # `__signature__` explicitamente garante que o Agno monte o schema da
    # tool a partir da função original mesmo com o wrapper usando *args/**kwargs.
    wrapper.__signature__ = inspect.signature(original)  # type: ignore[attr-defined]
    return wrapper
