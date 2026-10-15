"""Origem dos parâmetros de uma tool `kind="api"` (`source`) e o contrato que
ela cria com o agente: o que o modelo vê, o que o servidor injeta a partir de
`dependencies`, e a recusa de salvar um agente que não declara o que a tool exige.
"""

import asyncio
import time
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

from agent_service.tools import api_tool

from agent_service.api.agents_routes import (
    AgentDefinitionIn,
    AgentDefinitionUpdate,
    create_agent,
    delete_agent,
    update_agent,
)
from agent_service.api.tools_routes import ToolIn, ToolInvokeIn, create_tool, delete_tool, invoke_tool
from agent_service.tools.api_tool import (
    ApiToolConfigError,
    build_api_function,
    required_dependencies,
    validate_api_config,
)
from agent_service.tools.context import dependencies_scope

CONFIG = {
    "method": "GET",
    "url": "https://exemplo.test/clientes/{cpf}",
    "parameters": [
        {"name": "cpf", "type": "string", "location": "path", "required": True,
         "source": "dependency", "dependency": "cpf"},
        {"name": "assunto", "type": "string", "location": "query", "required": True},
        {"name": "origem", "type": "string", "location": "query", "source": "const", "value": "agente"},
    ],
}


class RedeFalsa:
    """Substitui o cliente das tools de API por um com `MockTransport`.

    Guarda as requisições enviadas, então o teste continua conseguindo afirmar
    para onde a tool falou e com o quê — agora olhando a `httpx.Request` real,
    em vez dos argumentos de uma chamada mockada.
    """

    def __init__(self, payload=None, status_code=200):
        self.requests: list[httpx.Request] = []
        self._payload = payload
        self._status = status_code

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status, json=self._payload)

    def __enter__(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler), follow_redirects=False)
        self._patch = patch.object(api_tool, "_client", client)
        self._patch.start()
        return self

    def __exit__(self, *exc_info):
        self._patch.stop()
        return False

    @property
    def nao_chamou(self) -> bool:
        return not self.requests


def invocar(tool_name, body):
    """`POST /tools/{nome}/invoke` é `async` desde que o entrypoint virou corrotina."""
    return asyncio.run(invoke_tool(tool_name, body))


@pytest.fixture
def ficha_tool():
    create_tool(ToolIn(tool_name="ficha_cliente", kind="api", label="Ficha do cliente", config=CONFIG))
    yield "ficha_cliente"
    delete_tool("ficha_cliente")


# -- schema e validação ---------------------------------------------------------


def test_model_only_sees_model_sourced_parameters():
    fn = build_api_function(tool_name="ficha", description=None, config=validate_api_config(CONFIG))
    assert fn.parameters["properties"].keys() == {"assunto"}, "cpf e origem não são assunto do modelo"
    assert fn.parameters["required"] == ["assunto"]


def test_required_dependencies_lists_what_the_agent_must_declare():
    assert required_dependencies(validate_api_config(CONFIG)) == ["cpf"]


@pytest.mark.parametrize(
    "param, erro",
    [
        ({"name": "x", "type": "string", "source": "inventado"}, "source inválido"),
        ({"name": "x", "type": "string", "source": "dependency"}, "exige `dependency`"),
        ({"name": "x", "type": "string", "source": "const"}, "exige `value`"),
        ({"name": "Authorization", "type": "string", "location": "header"}, "header Authorization"),
    ],
)
def test_invalid_parameter_sources_are_rejected(param, erro):
    with pytest.raises(ApiToolConfigError, match=erro):
        validate_api_config({"method": "GET", "url": "https://x.test", "parameters": [param]})


def test_header_from_dependency_can_have_a_real_header_name():
    """`Idempotency-Key`, `X-Request-Id`: headers HTTP de verdade têm hífen. Vale
    para o que o modelo não preenche; o que ele preenche vira propriedade do schema."""
    config = validate_api_config({
        "method": "POST",
        "url": "https://x.test/chamados",
        "parameters": [
            {"name": "Idempotency-Key", "type": "string", "location": "header", "required": True,
             "source": "dependency", "dependency": "idempotency_key"},
            {"name": "X-Origem", "type": "string", "location": "header", "source": "const", "value": "kuro"},
            {"name": "assunto", "type": "string", "location": "body", "required": True},
        ],
    })
    # Preenchida pelo servidor (etapa action ou header Idempotency-Key): não se declara.
    assert required_dependencies(config) == []
    fn = build_api_function(tool_name="chamado", description=None, config=config)
    assert fn.parameters["properties"].keys() == {"assunto"}
    with RedeFalsa({"ok": True}) as rede, dependencies_scope({"idempotency_key": "abc:abrir:123"}):
        asyncio.run(fn.entrypoint(assunto="tv"))
    enviada = rede.requests[-1]
    assert enviada.headers["Idempotency-Key"] == "abc:abrir:123"
    assert enviada.headers["X-Origem"] == "kuro"


@pytest.mark.parametrize(
    "param",
    [
        {"name": "Idempotency-Key", "type": "string", "location": "header"},  # o modelo preencheria
        {"name": "page-size", "type": "integer", "location": "query", "source": "const", "value": 10},
        {"name": "-X", "type": "string", "location": "header", "source": "const", "value": "a"},
    ],
)
def test_hyphen_only_in_headers_the_model_does_not_fill(param):
    with pytest.raises(ApiToolConfigError, match="nome de parâmetro inválido"):
        validate_api_config({"method": "GET", "url": "https://x.test", "parameters": [param]})


def test_parameter_cannot_hijack_the_api_key_header():
    with pytest.raises(ApiToolConfigError, match="header de autenticação"):
        validate_api_config({
            "method": "GET",
            "url": "https://x.test",
            "parameters": [{"name": "X_Key", "type": "string", "location": "header"}],
            "auth": {"type": "api_key", "header": "x_key", "value": "segredo"},
        })


# -- execução --------------------------------------------------------------------


def test_dependency_value_reaches_the_api_without_passing_through_the_model(ficha_tool):
    with RedeFalsa({"ok": True}) as rede:
        result = invocar(
            ficha_tool, ToolInvokeIn(arguments={"assunto": "fatura"}, dependencies={"cpf": "12345678900"})
        )
    assert result["ok"] is True
    enviada = rede.requests[-1]
    assert str(enviada.url).startswith("https://exemplo.test/clientes/12345678900")
    assert dict(enviada.url.params) == {"assunto": "fatura", "origem": "agente"}


def test_missing_dependency_explains_instead_of_calling_the_api(ficha_tool):
    with RedeFalsa() as rede:
        result = invocar(ficha_tool, ToolInvokeIn(arguments={"assunto": "fatura"}))
    assert rede.nao_chamou
    assert "dependencies.cpf" in result["result"]


def test_missing_required_model_parameter_is_charged_before_the_call(ficha_tool):
    with RedeFalsa() as rede:
        result = invocar(ficha_tool, ToolInvokeIn(dependencies={"cpf": "1"}))
    assert rede.nao_chamou
    assert "assunto" in result["result"]


def test_dependencies_do_not_leak_between_calls(ficha_tool):
    with dependencies_scope({"cpf": "1"}):
        pass
    with RedeFalsa() as rede:
        result = invocar(ficha_tool, ToolInvokeIn(arguments={"assunto": "x"}))
    assert rede.nao_chamou
    assert "dependencies.cpf" in result["result"]


# -- contrato com o agente --------------------------------------------------------


def _agent_body(**extra):
    return AgentDefinitionIn(
        agent_type="agente_ficha", name="Ficha", instructions=["Ajude."], tools=["ficha_cliente"], **extra
    )


def test_agent_cannot_use_a_tool_whose_dependency_it_does_not_declare(ficha_tool):
    with pytest.raises(HTTPException) as exc:
        create_agent(_agent_body())
    assert exc.value.status_code == 422
    assert "cpf" in exc.value.detail and "ficha_cliente" in exc.value.detail


def test_agent_with_the_declared_dependency_is_accepted_and_kept_consistent(ficha_tool):
    create_agent(_agent_body(dependency_fields=[{"name": "cpf", "type": "string", "required": True}]))
    try:
        # Remover o campo depois deixaria a tool sem o dado — também é recusado.
        with pytest.raises(HTTPException) as exc:
            update_agent("agente_ficha", AgentDefinitionUpdate(dependency_fields=[]))
        assert exc.value.status_code == 422
        assert "cpf" in exc.value.detail

        # Tirar a tool, por outro lado, libera o campo.
        update_agent("agente_ficha", AgentDefinitionUpdate(tools=[], dependency_fields=[]))
    finally:
        delete_agent("agente_ficha")


def test_editing_a_tool_reaches_agents_already_built(ficha_tool):
    """O schema que o modelo vê mora dentro do Function preso no Agent construído."""
    from agent_service.agents.registry import get_agent
    from agent_service.api.tools_routes import ToolUpdateIn, update_tool

    create_agent(_agent_body(dependency_fields=[{"name": "cpf", "type": "string", "required": True}]))
    try:
        antes = get_agent("agente_ficha").tools[0].parameters["properties"]
        assert antes.keys() == {"assunto"}

        novo = {**CONFIG, "parameters": [*CONFIG["parameters"],
                                         {"name": "canal", "type": "string", "location": "query"}]}
        # A invalidação compara `updated_at`, e no SQLite dos testes ele tem
        # resolução de 1 segundo (no Postgres, microssegundos).
        time.sleep(1.1)
        update_tool("ficha_cliente", ToolUpdateIn(config=novo))

        depois = get_agent("agente_ficha").tools[0].parameters["properties"]
        assert depois.keys() == {"assunto", "canal"}
    finally:
        delete_agent("agente_ficha")


def test_tool_cannot_start_requiring_a_dependency_its_agents_do_not_declare(ficha_tool):
    from agent_service.api.tools_routes import ToolUpdateIn, update_tool

    create_agent(_agent_body(dependency_fields=[{"name": "cpf", "type": "string", "required": True}]))
    try:
        novo = {**CONFIG, "parameters": [*CONFIG["parameters"],
                                         {"name": "conta", "type": "string", "location": "query", "required": True,
                                          "source": "dependency", "dependency": "conta_id"}]}
        with pytest.raises(HTTPException) as exc:
            update_tool("ficha_cliente", ToolUpdateIn(config=novo))
        assert exc.value.status_code == 409
        assert "agente_ficha" in exc.value.detail and "conta_id" in exc.value.detail
    finally:
        delete_agent("agente_ficha")


# -- dry_run ---------------------------------------------------------------------

CONFIG_DRY_RUN = {
    "method": "POST",
    "url": "https://exemplo.test/acordos",
    "parameters": [
        {"name": "valor", "type": "number", "location": "body", "required": True},
        {"name": "simular", "type": "boolean", "location": "body", "required": True,
         "source": "dependency", "dependency": "dry_run"},
    ],
}


@pytest.fixture
def acordo_tool():
    create_tool(ToolIn(tool_name="fechar_acordo", kind="api", label="Fechar acordo", config=CONFIG_DRY_RUN))
    yield "fechar_acordo"
    delete_tool("fechar_acordo")


def test_dry_run_avisa_a_api_pelo_header_e_pela_dependency(acordo_tool):
    import json

    with RedeFalsa({"ok": True}) as rede:
        invocar(acordo_tool, ToolInvokeIn(arguments={"valor": 100}, dry_run=True))
    enviada = rede.requests[-1]
    assert enviada.headers["X-Kuro-Dry-Run"] == "true"
    assert json.loads(enviada.content) == {"valor": 100, "simular": True}


def test_sem_dry_run_nao_ha_header_e_a_dependency_vale_false(acordo_tool):
    import json

    with RedeFalsa({"ok": True}) as rede:
        invocar(acordo_tool, ToolInvokeIn(arguments={"valor": 100}, dependencies={"dry_run": True}))
    enviada = rede.requests[-1]
    assert "X-Kuro-Dry-Run" not in enviada.headers
    # Quem manda é o flag do servidor, não um `dry_run` vindo nas dependencies.
    assert json.loads(enviada.content)["simular"] is False


def test_dry_run_nao_precisa_ser_declarado_pelo_agente():
    assert required_dependencies(validate_api_config(CONFIG_DRY_RUN)) == []


def test_dry_run_marca_o_run():
    from agent_service.api.routes import _run_metadata

    assert _run_metadata({"conversation_id": 7}, True) == {"conversation_id": "7", "dry_run": "true"}
    assert _run_metadata(None, False) == {}
