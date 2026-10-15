"""Tools `kind="api"`: chamam uma API HTTP existente, descrita em JSON — sem
escrever código. `config` diz o método, a URL (com placeholders `{nome}` para
parâmetros de path), os parâmetros e a autenticação.

Cada parâmetro declara de onde vem o valor (`source`):

- `"model"` (padrão): o modelo preenche — é o único que aparece no schema.
- `"dependency"`: o servidor injeta `dependencies[<campo>]` da requisição
  (`dependency: "cpf"`). O modelo não vê o parâmetro e portanto não pode
  inventá-lo — é assim que um CPF chega na API sem passar pelo LLM.
- `"const"`: valor fixo em `value`.

`required` é cobrado aqui, antes da chamada HTTP: faltando um obrigatório, a
tool devolve um erro explicando o que faltou em vez de chamar a API pela metade.

`build_api_function` monta um `agno.tools.function.Function` com `parameters`
(o JSON Schema que o modelo vê) e `entrypoint` explícitos — o Agno chama
`entrypoint(**argumentos)` diretamente, sem inspecionar uma assinatura Python
real (`skip_entrypoint_processing=True`), que é o mecanismo que permite uma
tool inteiramente definida por dados.

O entrypoint é `async` de propósito. No caminho assíncrono do Agno
(`Function.aexecute`), um entrypoint síncrono é chamado direto, sem thread —
e o `/chat` é `async` de ponta a ponta. Um `httpx.request` síncrono ali dentro
travaria o event loop inteiro durante toda a chamada HTTP (até
`timeout_seconds`), parando todas as outras requisições do worker. O cliente é
único e reaproveitado, então a conexão (e o handshake TLS) é reusada entre
chamadas em vez de refeita a cada uma.

Todo destino passa por `tools/egress.py` antes do envio: uma tool só alcança
endereços públicos, exceto o que estiver em `TOOL_EGRESS_ALLOWLIST`. A
checagem é feita com a URL já montada porque um parâmetro `location="path"`
pode compor o host (`https://{host}/x`), e aí o destino seria escolha do modelo.
"""

import asyncio
import copy
import re
import weakref
from typing import Any, Literal

import httpx
from agno.tools.function import Function

from agent_service.field_schema import FieldSchemaError, object_schema, validate_composite
from agent_service.tools import failures
from agent_service.tools.context import (
    DRY_RUN_DEPENDENCY,
    DRY_RUN_HEADER,
    IDEMPOTENCY_DEPENDENCY,
    get_dependencies,
    get_idempotency_key,
    is_dry_run,
)
from agent_service.tools.egress import EgressBlockedError, acheck_url, check_url_template

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
ParamLocation = Literal["query", "path", "header", "body"]
ParamType = Literal["string", "integer", "number", "boolean", "object", "array"]
AuthType = Literal["none", "bearer", "api_key", "basic"]
ParamSource = Literal["model", "dependency", "const"]
_SOURCES: set[str] = {"model", "dependency", "const"}

_JSON_SCHEMA_TYPE: dict[ParamType, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}
_METHODS: set[str] = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_MAX_RESPONSE_CHARS = 8000
_DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_TIMEOUT_SECONDS = 60.0

_client: httpx.AsyncClient | None = None
"""Substituto explícito — os testes põem aqui um cliente com `MockTransport`."""

_clients_por_loop: "weakref.WeakKeyDictionary[Any, httpx.AsyncClient]" = weakref.WeakKeyDictionary()


def get_client() -> httpx.AsyncClient:
    """Cliente das tools de API — o pool de conexões vive aqui.

    Um por event loop, não um global: o pool do `httpx` guarda conexões e locks
    presos ao loop que as criou, e reusar num loop diferente (um worker que
    reinicia o loop, um teste com dois `asyncio.run`) estoura em cima de
    conexões mortas. Em produção há um loop só, então na prática é um cliente
    só, com a conexão e o handshake TLS reaproveitados entre chamadas.

    `follow_redirects` fica desligado (o padrão do httpx) de propósito: seguir
    um 302 levaria a um destino que ninguém checou, e o modelo lida bem com um
    3xx na resposta.
    """
    if _client is not None:
        return _client
    loop = asyncio.get_running_loop()
    client = _clients_por_loop.get(loop)
    if client is None:
        client = httpx.AsyncClient(follow_redirects=False)
        _clients_por_loop[loop] = client
    return client


class ApiToolConfigError(ValueError):
    """`config` inválido — reportado como 422 na criação/edição da tool."""


def validate_api_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normaliza e valida `config`; levanta `ApiToolConfigError` com uma
    mensagem específica do primeiro problema encontrado."""
    method = str(config.get("method", "GET")).upper()
    if method not in _METHODS:
        raise ApiToolConfigError(f"method deve ser um de {sorted(_METHODS)}")

    url = config.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ApiToolConfigError("url deve ser http(s)://...")
    # Falhar aqui evita descobrir só na primeira chamada do modelo que o destino
    # é interno. Quando o host é um placeholder, quem cobra é a checagem da chamada.
    try:
        check_url_template(url)
    except EgressBlockedError as exc:
        raise ApiToolConfigError(str(exc)) from exc

    # Cópia: os compostos são normalizados no lugar, e mexer no dict de quem
    # chamou seria efeito colateral escondido.
    parameters = copy.deepcopy(config.get("parameters", []))
    if not isinstance(parameters, list):
        raise ApiToolConfigError("parameters deve ser uma lista")
    seen_names: set[str] = set()
    path_names: set[str] = set()
    for p in parameters:
        name = p.get("name")
        if not _valid_param_name(name, p):
            raise ApiToolConfigError(
                f"nome de parâmetro inválido: {name!r} (use letras, números e _; um header que não vem do "
                "modelo — source dependency ou const — também aceita '-', como Idempotency-Key)"
            )
        if name in seen_names:
            raise ApiToolConfigError(f"parâmetro duplicado: {name!r}")
        seen_names.add(name)
        if p.get("type") not in _JSON_SCHEMA_TYPE:
            raise ApiToolConfigError(f"tipo inválido em {name!r}: {p.get('type')!r}")
        location = p.get("location", "query")
        if location not in ("query", "path", "header", "body"):
            raise ApiToolConfigError(f"location inválido em {name!r}: {location!r}")
        if location == "path":
            path_names.add(name)

        source = p.get("source", "model")
        if source not in _SOURCES:
            raise ApiToolConfigError(f"source inválido em {name!r}: {source!r} (use {sorted(_SOURCES)})")
        if source == "dependency":
            dependency = p.get("dependency")
            if not isinstance(dependency, str) or not dependency.isidentifier():
                raise ApiToolConfigError(f"{name!r}: source='dependency' exige `dependency` com o nome do campo")
        elif source == "const" and "value" not in p:
            raise ApiToolConfigError(f"{name!r}: source='const' exige `value`")
        if location == "header" and name.lower() == "authorization":
            raise ApiToolConfigError(f"{name!r}: um parâmetro não pode ser o header Authorization (use auth)")

        # Só o que o modelo preenche entra no schema da tool (`_parameters_schema`),
        # e é só aí que um composto sem sub-schema vira uma declaração que o
        # provedor recusa. Em `dependency`/`const` o tipo é só o do valor.
        if source == "model" and p["type"] in ("object", "array"):
            try:
                p.update(validate_composite(p, path=f"parâmetro {name}"))
            except FieldSchemaError as exc:
                raise ApiToolConfigError(str(exc)) from exc

    for placeholder in _format_placeholders(url):
        if placeholder not in path_names:
            raise ApiToolConfigError(
                f"a url referencia {{{placeholder}}}, mas não há parâmetro location=path com esse nome"
            )

    auth = config.get("auth") or {"type": "none"}
    auth_type = auth.get("type", "none")
    if auth_type not in ("none", "bearer", "api_key", "basic"):
        raise ApiToolConfigError(f"auth.type inválido: {auth_type!r}")
    if auth_type == "api_key" and isinstance(auth.get("header"), str):
        header_params = {str(p["name"]).lower() for p in parameters if p.get("location") == "header"}
        if auth["header"].lower() in header_params:
            raise ApiToolConfigError(f"um parâmetro não pode usar o header de autenticação {auth['header']!r}")
    if auth_type == "bearer" and not auth.get("token"):
        raise ApiToolConfigError("auth.token é obrigatório para auth.type='bearer'")
    if auth_type == "api_key" and not (auth.get("header") and auth.get("value")):
        raise ApiToolConfigError("auth.header e auth.value são obrigatórios para auth.type='api_key'")
    if auth_type == "basic" and not (auth.get("username") and auth.get("password")):
        raise ApiToolConfigError("auth.username e auth.password são obrigatórios para auth.type='basic'")

    timeout = config.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or not (0 < timeout <= _MAX_TIMEOUT_SECONDS):
        raise ApiToolConfigError(f"timeout_seconds deve ser > 0 e <= {_MAX_TIMEOUT_SECONDS}")

    headers = config.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ApiToolConfigError("headers deve ser um objeto string -> string")

    return {
        "method": method,
        "url": url,
        "parameters": parameters,
        "headers": headers,
        "auth": auth,
        "timeout_seconds": float(timeout),
    }


def _format_placeholders(url: str) -> set[str]:
    import string

    return {name for _, name, _, _ in string.Formatter().parse(url) if name}


_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _valid_param_name(name: Any, param: dict[str, Any]) -> bool:
    """Identificador, como sempre. A exceção é um header que o modelo não preenche:
    headers HTTP de verdade têm hífen (`Idempotency-Key`, `X-Request-Id`), e sem
    isto não havia como mandar a `idempotency_key` de uma etapa `action` no header
    que o sistema de destino espera. O que o modelo preenche continua identificador:
    o nome vira propriedade do schema da tool, e o provedor é exigente com isso."""
    if not isinstance(name, str):
        return False
    if name.isidentifier():
        return True
    return (
        param.get("location") == "header"
        and param.get("source", "model") in ("dependency", "const")
        and bool(_HEADER_NAME.match(name))
    )


def _parameters_schema(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    """Só os parâmetros `source="model"`: o que vem de dependency/const não é
    assunto do modelo e fica fora do schema, para ele não tentar preencher.

    A montagem em si é do `field_schema`, o mesmo que serve o `response_schema`
    de um agente analista — assim um `array` declarado a uma tool sai com `items`
    e um `object` com `properties`, em vez do tipo pelado que o provedor recusa."""
    return object_schema([p for p in parameters if p.get("source", "model") == "model"])


def required_dependencies(config: dict[str, Any]) -> list[str]:
    """Campos de `dependencies` que esta tool exige — o agente que a usa precisa
    declará-los em `dependency_fields` (validado em `api/agents_routes.py`)."""
    return sorted(
        {
            p["dependency"]
            for p in config.get("parameters") or []
            if p.get("source") == "dependency"
            and p.get("required")
            and isinstance(p.get("dependency"), str)
            # Preenchidos pelo servidor (o dry_run em todo run; a chave de idempotência no
            # procedural e quando quem chama manda `Idempotency-Key`) — não é coisa de declarar.
            and p["dependency"] not in (DRY_RUN_DEPENDENCY, IDEMPOTENCY_DEPENDENCY)
        }
    )


def _resolve_arguments(
    parameters: list[dict[str, Any]], model_arguments: dict[str, Any], dependencies: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Junta o que o modelo preencheu com o que vem de dependency/const e cobra os
    obrigatórios. Devolve (argumentos, erro); `erro` preenchido vira a resposta da tool."""
    resolved: dict[str, Any] = {}
    missing_model: list[str] = []
    missing_deps: list[str] = []
    for p in parameters:
        name = p["name"]
        source = p.get("source", "model")
        if source == "const":
            value = p.get("value")
        elif source == "dependency":
            value = dependencies.get(p["dependency"])
            if value is None and p.get("required"):
                missing_deps.append(p["dependency"])
        else:
            value = model_arguments.get(name)
            if value is None and p.get("required"):
                missing_model.append(name)
        if value is not None:
            resolved[name] = value

    if missing_deps:
        campos = ", ".join(f"dependencies.{d}" for d in missing_deps)
        return resolved, (
            f"Erro de configuração: esta tool precisa de {campos}, que não veio na requisição do "
            "/chat. Avise que o dado não está disponível em vez de inventá-lo."
        )
    if missing_model:
        return resolved, (
            f"Erro: faltam parâmetros obrigatórios: {', '.join(missing_model)}. "
            "Chame a tool de novo preenchendo-os, ou peça os valores ao usuário."
        )
    return resolved, None


def _apply_auth(headers: dict[str, str], auth: dict[str, Any]) -> None:
    auth_type = auth.get("type", "none")
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {auth['token']}"
    elif auth_type == "api_key":
        headers[auth["header"]] = auth["value"]
    elif auth_type == "basic":
        pass  # aplicado via httpx.BasicAuth na chamada, não num header manual


async def _call_api(config: dict[str, Any], model_arguments: dict[str, Any], tool_name: str = "") -> str:
    def failed(output: str, kind: failures.FailureKind, http_status: int | None = None) -> str:
        # O texto vai para o modelo como sempre; o registro é o que o trace lê
        # para marcar a span como falha (ver `tools/failures.py`).
        failures.record_failure(tool_name, output, kind, http_status)
        return output

    # O flag do servidor vence um `dry_run` que viesse nas dependencies; a chave também.
    dependencies = {**get_dependencies(), DRY_RUN_DEPENDENCY: is_dry_run()}
    if key := get_idempotency_key():
        dependencies[IDEMPOTENCY_DEPENDENCY] = key
    arguments, error = _resolve_arguments(config["parameters"], model_arguments, dependencies)
    if error is not None:
        return failed(error, "config" if error.startswith("Erro de configuração") else "invalid_arguments")

    method = config["method"]
    url = config["url"]
    headers = dict(config.get("headers") or {})
    query: dict[str, Any] = {}
    path_values: dict[str, Any] = {}
    body: dict[str, Any] = {}

    for p in config["parameters"]:
        name = p["name"]
        if name not in arguments:
            continue
        value = arguments[name]
        location = p.get("location", "query")
        if location == "query":
            query[name] = value
        elif location == "path":
            path_values[name] = value
        elif location == "header":
            headers[str(name)] = str(value)
        elif location == "body":
            body[name] = value

    try:
        url = url.format(**path_values)
    except KeyError as exc:
        return failed(f"Erro ao montar a URL: parâmetro de path ausente {exc}", "invalid_arguments")

    # Com a URL já montada: um parâmetro de path pode ter composto o host.
    try:
        await acheck_url(url)
    except EgressBlockedError as exc:
        return failed(f"Chamada recusada: {exc}", "config")

    # Um header da config ou de parâmetro com outra caixa (`x-kuro-dry-run: false`)
    # iria junto com o nosso; quem decide é o flag da requisição.
    headers = {k: v for k, v in headers.items() if k.lower() != DRY_RUN_HEADER.lower()}
    if is_dry_run():
        headers[DRY_RUN_HEADER] = "true"

    auth = config.get("auth") or {"type": "none"}
    _apply_auth(headers, auth)
    basic_auth = httpx.BasicAuth(auth["username"], auth["password"]) if auth.get("type") == "basic" else None

    try:
        response = await get_client().request(
            method,
            url,
            params=query or None,
            json=body or None,
            headers=headers or None,
            auth=basic_auth,
            timeout=config.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS),
        )
    except httpx.HTTPError as exc:
        # Timeout e afins chegam com a mensagem vazia: sem o tipo, o log dizia
        # só "Falha ao chamar a API: " e ninguém sabia o que tinha acontecido.
        return failed(f"Falha ao chamar a API: {type(exc).__name__}: {exc}".rstrip(": "), "unavailable")

    text = response.text
    try:
        import json

        text = json.dumps(response.json(), ensure_ascii=False)
    except ValueError:
        pass
    if len(text) > _MAX_RESPONSE_CHARS:
        text = text[:_MAX_RESPONSE_CHARS] + f"... (truncado, {len(response.text)} chars no total)"
    output = f"HTTP {response.status_code}: {text}"
    kind = failures.classify_http(response.status_code)
    return failed(output, kind, response.status_code) if kind else output


def build_api_function(*, tool_name: str, description: str | None, config: dict[str, Any]) -> Function:
    validated = validate_api_config(config)

    async def entrypoint(**arguments: Any) -> str:
        return await _call_api(validated, arguments, tool_name)

    return Function(
        name=tool_name,
        description=description or f"Chama {validated['method']} {validated['url']}",
        parameters=_parameters_schema(validated["parameters"]),
        entrypoint=entrypoint,
        skip_entrypoint_processing=True,
    )
