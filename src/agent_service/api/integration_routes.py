"""Contrato de integração de um agente (`GET /agents/{tipo}/integration`).

Quem vai chamar o agente de outro módulo precisa saber três coisas: qual
endpoint, o que mandar no corpo e quais `dependencies` são obrigatórias. Isso
é derivado, não escrito à mão: os campos declarados pelo agente e os campos
que as tools dele exigem (parâmetros `source="dependency"`) são a mesma
verdade — `agents_routes` só deixa salvar quando batem.

O console, a CLI e o MCP consomem este endpoint, em vez de cada um montar o seu
exemplo de cURL e divergirem no primeiro campo novo.

**A URL dos exemplos é a pública**, decidida aqui: sem `base_url`, vale a mesma ordem do
`kuro mcp-config` (`KURO_PUBLIC_URL` → `KURO_API_DOMAIN` → profile `ip` → o host). Pedido de
dentro do container (o MCP, a CLI no `docker compose exec`), o cliente só conhece
`http://localhost:8000`, que não serve para quem integra.
"""

import copy
import json
import os
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from agent_service.agents.procedural import fields_before
from agent_service.agents.store import get_definition
from agent_service.field_schema import object_schema
from agent_service.public_url import public_url
from agent_service.tools.api_tool import required_dependencies
from agent_service.tools.store import get_tool

router = APIRouter(prefix="/agents", tags=["agents"])

_EXAMPLE_BY_TYPE: dict[str, Any] = {"string": "texto", "integer": 42, "number": 1.5, "boolean": True}


class DependencyContract(BaseModel):
    name: str
    type: str
    required: bool
    label: str
    description: str
    example: Any
    required_by_tools: list[str] = []
    """Tools do agente que consomem este campo — elas falham sem ele."""


class RequestField(BaseModel):
    name: str
    type: str
    required: bool
    description: str


class StreamEvent(BaseModel):
    event: str
    data: str
    """A forma do `data` (JSON) desse evento."""


class ErrorContract(BaseModel):
    status: str
    meaning: str
    action: str


class IntegrationContract(BaseModel):
    agent_type: str
    name: str
    kind: str = "conversational"
    prompt_version: int
    agent_version: int | None = None
    """A versão da configuração inteira que responde hoje — a mesma que cada resposta traz."""
    base_url: str
    """O endereço usado nos exemplos: o `base_url` pedido, ou o público que o servidor conhece."""
    base_url_source: str
    """De onde saiu o `base_url`: `explicit` (pedido ou `KURO_PUBLIC_URL`), `domain`
    (`KURO_API_DOMAIN`), `ip` (profile `ip`) ou `local` (o serviço só escuta no host)."""
    openapi_url: str
    """OpenAPI só das rotas que a chave `runtime` alcança — abre com ela, diferente do `/docs`."""
    agent_openapi_url: str
    """O OpenAPI só deste agente, com `dependencies` e a resposta tipadas — para gerar o cliente."""
    endpoint: str
    """Rota que quem integra chama: `/chat` num agente conversacional,
    `/analyze` num analista — são contratos de corpo diferentes."""
    chat_url: str
    stream_url: str | None = None
    """`None` em `kind="analysis"`: analista é one-shot, não tem streaming."""
    response_schema: list[dict[str, Any]] = []
    """Só em `kind="analysis"`: a forma do `result` que volta."""
    stages: list[dict[str, Any]] = []
    """Só em `kind="procedural"`: as etapas. A resposta do `/chat` traz `state` com a
    etapa atual, o que falta e, quando `done`, o `result`."""
    state_url: str | None = None
    """Só em `kind="procedural"`: onde ler o estado de uma conversa (troque `{session_id}`)."""
    dependencies: list[DependencyContract]
    request_fields: list[RequestField]
    """Os campos do corpo do `endpoint`: os de `request_example` e os opcionais."""
    request_example: dict[str, Any]
    response_example: dict[str, Any]
    stream_events: list[StreamEvent] = []
    """Eventos do `stream_url`, na ordem em que podem chegar. Vazio em analista."""
    errors: list[ErrorContract]
    curl: str
    warnings: list[str] = []
    """Deve vir vazio: uma tool exigindo campo não declarado é inconsistência de dados."""


def _example_for(field: dict[str, Any]) -> Any:
    if field.get("default") is not None:
        return field["default"]
    if field.get("enum"):
        return field["enum"][0]
    tipo = field.get("type", "string")
    if tipo == "object":
        return {f["name"]: _example_for(f) for f in field.get("fields") or []}
    if tipo == "array":
        return [_example_for(field["items"])] if field.get("items") else []
    return _EXAMPLE_BY_TYPE.get(tipo, "texto")


_ATTACHMENTS = (
    "Anexos direto ao modelo: [{content_base64 | url, mime_type, filename}] (imagem, áudio, PDF...). "
    "O modelo do agente precisa suportar o tipo; limite MAX_ATTACHMENT_MB."
)
_METADATA = (
    'Correlação com o seu sistema (até 20 pares, ex.: {"conversation_id": "98231"}). Não vai ao modelo; '
    "filtra os runs (kuro runs list --meta)."
)
_DRY_RUN = (
    "Teste: as tools recebem X-Kuro-Dry-Run: true e dependencies.dry_run e decidem o que simular. "
    "O run fica marcado como teste."
)
_DEPENDENCIES = "Dados que você já tem (cpf, plano...): viram contexto do prompt e alimentam as tools."

CHAT_FIELDS = [
    RequestField(name="agent_type", type="string", required=True, description="Slug do agente."),
    RequestField(name="user_id", type="string", required=True,
                 description="Identificador estável do usuário final: base do histórico e das memórias."),
    RequestField(name="session_id", type="string", required=True,
                 description="A conversa. Repita entre mensagens para manter o histórico; outro valor começa do zero."),
    RequestField(name="message", type="string", required=True, description="O texto do usuário."),
    RequestField(name="dependencies", type="object", required=False, description=_DEPENDENCIES),
    RequestField(name="metadata", type="object", required=False, description=_METADATA),
    RequestField(name="dry_run", type="boolean", required=False, description=_DRY_RUN),
    RequestField(name="attachments", type="array", required=False, description=_ATTACHMENTS),
]

ANALYZE_FIELDS = [
    RequestField(name="agent_type", type="string", required=True, description="Slug do agente analista."),
    RequestField(name="document", type="string", required=True,
                 description="O texto a analisar (até MAX_INPUT_CHARS caracteres)."),
    RequestField(name="dependencies", type="object", required=False, description=_DEPENDENCIES),
    RequestField(name="session_id", type="string", required=False,
                 description="Agrupa os runs por conversa (ex.: o id da conversa no seu sistema). Não cria histórico."),
    RequestField(name="user_id", type="string", required=False, description="Quem está por trás do documento."),
    RequestField(name="metadata", type="object", required=False, description=_METADATA),
    RequestField(name="dry_run", type="boolean", required=False, description=_DRY_RUN),
    RequestField(name="attachments", type="array", required=False, description=_ATTACHMENTS),
]

STREAM_EVENTS = [
    StreamEvent(event="run", data="{run_id, trace_id, agent_version, config_hash}: antes de tudo"),
    StreamEvent(event="message", data="{content}: um trecho do texto, a cada pedaço"),
    StreamEvent(event="usage", data="métricas do run (tokens), ao terminar"),
    StreamEvent(event="state", data="só em procedural: o mesmo state do /chat, antes do done"),
    StreamEvent(event="sources", data="{sources}: documentos consultados, antes do done, se o run não falhou"),
    StreamEvent(event="error", data="{message, status?}: o status HTTP que o /chat daria (409, 503, 504...)"),
    StreamEvent(event="done", data="{}: fim do stream, sempre"),
]

ERRORS = [
    ErrorContract(status="401 / 403", meaning="Chave ausente ou de escopo errado",
                  action="Erro de configuração: alertar, não repetir"),
    ErrorContract(status="404", meaning="agent_type não existe", action="Erro de configuração: alertar, não repetir"),
    ErrorContract(status="409",
                  meaning="Procedural: outra mensagem da mesma sessão ainda em andamento; ou a mesma "
                  "Idempotency-Key ainda em andamento",
                  action="Esperar a resposta dela e reenviar (com a mesma Idempotency-Key)"),
    ErrorContract(status="422", meaning="Requisição inválida: dependency faltando ou de tipo errado, texto grande demais",
                  action="Bug de quem chama: não repetir, alertar"),
    ErrorContract(status="502", meaning="Falha do provedor de modelo ou saída fora do schema",
                  action="Fallback; pode tentar de novo depois"),
    ErrorContract(status="503 com Retry-After", meaning="Sem vaga de execução (fila cheia) ou banco fora",
                  action="Fallback agora, repetir depois do Retry-After"),
    ErrorContract(status="503 com error",
                  meaning="Configuração do Kuro: model_provider_not_configured ou encryption_not_configured",
                  action="Fallback e alertar quem opera o Kuro: repetir não resolve"),
    ErrorContract(status="504", meaning="A execução passou do timeout_seconds do agente",
                  action="Fallback; o run fica registrado com o motivo"),
]

_EXAMPLE_SOURCE = {
    "title": "Manual do produto", "document": "manual.pdf", "source_url": None,
    "content_id": "c0ffee", "collection": "manuais", "score": 0.82, "chunk_ids": ["c0ffee_3"],
}


def _response_example(
    agent_type: str, kind: str, agent_version: int | None, response_schema: list[dict[str, Any]]
) -> dict[str, Any]:
    """A forma da resposta, com os campos que a doc manda guardar (`run_id`, `agent_version`,
    `config_hash`). Os valores são ilustrativos."""
    comum = {"run_id": "run_8f2c1d", "trace_id": "4bf92f35", "agent_version": agent_version,
             "config_hash": "a1b2c3d4e5f6"}
    if kind == "analysis":
        result = {f["name"]: _example_for(f) for f in response_schema}
        return {"agent_type": agent_type, "result": result, **comum, "sources": []}
    resposta: dict[str, Any] = {
        "agent_type": agent_type, "session_id": "sessao-123", "content": "Olá! Como posso ajudar?", **comum,
        "sources": [] if kind == "procedural" else [_EXAMPLE_SOURCE],
    }
    if kind == "procedural":
        resposta["state"] = {"stage": "…", "collected": {}, "missing": ["…"], "invalid": {}, "done": False,
                             "result": None}
    return resposta


@router.get("/{agent_type}/integration", response_model=IntegrationContract)
def get_integration_contract(
    agent_type: str,
    base_url: str | None = Query(
        None,
        description="Endereço do serviço usado nos exemplos. Sem ele, o público que o servidor conhece "
        "(KURO_PUBLIC_URL, KURO_API_DOMAIN, profile ip).",
    ),
) -> IntegrationContract:
    definition = get_definition(agent_type)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")

    # De quais campos cada tool do agente depende (parâmetros source="dependency").
    por_tool: dict[str, list[str]] = {}
    for tool_name in definition["tools"] or []:
        row = get_tool(tool_name)
        if row is not None and row["kind"] == "api":
            for campo in required_dependencies(row["config"] or {}):
                por_tool.setdefault(campo, []).append(tool_name)
    # As tools das etapas `action` também dependem de dependencies — menos do que
    # o próprio procedimento coleta e da chave de idempotência, que o servidor põe.
    stages = definition.get("stages") or []
    for index, stage in enumerate(stages):
        if stage.get("type") != "action":
            continue
        row = get_tool(stage["tool"])
        if row is None or row["kind"] != "api":
            continue
        fornecidos = set(fields_before(stages, index)) | {"idempotency_key"}
        for campo in required_dependencies(row["config"] or {}):
            if campo not in fornecidos:
                por_tool.setdefault(campo, []).append(stage["tool"])

    declarados = {f["name"] for f in definition["dependency_fields"] or []}
    warnings = [
        f"a tool {', '.join(tools)} exige dependencies.{campo}, que o agente não declara"
        for campo, tools in sorted(por_tool.items())
        if campo not in declarados
    ]

    dependencies = [
        DependencyContract(
            name=f["name"],
            type=f["type"],
            required=f["required"],
            label=f["label"],
            description=f["description"],
            example=_example_for(f),
            required_by_tools=sorted(por_tool.get(f["name"], [])),
        )
        for f in definition["dependency_fields"] or []
    ]

    exemplo_dependencies = {d.name: d.example for d in dependencies if d.required or d.example is not None}
    # Um analista não recebe `message` nem sessão: o corpo dele é `document`, e a
    # resposta é o objeto do `response_schema`. Um exemplo de `/chat` aqui manda
    # quem integra montar a chamada errada.
    kind = definition.get("kind") or "conversational"
    is_analysis = kind == "analysis"
    if is_analysis:
        request_example = {"agent_type": agent_type, "document": "Texto a analisar."}
    else:
        request_example = {
            "agent_type": agent_type,
            "user_id": "usuario-123",
            "session_id": "sessao-123",
            "message": "Olá!",
        }
    if exemplo_dependencies:
        request_example["dependencies"] = exemplo_dependencies
    # Correlação e teste no exemplo porque é o que a doc manda usar desde o primeiro
    # dia: o `metadata` liga o run ao seu sistema, o `dry_run` evita efeito real.
    request_example["metadata"] = {"conversation_id": "98231"}
    request_example["dry_run"] = False

    base, base_source = public_url(base_url or os.environ.get("KURO_PUBLIC_URL"))
    endpoint = "/analyze" if is_analysis else "/chat"
    corpo = json.dumps(request_example, ensure_ascii=False, indent=2)
    # Com auth ligada o exemplo precisa do header: um cURL que não funciona é
    # pior que exemplo nenhum, porque manda quem integra procurar no lugar errado.
    from agent_service.api.auth import auth_enabled

    # Aspas duplas: dentro de aspas simples o shell não expande a variável, e o
    # cURL sairia com o literal `$KURO_RUNTIME_API_KEY` no header — 401 para
    # quem copiasse e colasse, que é o oposto do que este endpoint existe para fazer.
    auth_header = '  -H "Authorization: Bearer $KURO_RUNTIME_API_KEY" \\\n' if auth_enabled() else ""
    curl = (
        f"curl -X POST {base}{endpoint} \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"{auth_header}"
        f"  -d '{corpo}'"
    )

    agent_version = definition.get("agent_version")
    return IntegrationContract(
        agent_type=agent_type,
        name=definition["name"],
        kind=kind,
        prompt_version=definition["prompt_version"],
        agent_version=agent_version,
        base_url=base,
        base_url_source=base_source,
        openapi_url=f"{base}/integration/openapi.json",
        agent_openapi_url=f"{base}/agents/{agent_type}/openapi.json",
        endpoint=endpoint,
        chat_url=f"{base}{endpoint}",
        stream_url=None if is_analysis else f"{base}/chat/stream",
        response_schema=definition.get("response_schema") or [],
        stages=stages,
        state_url=f"{base}/agents/{agent_type}/procedures/{{session_id}}" if kind == "procedural" else None,
        dependencies=dependencies,
        request_fields=ANALYZE_FIELDS if is_analysis else CHAT_FIELDS,
        request_example=request_example,
        response_example=_response_example(agent_type, kind, agent_version, definition.get("response_schema") or []),
        stream_events=[] if is_analysis else STREAM_EVENTS,
        errors=ERRORS,
        curl=curl,
        warnings=warnings,
    )


openapi_router = APIRouter(tags=["integration"])

RUNTIME_OPENAPI_PATH = "/integration/openapi.json"


def _refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)


def runtime_openapi(spec: dict[str, Any]) -> dict[str, Any]:
    """O OpenAPI do serviço só com as rotas que a chave `runtime` alcança (e as abertas), e só
    com os schemas que elas usam. É o mapa de quem integra — sem entregar as rotas de admin,
    que é o motivo de o `/openapi.json` inteiro exigir a chave admin."""
    from agent_service.api.auth import required_scope

    paths = {
        path: item
        for path, item in (spec.get("paths") or {}).items()
        if path != RUNTIME_OPENAPI_PATH and required_scope(path) != "admin"
    }
    schemas = (spec.get("components") or {}).get("schemas") or {}
    keep: set[str] = set()
    _refs(paths, keep)
    pending = list(keep)
    while pending:  # um schema pode citar outro
        found: set[str] = set()
        _refs(schemas.get(pending.pop()), found)
        pending.extend(found - keep)
        keep |= found
    components = {**(spec.get("components") or {}), "schemas": {k: v for k, v in schemas.items() if k in keep}}
    return {**spec, "paths": paths, "components": components}


@openapi_router.get(RUNTIME_OPENAPI_PATH, include_in_schema=False)
def get_runtime_openapi(request: Request) -> dict[str, Any]:
    return runtime_openapi(request.app.openapi())


_STATE_PATH = "/agents/{agent_type}/procedures/{session_id}"
_REQUEST_SCHEMA = {"/chat": "ChatRequest", "/chat/stream": "ChatRequest", "/analyze": "AnalyzeRequest"}
_IDEMPOTENCY_409 = (
    "Procedural: outra mensagem da mesma sessão em andamento; ou a mesma Idempotency-Key ainda em andamento. "
    "Espere e reenvie."
)


def _schema_name(agent_type: str, suffix: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", agent_type) if p) + suffix


def _retarget(node: Any, renames: dict[str, str]) -> None:
    """Troca os `$ref` dos schemas genéricos pelos deste agente."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.rsplit("/", 1)[-1] in renames:
            node["$ref"] = "#/components/schemas/" + renames[ref.rsplit("/", 1)[-1]]
        for value in node.values():
            _retarget(value, renames)
    elif isinstance(node, list):
        for value in node:
            _retarget(value, renames)


def agent_openapi(spec: dict[str, Any], definition: dict[str, Any], base_url: str, auth: bool) -> dict[str, Any]:
    """O OpenAPI de quem integra um agente só: as rotas que ele chama, com `agent_type` fixo,
    `dependencies` tipadas (com `enum`) e, num analista, o `result` no formato do
    `response_schema`. Sai do OpenAPI de runtime (`runtime_openapi`), não de schemas escritos à
    mão: um campo novo no `/chat` aparece aqui sozinho. Nada de instructions, tools nem modelo."""
    agent_type = definition["agent_type"]
    kind = definition.get("kind") or "conversational"
    runtime = copy.deepcopy(runtime_openapi(spec))
    wanted = {"/analyze"} if kind == "analysis" else {"/chat", "/chat/stream"}
    wanted |= {"/observability/scores", "/observability/references"}
    if kind == "procedural":
        wanted.add(_STATE_PATH)
    paths = {p: item for p, item in runtime["paths"].items() if p in wanted}
    schemas = runtime.get("components", {}).get("schemas", {})

    # Schemas próprios deste agente, a partir dos genéricos: `agent_type` fixo e dependencies tipadas.
    renames: dict[str, str] = {}
    fields = definition.get("dependency_fields") or []
    for generic in sorted({_REQUEST_SCHEMA[p] for p in paths if p in _REQUEST_SCHEMA}):
        if generic not in schemas:
            continue
        schema = copy.deepcopy(schemas[generic])
        props = schema.setdefault("properties", {})
        # `enum` de um valor só, e não `const`: os geradores de cliente (o de PHP, por exemplo) leem melhor.
        props["agent_type"] = {"type": "string", "enum": [agent_type], "default": agent_type, "title": "Agent Type"}
        if fields:
            deps = object_schema(fields)
            deps["description"] = "Os dados que você já tem; viram contexto do prompt e alimentam as tools."
            props["dependencies"] = deps
            if any(f.get("required") for f in fields):
                schema["required"] = sorted(set(schema.get("required") or []) | {"dependencies"})
        name = _schema_name(agent_type, generic)
        schemas[name] = schema
        renames[generic] = name
    if kind == "analysis" and "AnalyzeResponse" in schemas:
        response = copy.deepcopy(schemas["AnalyzeResponse"])
        result = object_schema(definition.get("response_schema") or [])
        result["description"] = "A decisão do agente, validada contra o response_schema dele."
        response.setdefault("properties", {})["result"] = result
        name = _schema_name(agent_type, "AnalyzeResponse")
        schemas[name] = response
        renames["AnalyzeResponse"] = name
    _retarget(paths, renames)

    for path, item in paths.items():
        for operation in item.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            if path in ("/chat", "/analyze"):
                operation["responses"].setdefault("409", {"description": _IDEMPOTENCY_409})
                operation["responses"].setdefault("504", {"description": "Passou do timeout_seconds do agente."})
    if _STATE_PATH in paths:
        for operation in paths[_STATE_PATH].values():
            for param in operation.get("parameters") or []:
                if param.get("name") == "agent_type":
                    param["schema"] = {"type": "string", "enum": [agent_type]}

    keep: set[str] = set()
    _refs(paths, keep)
    pending = list(keep)
    while pending:
        found: set[str] = set()
        _refs(schemas.get(pending.pop()), found)
        pending.extend(found - keep)
        keep |= found
    components: dict[str, Any] = {"schemas": {k: v for k, v in schemas.items() if k in keep}}
    document: dict[str, Any] = {
        "openapi": runtime.get("openapi", "3.1.0"),
        "info": {
            "title": f"Kuro — {definition['name']} ({agent_type})",
            "version": str(definition.get("agent_version") or definition.get("prompt_version") or "1"),
            "description": f"Contrato do agente {agent_type!r} (kind={kind}). Guia: docs/integracao.md.",
        },
        "servers": [{"url": base_url}],
        "paths": paths,
        "components": components,
    }
    if auth:
        components["securitySchemes"] = {"runtime": {"type": "http", "scheme": "bearer"}}
        document["security"] = [{"runtime": []}]
    return document


@openapi_router.get("/agents/{agent_type}/openapi.json", include_in_schema=False)
def get_agent_openapi(agent_type: str, request: Request) -> dict[str, Any]:
    """Aceita a chave `runtime` (`api/auth.py`): é o documento de quem integra este agente."""
    from agent_service.api.auth import auth_enabled

    definition = get_definition(agent_type)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")
    base, _ = public_url(os.environ.get("KURO_PUBLIC_URL"))
    return agent_openapi(request.app.openapi(), definition, base, auth_enabled())
