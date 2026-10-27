"""Testes das tools: catálogo de builtins, tools via API (kind="api"), tools
Python sandboxed (kind="python"), o registry que as resolve pro Agno e o CRUD
exposto em `api/tools_routes.py`.
"""

import asyncio
import copy
import json
from unittest.mock import patch

import httpx

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from agent_service.agents.store import create_definition, delete_definition
from agent_service.config import get_settings
from agent_service.tools import api_tool, registry, store
from agent_service.tools.api_tool import ApiToolConfigError, build_api_function, validate_api_config
from agent_service.tools.catalog import BuiltinConfigError, get_builtin_spec, list_builtin_catalog, validate_builtin_config
from agent_service.tools.python_tool import (
    PythonToolConfigError,
    PythonToolDisabledError,
    compile_python_tool,
    validate_python_config,
)
from agent_service.tools.registry import ToolBuildError, UnknownToolError

from agent_service.api.tools_routes import (
    ToolIn,
    ToolInvokeIn,
    ToolUpdateIn,
    create_tool,
    delete_tool,
    get_builtin_catalog,
    get_tool,
    invoke_tool,
    list_tools,
    update_tool,
)


# -- catálogo de builtins -------------------------------------------------


def test_builtin_catalog_has_the_curated_entries():
    ids = {spec.builtin_id for spec in list_builtin_catalog()}
    assert {"calculator", "hackernews", "reasoning", "email", "files", "sleep", "web_search"} <= ids
    assert {"pubmed", "openweather", "file_generation"} <= ids


@pytest.mark.parametrize("builtin_id, params", [
    ("pubmed", {}),
    ("openweather", {"api_key": "chave-de-teste"}),
    ("file_generation", {}),
])
def test_novas_builtins_constroem_sem_dependencia_extra(builtin_id, params):
    """O catálogo é curado: uma entrada que só constrói com um pacote a mais
    apareceria pronta e falharia na hora de usar."""
    built = get_builtin_spec(builtin_id).factory(params, builtin_id)
    assert getattr(built, "functions", {})


def test_catalogo_nao_oferece_toolkit_que_busca_url_arbitraria():
    """Elas têm cliente HTTP próprio e não passam por `tools/egress.py` — seriam
    o caminho curto para o agente alcançar a rede interna."""
    ids = {spec.builtin_id for spec in list_builtin_catalog()}
    assert not ids & {"custom_api", "website", "webtools"}


def test_validate_builtin_config_rejects_unknown_id():
    with pytest.raises(BuiltinConfigError, match="builtin_id"):
        validate_builtin_config({"builtin_id": "does-not-exist"})


def test_validate_builtin_config_requires_required_params():
    with pytest.raises(BuiltinConfigError, match="sender_email"):
        validate_builtin_config({"builtin_id": "email", "params": {"receiver_email": "a@b.com"}})


def test_validate_builtin_config_rejects_unknown_param():
    with pytest.raises(BuiltinConfigError, match="unknown_param"):
        validate_builtin_config({"builtin_id": "calculator", "params": {"unknown_param": 1}})


def test_validate_builtin_config_rejects_wrong_type():
    with pytest.raises(BuiltinConfigError, match="booleano"):
        validate_builtin_config({"builtin_id": "web_search", "params": {"enable_news": "yes"}})


def test_validate_builtin_config_applies_defaults_and_keeps_valid_values():
    normalized = validate_builtin_config({"builtin_id": "web_search", "params": {"max_results": 5}})
    assert normalized == {"builtin_id": "web_search", "params": {"max_results": 5, "enable_news": True}}


# -- tools kind="api" ------------------------------------------------------


def test_validate_api_config_rejects_bad_method_and_scheme():
    with pytest.raises(ApiToolConfigError, match="method"):
        validate_api_config({"method": "TRACE", "url": "https://x.com"})
    with pytest.raises(ApiToolConfigError, match="url"):
        validate_api_config({"method": "GET", "url": "ftp://x.com"})


def test_validate_api_config_requires_path_param_for_placeholder():
    with pytest.raises(ApiToolConfigError, match="path"):
        validate_api_config({"method": "GET", "url": "https://x.com/{id}", "parameters": []})


def test_validate_api_config_rejects_duplicate_and_bad_param():
    with pytest.raises(ApiToolConfigError, match="duplicado"):
        validate_api_config(
            {
                "method": "GET",
                "url": "https://x.com",
                "parameters": [{"name": "q", "type": "string"}, {"name": "q", "type": "string"}],
            }
        )
    with pytest.raises(ApiToolConfigError, match="tipo inválido"):
        validate_api_config({"method": "GET", "url": "https://x.com", "parameters": [{"name": "q", "type": "vector"}]})


def test_validate_api_config_requires_auth_fields():
    with pytest.raises(ApiToolConfigError, match="token"):
        validate_api_config({"method": "GET", "url": "https://x.com", "auth": {"type": "bearer"}})


def test_validate_api_config_rejects_timeout_out_of_range():
    with pytest.raises(ApiToolConfigError, match="timeout_seconds"):
        validate_api_config({"method": "GET", "url": "https://x.com", "timeout_seconds": 999})


def rede_falsa(status_code=200, json_data=None, text="", erro=None):
    """Troca o cliente das tools de API por um com `MockTransport`.

    O entrypoint de uma tool `kind="api"` é assíncrono e usa um cliente
    compartilhado (ver `tools/api_tool.py`), então o teste intercepta no
    transporte em vez de mockar uma função de módulo.
    """
    enviadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        enviadas.append(request)
        if erro is not None:
            raise erro
        if json_data is not None:
            return httpx.Response(status_code, json=json_data)
        return httpx.Response(status_code, text=text)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return patch.object(api_tool, "_client", client), enviadas


def chamar(fn, **kwargs):
    """O entrypoint virou corrotina — ver `tools/api_tool.py`."""
    return asyncio.run(fn.entrypoint(**kwargs))


def invocar(tool_name, body):
    return asyncio.run(invoke_tool(tool_name, body))


def test_api_function_substitutes_path_sends_query_and_bearer_auth():
    config = {
        "method": "GET",
        "url": "https://api.example.com/items/{item_id}",
        "parameters": [
            {"name": "item_id", "type": "string", "location": "path", "required": True},
            {"name": "q", "type": "string", "location": "query"},
        ],
        "auth": {"type": "bearer", "token": "s3cr3t"},
    }
    fn = build_api_function(tool_name="items_api", description=None, config=config)

    mock, enviadas = rede_falsa(200, {"ok": True})
    with mock:
        result = chamar(fn, item_id="42", q="hello")

    assert "HTTP 200" in result and '"ok": true' in result
    enviada = enviadas[-1]
    assert enviada.method == "GET"
    assert str(enviada.url).startswith("https://api.example.com/items/42")
    assert dict(enviada.url.params) == {"q": "hello"}
    assert enviada.headers["Authorization"] == "Bearer s3cr3t"


def test_api_function_sends_body_params_as_json():
    config = {
        "method": "POST",
        "url": "https://api.example.com/items",
        "parameters": [{"name": "name", "type": "string", "location": "body", "required": True}],
    }
    fn = build_api_function(tool_name="create_item", description=None, config=config)

    mock, enviadas = rede_falsa(201, {"id": 1})
    with mock:
        chamar(fn, name="Maria")

    assert json.loads(enviadas[-1].content) == {"name": "Maria"}


def test_api_function_truncates_huge_text_response():
    config = {"method": "GET", "url": "https://api.example.com/big", "parameters": []}
    fn = build_api_function(tool_name="big", description=None, config=config)

    mock, _ = rede_falsa(200, text="x" * 20_000)
    with mock:
        result = chamar(fn)

    assert "truncado" in result
    assert len(result) < 8500


def test_api_function_reports_http_errors_without_raising():
    config = {"method": "GET", "url": "https://api.example.com/down", "parameters": []}
    fn = build_api_function(tool_name="down", description=None, config=config)

    mock, _ = rede_falsa(erro=httpx.ConnectTimeout("timed out"))
    with mock:
        result = chamar(fn)

    assert "Falha ao chamar a API" in result


# -- tools kind="python" ----------------------------------------------------


def test_validate_python_config_accepts_simple_function():
    validated = validate_python_config({"code": "def handler(x: int) -> int:\n    return x * 2\n", "entrypoint": "handler"})
    assert validated["entrypoint"] == "handler"


def test_validate_python_config_requires_matching_entrypoint():
    with pytest.raises(PythonToolConfigError, match="handler"):
        validate_python_config({"code": "def other(x: int) -> int:\n    return x\n", "entrypoint": "handler"})


@pytest.mark.parametrize(
    "code",
    [
        "import os\ndef handler():\n    return os.getcwd()\n",
        "def handler():\n    return eval('1')\n",
        "def handler():\n    return ().__class__.__bases__\n",
        "class Foo:\n    pass\ndef handler():\n    return 1\n",
        "x = __import__('os')\ndef handler():\n    return x\n",
    ],
)
def test_validate_python_config_rejects_dangerous_code(code):
    with pytest.raises(PythonToolConfigError):
        validate_python_config({"code": code, "entrypoint": "handler"})


def test_validate_python_config_allows_listed_modules():
    validate_python_config(
        {"code": "import math, json, re\ndef handler(x: float) -> float:\n    return math.sqrt(x)\n", "entrypoint": "handler"}
    )


def test_compile_python_tool_raises_when_disabled():
    config = validate_python_config({"code": "def handler() -> int:\n    return 1\n", "entrypoint": "handler"})
    with pytest.raises(PythonToolDisabledError):
        compile_python_tool(tool_name="t", config=config, enabled=False)


def test_compile_python_tool_runs_and_uses_allowed_import():
    config = validate_python_config(
        {"code": "import math\ndef handler(x: float) -> float:\n    return math.sqrt(x)\n", "entrypoint": "handler"}
    )
    fn = compile_python_tool(tool_name="sqrt_tool", config=config, enabled=True)
    assert fn(x=16.0) == 4.0
    assert fn.__name__ == "handler"


def test_compile_python_tool_enforces_timeout():
    config = validate_python_config(
        {
            "code": "import time\ndef handler() -> str:\n    time.sleep(0.3)\n    return 'done'\n",
            "entrypoint": "handler",
            "timeout_seconds": 0.05,
        }
    )
    fn = compile_python_tool(tool_name="slow_tool", config=config, enabled=True)
    with pytest.raises(TimeoutError):
        fn()


def test_compile_python_tool_propagates_errors_from_the_function():
    config = validate_python_config({"code": "def handler() -> int:\n    return 1 / 0\n", "entrypoint": "handler"})
    fn = compile_python_tool(tool_name="boom_tool", config=config, enabled=True)
    with pytest.raises(ZeroDivisionError):
        fn()


# -- registry ---------------------------------------------------------------


def test_registry_resolves_seeded_builtin_tool():
    [calc] = registry.resolve_tools(["calculator"])
    assert "add" in calc.functions


def test_registry_unknown_tool_raises():
    with pytest.raises(UnknownToolError):
        registry.resolve_tools(["does-not-exist"])


def test_registry_disabled_tool_raises_build_error():
    store.create_tool(tool_name="disabled_tool", kind="builtin", label="x", description=None, config={"builtin_id": "calculator", "params": {}}, enabled=False)
    with pytest.raises(ToolBuildError, match="desativada"):
        registry.resolve_tools(["disabled_tool"])


def test_registry_caches_until_updated_at_changes():
    store.create_tool(tool_name="cache_tool", kind="builtin", label="x", description=None, config={"builtin_id": "calculator", "params": {}})
    [first] = registry.resolve_tools(["cache_tool"])
    [second] = registry.resolve_tools(["cache_tool"])
    assert first is second

    # SQLite (usado nos testes) arredonda `func.now()` ao segundo — uma edição
    # real dentro do mesmo segundo não muda `updated_at`. Simula o "miss" do
    # jeito que uma mudança de fato faria: um `updated_at` diferente em cache.
    from datetime import datetime

    stale_object, _ = registry._cache["cache_tool"]
    registry._cache["cache_tool"] = (stale_object, datetime.min)
    [third] = registry.resolve_tools(["cache_tool"])
    assert third is not first


# -- rotas: CRUD --------------------------------------------------------


def test_list_tools_includes_seeded_examples():
    names = {t["tool_name"] for t in list_tools()}
    assert {"calculator", "hackernews", "cat_fact"} <= names


def test_get_builtin_catalog_route_matches_catalog_module():
    payload = get_builtin_catalog()
    assert {e["builtin_id"] for e in payload} == {s.builtin_id for s in list_builtin_catalog()}


def test_create_tool_rejects_bad_slug():
    with pytest.raises(ValidationError):
        ToolIn(tool_name="Bad Name!", kind="builtin", label="x", config={"builtin_id": "calculator"})


def test_create_tool_builtin_and_duplicate():
    body = ToolIn(tool_name="calc_2", kind="builtin", label="Calc 2", config={"builtin_id": "calculator", "params": {}})
    created = create_tool(body)
    assert created["tool_name"] == "calc_2"

    with pytest.raises(HTTPException) as exc:
        create_tool(body)
    assert exc.value.status_code == 409


def test_create_tool_api_rejects_invalid_config():
    with pytest.raises(HTTPException) as exc:
        create_tool(ToolIn(tool_name="bad_api", kind="api", label="x", config={"method": "GET", "url": "not-a-url"}))
    assert exc.value.status_code == 422


def test_create_python_tool_allowed_even_while_disabled_but_invoke_is_not():
    settings = get_settings()
    assert settings.custom_python_tools_enabled is False  # default do projeto

    created = create_tool(
        ToolIn(
            tool_name="py_tool_disabled",
            kind="python",
            label="Python disabled",
            config={"code": "def handler(x: int) -> int:\n    return x + 1\n", "entrypoint": "handler"},
        )
    )
    assert created["tool_name"] == "py_tool_disabled"

    with pytest.raises(HTTPException) as exc:
        invocar("py_tool_disabled", ToolInvokeIn(arguments={"x": 1}))
    assert exc.value.status_code == 403


def test_invoke_python_tool_when_enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "custom_python_tools_enabled", True)

    create_tool(
        ToolIn(
            tool_name="py_tool_enabled",
            kind="python",
            label="Python enabled",
            config={"code": "def handler(x: int) -> int:\n    return x + 1\n", "entrypoint": "handler"},
        )
    )
    result = invocar("py_tool_enabled", ToolInvokeIn(arguments={"x": 41}))
    assert result["ok"] is True
    assert result["result"] == 42


def test_invoke_api_tool_mocking_http():
    mock, _ = rede_falsa(200, {"fact": "meow"})
    with mock:
        result = invocar("cat_fact", ToolInvokeIn())
    assert result["ok"] is True
    assert "meow" in result["result"]


def test_invoke_builtin_tool_requires_function_name_then_succeeds():
    with pytest.raises(HTTPException) as exc:
        invocar("calculator", ToolInvokeIn())
    assert exc.value.status_code == 422
    assert "add" in exc.value.detail

    result = invocar("calculator", ToolInvokeIn(function_name="add", arguments={"a": 2, "b": 3}))
    assert result["ok"] is True


def test_invoke_tool_that_fails_to_build_reports_error_not_500():
    create_tool(
        ToolIn(
            tool_name="broken_builtin",
            kind="builtin",
            label="broken",
            config={"builtin_id": "web_search", "params": {}},  # ddgs não está instalado neste ambiente de teste
        )
    )
    result = invocar("broken_builtin", ToolInvokeIn())
    assert result["ok"] is False
    assert result["error"]


def test_update_tool_masks_and_preserves_secret_when_echoed_back():
    create_tool(
        ToolIn(
            tool_name="secret_api",
            kind="api",
            label="Secret API",
            config={"method": "GET", "url": "https://x.com/a", "auth": {"type": "bearer", "token": "real-secret"}},
        )
    )
    fetched = get_tool("secret_api")
    masked_token = fetched["config"]["auth"]["token"]
    assert masked_token != "real-secret"

    update_tool(
        "secret_api",
        ToolUpdateIn(config={"method": "GET", "url": "https://x.com/b", "auth": {"type": "bearer", "token": masked_token}}),
    )

    raw = store.get_tool("secret_api")
    assert raw["config"]["auth"]["token"] == "real-secret"  # preservado
    assert raw["config"]["url"] == "https://x.com/b"  # o resto da edição foi aplicado


def test_secret_headers_are_masked_and_preserved_when_echoed_back():
    create_tool(
        ToolIn(
            tool_name="header_secret_api",
            kind="api",
            label="Header secret",
            config={
                "method": "GET",
                "url": "https://x.com/a",
                "headers": {"X-Regente-Token": "real-secret", "Authorization": "Bearer abc", "Accept": "application/json"},
            },
        )
    )
    headers = get_tool("header_secret_api")["config"]["headers"]
    assert headers["X-Regente-Token"] != "real-secret"
    assert headers["Authorization"] != "Bearer abc"
    assert headers["Accept"] == "application/json"  # não é segredo: continua legível

    # Ecoar a máscara (o `--editable` → `apply`) não troca o token pelo texto da máscara;
    # o nome do header pode vir com outra caixa.
    update_tool(
        "header_secret_api",
        ToolUpdateIn(
            config={
                "method": "GET",
                "url": "https://x.com/b",
                "headers": {"x-regente-token": headers["X-Regente-Token"], "Authorization": "Bearer novo"},
            }
        ),
    )

    raw = store.get_tool("header_secret_api")["config"]
    assert raw["headers"] == {"x-regente-token": "real-secret", "Authorization": "Bearer novo"}
    assert raw["url"] == "https://x.com/b"


def test_delete_tool_blocked_when_seeded_or_in_use():
    with pytest.raises(HTTPException) as exc:
        delete_tool("calculator")  # semeada
    assert exc.value.status_code == 403

    store.create_tool(tool_name="in_use_tool", kind="builtin", label="x", description=None, config={"builtin_id": "calculator", "params": {}})
    create_definition(agent_type="uses-tool", name="Usa Tool", instructions=["oi"], tools=["in_use_tool"])
    try:
        with pytest.raises(HTTPException) as exc:
            delete_tool("in_use_tool")
        assert exc.value.status_code == 409
    finally:
        delete_definition("uses-tool")

    delete_tool("in_use_tool")  # agora sem uso, funciona
    assert store.get_tool("in_use_tool") is None


def test_get_tool_reports_build_error_for_missing_optional_dependency():
    create_tool(ToolIn(tool_name="web_search_probe", kind="builtin", label="x", config={"builtin_id": "web_search", "params": {}}))
    detail = get_tool("web_search_probe")
    assert detail.get("build_error")  # ddgs não instalado neste ambiente de teste


def test_get_tool_lists_functions_for_working_builtin():
    detail = get_tool("calculator")
    assert "add" in detail["functions"]


# -- integração com agents_routes: nomes de tool precisam existir -----------


def test_agent_create_rejects_unknown_tool_name():
    from agent_service.api.agents_routes import AgentDefinitionIn, create_agent

    with pytest.raises(HTTPException) as exc:
        create_agent(
            AgentDefinitionIn(agent_type="tool-checker", name="x", instructions=["oi"], tools=["not-a-real-tool"])
        )
    assert exc.value.status_code == 422


def test_agent_create_accepts_known_tool_name():
    from agent_service.api.agents_routes import AgentDefinitionIn, create_agent

    created = create_agent(
        AgentDefinitionIn(agent_type="tool-checker-2", name="x", instructions=["oi"], tools=["calculator"])
    )
    assert created["tools"] == ["calculator"]
    delete_definition("tool-checker-2")


def test_tool_python_travada_nao_impede_as_outras():
    """Antes todas as tools Python dividiam um pool de 8 workers e o timeout não
    interrompe o código: oito tools travadas e nenhuma outra rodava mais. Agora
    cada chamada tem a sua thread, então uma travada só queima um slot do teto."""
    travada = compile_python_tool(
        tool_name="trava",
        config=validate_python_config(
            {"code": "import time\ndef handler() -> int:\n    time.sleep(2)\n    return 1\n",
             "entrypoint": "handler", "timeout_seconds": 0.1}
        ),
        enabled=True,
    )
    for _ in range(10):
        with pytest.raises(TimeoutError):
            travada()

    rapida = compile_python_tool(
        tool_name="rapida",
        config=validate_python_config({"code": "def handler() -> int:\n    return 7\n", "entrypoint": "handler"}),
        enabled=True,
    )
    assert rapida() == 7


def test_tool_python_enxerga_as_dependencies_da_requisicao():
    """A thread da tool recebe uma cópia do contexto — `ContextVar` não é herdado."""
    from agent_service.tools.context import dependencies_scope, get_dependencies

    validate_python_config({"code": "def handler() -> int:\n    return 1\n", "entrypoint": "handler"})
    visto = {}

    def espia() -> int:
        visto.update(get_dependencies())
        return 1

    with dependencies_scope({"cpf": "123"}):
        from agent_service.tools.python_tool import _run_with_timeout

        _run_with_timeout(espia, 5.0)
    assert visto == {"cpf": "123"}


# -- parâmetros compostos numa tool de API ---------------------------------------
#
# Mesmo buraco que o `response_schema` tinha: um `array` sem `items` ou um
# `object` sem `fields` vira uma declaração de tool que o provedor recusa — e o
# erro só aparecia na chamada, longe de quem escreveu a tool.

BASE_API = {"method": "POST", "url": "https://exemplo.test/busca"}


@pytest.mark.parametrize(
    "parametro, trecho",
    [
        ({"name": "ids", "type": "array", "location": "body"}, "items"),
        ({"name": "filtros", "type": "object", "location": "body"}, "fields"),
        ({"name": "itens", "type": "array", "location": "body", "items": {"type": "object"}}, "fields"),
        ({"name": "matriz", "type": "array", "location": "body", "items": {"type": "array"}}, "tipo inválido"),
    ],
)
def test_parametro_composto_sem_sub_schema_e_recusado(parametro, trecho):
    with pytest.raises(ApiToolConfigError) as exc:
        validate_api_config({**BASE_API, "parameters": [parametro]})
    assert trecho in str(exc.value)


def test_schema_da_tool_sai_com_items_e_properties():
    config = validate_api_config(
        {
            **BASE_API,
            "parameters": [
                {"name": "ids", "type": "array", "location": "body", "required": True,
                 "items": {"type": "integer"}},
                {"name": "filtros", "type": "object", "location": "body",
                 "fields": [{"name": "status", "type": "string", "required": True}]},
            ],
        }
    )
    schema = build_api_function(tool_name="busca", description=None, config=config).parameters
    assert schema["properties"]["ids"]["items"] == {"type": "integer"}
    filtros = schema["properties"]["filtros"]
    assert filtros["properties"]["status"]["type"] == "string"
    assert filtros["required"] == ["status"]
    assert schema["required"] == ["ids"]  # só o que é obrigatório para o modelo


def test_composto_de_dependency_ou_const_nao_precisa_de_sub_schema():
    """Eles não entram no schema do modelo (`_parameters_schema` os pula), então
    não há declaração inválida a evitar — exigir sub-schema só quebraria tool salva."""
    config = validate_api_config(
        {
            **BASE_API,
            "parameters": [
                {"name": "perfil", "type": "object", "location": "body",
                 "source": "dependency", "dependency": "perfil"},
                {"name": "flags", "type": "array", "location": "body", "source": "const", "value": [1, 2]},
            ],
        }
    )
    schema = build_api_function(tool_name="busca", description=None, config=config).parameters
    assert schema["properties"] == {}


def test_config_de_tool_com_composto_sobrevive_ao_get_e_apply():
    """O fluxo `tools get --editable > f.json && tools apply -f f.json`: a config
    lida já vem normalizada, e revalidá-la não pode fazer o `fields`/`items`
    derivar a cada edição."""
    config = {
        "method": "POST", "url": "https://exemplo.test/busca",
        "parameters": [
            {"name": "ids", "type": "array", "location": "body", "items": {"type": "integer"}},
            {"name": "filtros", "type": "object", "location": "body",
             "fields": [{"name": "status", "type": "string", "required": True}]},
        ],
    }
    criada = create_tool(ToolIn(tool_name="rt_tool", kind="api", label="RT", config=config))
    try:
        reaplicada = update_tool("rt_tool", ToolUpdateIn(config=get_tool("rt_tool")["config"]))
        assert reaplicada["config"]["parameters"] == criada["config"]["parameters"]
        de_novo = update_tool("rt_tool", ToolUpdateIn(config=get_tool("rt_tool")["config"]))
        assert de_novo["config"]["parameters"] == criada["config"]["parameters"]
    finally:
        delete_tool("rt_tool")


def test_validar_nao_altera_o_config_de_quem_chamou():
    """A normalização do composto acontece numa cópia."""
    original = {**BASE_API, "parameters": [
        {"name": "ids", "type": "array", "location": "body", "items": {"type": "integer"}}
    ]}
    antes = copy.deepcopy(original)
    validate_api_config(original)
    assert original == antes
