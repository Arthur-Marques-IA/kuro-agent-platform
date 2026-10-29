"""CRUD de definições de agente + histórico de prompt (read-only).

Separado do contrato de chat (`api/routes.py`) porque é uma superfície
diferente: isso é gerenciamento (usado pela tela `/agents` do frontend e por
integrações administrativas), não o contrato estável de execução que outros
módulos da plataforma consomem. Tools têm CRUD próprio em `tools_routes.py`
(`GET /tools` etc.) — aqui só se valida que os nomes em `tools` existem.
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from agent_service.agents.dependency_fields import (
    DependencyFieldSpecError,
    FieldType,
    validate_field_specs,
)
from agent_service.agents.feedback import FeedbackMergeError, merge_feedback
from agent_service.agents.procedural import StageSpecError, fields_before, state_view, validate_stages
from agent_service.agents.procedure_store import funnel as procedure_funnel
from agent_service.agents.procedure_store import delete_procedures_of, get_procedure
from agent_service.agents.response_model import ResponseSchemaError, validate_response_schema
from agent_service.agents.registry import get_agent_with_definition
from agent_service.agents.versions import effective_config, ensure_version, get_version, list_versions
from agent_service.agents.store import (
    DefinitionNotFoundError,
    clear_feedback_note,
    create_definition,
    delete_definition,
    get_definition,
    get_feedback_note,
    get_feedback_version,
    list_definitions,
    list_feedback_versions,
    list_prompt_versions,
    save_feedback_note,
    update_definition,
)
from agent_service.documents.collections import collection_exists
from agent_service.models.params import ModelParamsError, validate_model_params
from agent_service.tools.api_tool import required_dependencies
from agent_service.tools.registry import ToolBuildError, UnknownToolError, tool_exists
from agent_service.tools.store import get_tool

AgentKind = Literal["conversational", "analysis", "procedural"]

router = APIRouter(prefix="/agents", tags=["agents"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class DependencyFieldIn(BaseModel):
    name: str
    type: FieldType = "string"
    label: str | None = None
    description: str | None = None
    required: bool = False
    default: Any = None
    enum: list[Any] | None = None
    """Valores permitidos (não vale para boolean)."""


class DependencyFieldOut(BaseModel):
    name: str
    type: FieldType
    label: str
    description: str
    required: bool
    default: Any
    enum: list[Any] | None = None


ResponseFieldType = Literal["string", "integer", "number", "boolean", "object", "array"]
ItemType = Literal["string", "integer", "number", "boolean", "object"]


class ResponseItemIn(BaseModel):
    """O elemento de um `array`. `fields` é obrigatório quando `type="object"`."""

    type: ItemType = "string"
    fields: "list[ResponseFieldIn] | None" = None
    enum: list[Any] | None = None


class ResponseFieldIn(BaseModel):
    """Campo de `response_schema`. Folha tem a mesma forma de `dependency_fields`;
    `object` pede `fields` e `array` pede `items` (ver agents/response_model.py)."""

    name: str
    type: ResponseFieldType = "string"
    label: str | None = None
    description: str | None = None
    required: bool = False
    default: Any = None
    enum: list[Any] | None = None
    """Valores permitidos: o modelo só pode devolver um deles (ex.: `acao`)."""
    fields: "list[ResponseFieldIn] | None" = None
    items: ResponseItemIn | None = None


class ResponseItemOut(BaseModel):
    type: ItemType
    fields: "list[ResponseFieldOut] | None" = None
    enum: list[Any] | None = None


class ResponseFieldOut(BaseModel):
    name: str
    type: ResponseFieldType
    label: str
    description: str
    required: bool
    default: Any = None
    enum: list[Any] | None = None
    fields: "list[ResponseFieldOut] | None" = None
    items: ResponseItemOut | None = None


for _model in (ResponseItemIn, ResponseFieldIn, ResponseItemOut, ResponseFieldOut):
    _model.model_rebuild()


class AgentDefinitionIn(BaseModel):
    agent_type: str = Field(..., description="Slug estável, usado como agent_id no /chat")
    name: str
    instructions: list[str] = Field(..., min_length=1)
    tools: list[str] = []
    model_provider: str | None = None
    model_id: str | None = None
    model_credential_id: str | None = None
    knowledge_collection: str | None = Field(
        default=None, description="Collection de documentos que o agente pode consultar (GET /collections)"
    )
    dependency_fields: list[DependencyFieldIn] = []
    memory_backend: Literal["none", "auto", "agentic", "common", "mem0"] = "none"
    """Memória de longo prazo: `none` (padrão), `auto` (extraída em paralelo, modelo
    auxiliar), `agentic` (o modelo decide quando gravar; `common` é o nome antigo)
    ou `mem0`."""
    session_summary: bool = False
    """Resume o que sai da janela de histórico, em vez de perdê-lo."""
    plain_text: bool = False
    """Responde em texto puro (sem markdown) — canais como WhatsApp."""
    num_history_runs: int = 10
    kind: AgentKind = "conversational"
    response_schema: list[ResponseFieldIn] = Field(
        default=[],
        description="Só para kind='analysis': campos da saída estruturada. Folhas iguais a "
        "dependency_fields, mais 'object' (com fields) e 'array' (com items).",
    )
    stages: list[dict[str, Any]] = Field(
        default=[],
        description="Só para kind='procedural': as etapas do fluxo — collect (fields), confirm e "
        "action (tool). Ver agents/procedural.py.",
    )
    model_params: dict[str, Any] | None = Field(
        default=None,
        description="temperature, top_p, max_tokens, reasoning (off|low|medium|high), thinking_budget (Gemini), "
        "prompt_cache (off|5m|1h, só anthropic). Vazio = padrão do provedor.",
    )
    timeout_seconds: int | None = Field(
        default=None, ge=1, le=600, description="Tempo limite de uma execução; vazio = RUN_TIMEOUT_SECONDS."
    )

    @field_validator("agent_type")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("agent_type deve ser um slug: letras minúsculas, números, '-' ou '_'")
        return v

    @field_validator("dependency_fields")
    @classmethod
    def _validate_dependency_fields(cls, v: list[DependencyFieldIn]) -> list[DependencyFieldIn]:
        _normalize_dependency_fields(v)
        return v


class AgentDefinitionUpdate(BaseModel):
    name: str | None = None
    instructions: list[str] | None = Field(default=None, min_length=1)
    tools: list[str] | None = None
    model_provider: str | None = None
    model_id: str | None = None
    model_credential_id: str | None = None
    knowledge_collection: str | None = None
    dependency_fields: list[DependencyFieldIn] | None = None
    memory_backend: Literal["none", "auto", "agentic", "common", "mem0"] | None = None
    session_summary: bool | None = None
    plain_text: bool | None = None
    num_history_runs: int | None = None
    kind: AgentKind | None = None
    response_schema: list[ResponseFieldIn] | None = None
    stages: list[dict[str, Any]] | None = None
    model_params: dict[str, Any] | None = None
    """`{}` ou `null` volta ao padrão do provedor."""
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    """`null` volta ao RUN_TIMEOUT_SECONDS do serviço."""

    @field_validator("dependency_fields")
    @classmethod
    def _validate_dependency_fields(cls, v: list[DependencyFieldIn] | None) -> list[DependencyFieldIn] | None:
        if v is not None:
            _normalize_dependency_fields(v)
        return v


class AgentDefinitionOut(BaseModel):
    agent_type: str
    name: str
    instructions: list[str]
    tools: list[str]
    model_provider: str | None
    model_id: str | None
    model_credential_id: str | None = None
    knowledge_collection: str | None = None
    dependency_fields: list[DependencyFieldOut]
    memory_backend: str
    session_summary: bool = False
    plain_text: bool = False
    num_history_runs: int
    kind: AgentKind
    response_schema: list[ResponseFieldOut]
    stages: list[dict[str, Any]] = []
    model_params: dict[str, Any] | None = None
    timeout_seconds: int | None = None
    is_seed: bool
    prompt_version: int
    created_at: datetime
    updated_at: datetime


class FeedbackIn(BaseModel):
    session_id: str
    feedback: str
    run_id: str | None = None
    """A resposta que motivou o feedback (o 👎 numa mensagem). Ela vai marcada na
    transcrição, para o merge saber de qual trecho se trata; sem isto o feedback
    vale para a conversa inteira."""


class FeedbackRule(BaseModel):
    id: str = ""
    """Vazio numa regra nova; o servidor gera."""
    texto: str = Field(..., min_length=1, max_length=500)


class FeedbackRulesIn(BaseModel):
    rules: list[FeedbackRule] = Field(..., max_length=100)


class FeedbackOut(BaseModel):
    agent_type: str
    content: str
    """Markdown derivado das regras — é o que entra nas instructions."""
    rules: list[FeedbackRule] = []
    version: int = 1
    updated_at: datetime
    diff: dict[str, list[str]] | None = None
    """Só no POST: o que o merge mudou (adicionadas, editadas, removidas, fundidas)."""


class FeedbackVersionOut(BaseModel):
    version: int
    rules: list[FeedbackRule]
    content: str
    origin: str
    created_at: datetime


def _normalize_dependency_fields(fields: list[DependencyFieldIn]) -> list[dict[str, Any]]:
    try:
        return validate_field_specs([f.model_dump() for f in fields])
    except DependencyFieldSpecError as exc:
        raise ValueError(str(exc)) from exc


def _normalize_model_params(params: dict[str, Any] | None, provider: str | None) -> dict[str, Any] | None:
    try:
        return validate_model_params(params, provider)
    except ModelParamsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _normalize_response_schema(fields: list[ResponseFieldIn]) -> list[dict[str, Any]]:
    """Valida aqui, e não num `field_validator`: as regras dos tipos compostos
    (`fields` obrigatório em object, `items` em array, profundidade) rendem
    mensagens específicas, e 422 com a explicação é mais útil que um erro de
    schema do pydantic."""
    try:
        return validate_response_schema([f.model_dump(exclude_none=True) for f in fields])
    except ResponseSchemaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class PromptVersionOut(BaseModel):
    version: int
    instructions: list[str]
    created_at: datetime


# Campos que existem para agentes conversacionais e não fazem nada num agente
# `analysis`: ele é one-shot, sem sessão, sem histórico e sem memória
# (`agents/base.py`). Aceitá-los calado deixa quem opera achando que configurou algo.
_INERTES_EM_ANALYSIS = {
    "num_history_runs": "analysis é one-shot: não há histórico para reaproveitar",
    "memory_backend": "analysis não usa memória de longo prazo (nem a do Agno, nem o mem0)",
    "session_summary": "analysis não tem sessão para resumir",
}


_PADROES_ANTIGOS = {("memory_backend", "common")}
"""Valores que já foram o padrão: um agente criado antes de 2026-09-26 tem
`memory_backend="common"` gravado, e reenviar a definição dele (`get --editable`
→ `apply`) não configurou nada."""


def _validate_kind(kind: str, response_schema: list[dict[str, Any]], stages: list[dict[str, Any]] | None = None) -> None:
    if kind == "analysis" and not response_schema:
        raise HTTPException(status_code=422, detail="kind='analysis' precisa de response_schema (ao menos 1 campo)")
    if kind != "analysis" and response_schema:
        raise HTTPException(
            status_code=422, detail=f"response_schema só se aplica a kind='analysis' (deixe [] para {kind})"
        )
    if kind == "procedural" and not stages:
        raise HTTPException(status_code=422, detail="kind='procedural' precisa de stages (ao menos uma etapa)")
    if kind != "procedural" and stages:
        raise HTTPException(status_code=422, detail=f"stages só se aplica a kind='procedural' (deixe [] para {kind})")


def _normalize_stages(stages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not stages:
        return []
    try:
        return validate_stages(stages)
    except StageSpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_procedure_tools(
    stages: list[dict[str, Any]], tools: list[str], declared_dependencies: set[str]
) -> None:
    """O que só dá para conferir com as tools em mãos.

    - Cada etapa `action` aponta para uma tool que existe, está ativa e recebe o
      que precisa: os parâmetros obrigatórios que o modelo preencheria saem dos
      campos coletados antes da etapa; os de `dependency`, de `dependency_fields`
      ou dos campos coletados.
    - Em `tools` (as que o modelo chama ao redigir a resposta) não entra tool com
      efeito colateral: efeito só por etapa `action`, depois da confirmação — senão
      o modelo poderia agir fora de ordem."""
    for name in tools:
        row = get_tool(name)
        if row is not None and row.get("side_effect") is True:
            raise HTTPException(
                status_code=422,
                detail=f"A tool {name!r} tem efeito colateral: num agente procedural ela entra como etapa "
                "action (depois de uma confirmação), não em tools.",
            )
    for index, stage in enumerate(stages):
        if stage["type"] != "action":
            continue
        row = get_tool(stage["tool"])
        if row is None:
            raise HTTPException(status_code=422, detail=f"Etapa {stage['id']!r}: tool desconhecida {stage['tool']!r}")
        if not row["enabled"]:
            raise HTTPException(status_code=422, detail=f"Etapa {stage['id']!r}: a tool {stage['tool']!r} está desativada")
        if row["kind"] == "builtin" and not stage.get("function"):
            raise HTTPException(
                status_code=422, detail=f"Etapa {stage['id']!r}: tool builtin precisa de `function` (a função da toolkit)"
            )
        if row["kind"] != "api":
            continue
        coletados = set(fields_before(stages, index))
        faltando = []
        for param in (row["config"] or {}).get("parameters") or []:
            if not param.get("required"):
                continue
            source = param.get("source", "model")
            if source == "model" and param["name"] not in coletados:
                faltando.append(param["name"])
            elif source == "dependency" and param.get("dependency") not in (
                coletados | declared_dependencies | {"idempotency_key", "dry_run"}
            ):
                faltando.append(f"dependencies.{param.get('dependency')}")
        if faltando:
            raise HTTPException(
                status_code=422,
                detail=f"Etapa {stage['id']!r}: a tool {stage['tool']!r} precisa de {', '.join(faltando)}, "
                "que nenhuma etapa anterior coleta (o nome do campo tem de ser o do parâmetro).",
            )


def _default_de(campo: str) -> Any:
    return AgentDefinitionIn.model_fields[campo].default


def _validate_inert_fields(kind: str, body: BaseModel) -> None:
    """Recusa campo inerte que veio com valor diferente do padrão.

    Dois critérios juntos, e os dois são necessários. `model_fields_set` porque
    um agente salvo carrega `num_history_runs=10` por default e cobrar isso faria
    um `PUT` de instructions falhar por um campo que ninguém escreveu. E o valor
    ter de diferir do padrão porque `kuro agents get --editable` devolve *todos*
    os campos editáveis — o fluxo documentado `get --editable > f.json && apply
    -f f.json` reenviaria os defaults e quebraria em todo agente analista.

    O que sobra é o que interessa: alguém configurou de fato algo que não vai
    surtir efeito."""
    if kind != "analysis":
        return
    configurados = [
        campo
        for campo in body.model_fields_set
        if campo in _INERTES_EM_ANALYSIS
        and getattr(body, campo) != _default_de(campo)
        and (campo, getattr(body, campo)) not in _PADROES_ANTIGOS
    ]
    if configurados:
        detalhe = "; ".join(f"{campo} ({_INERTES_EM_ANALYSIS[campo]})" for campo in sorted(configurados))
        raise HTTPException(
            status_code=422,
            detail=f"Estes campos não têm efeito em kind='analysis' — tire-os da requisição: {detalhe}",
        )


def _validate_collection(name: str | None) -> None:
    if name is not None and not collection_exists(name):
        raise HTTPException(status_code=422, detail=f"Collection desconhecida: {name!r} (veja GET /collections)")


def _validate_tools(names: list[str], declared_dependencies: set[str]) -> None:
    """Confere que os nomes existem em `tool_definitions` e que o agente declara
    as dependências que as tools exigem (parâmetros `source="dependency"`
    obrigatórios). Construir a tool de fato fica pra hora do `/chat`
    (`agents/registry.py`), não pra cada salvamento do agente."""
    unknown = [n for n in names if not tool_exists(n)]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Tools desconhecidas: {unknown}")

    faltando: dict[str, list[str]] = {}
    for name in names:
        row = get_tool(name)
        if row is None or row["kind"] != "api":
            continue
        ausentes = [d for d in required_dependencies(row["config"] or {}) if d not in declared_dependencies]
        if ausentes:
            faltando[name] = ausentes
    if faltando:
        detalhe = "; ".join(f"{tool} precisa de {', '.join(deps)}" for tool, deps in faltando.items())
        raise HTTPException(
            status_code=422,
            detail=f"Declare estes campos em dependency_fields — {detalhe}",
        )


@router.get("", response_model=list[AgentDefinitionOut])
def list_agents() -> list[dict[str, Any]]:
    return list_definitions()


@router.post("", response_model=AgentDefinitionOut, status_code=201)
def create_agent(body: AgentDefinitionIn, dry_run: bool = False) -> dict[str, Any]:
    """`?dry_run=true` valida tudo e devolve o agente como ficaria, sem gravar —
    é o que a CI de quem mantém os agentes como código roda antes do merge."""
    if get_definition(body.agent_type) is not None:
        raise HTTPException(status_code=409, detail=f"Agente {body.agent_type!r} já existe")
    payload = body.model_dump()
    payload["dependency_fields"] = _normalize_dependency_fields(body.dependency_fields)
    payload["response_schema"] = _normalize_response_schema(body.response_schema)
    payload["stages"] = _normalize_stages(body.stages)
    _validate_kind(body.kind, payload["response_schema"], payload["stages"])
    if body.kind == "procedural":
        # Antes de `_validate_tools`: uma tool com efeito colateral em `tools` é o
        # erro de fundo, e a falta de uma dependência dela seria só o sintoma.
        _validate_procedure_tools(payload["stages"], body.tools, {f["name"] for f in payload["dependency_fields"]})
    _validate_tools(body.tools, {f["name"] for f in payload["dependency_fields"]})
    _validate_collection(body.knowledge_collection)
    _validate_inert_fields(body.kind, body)
    payload["model_params"] = _normalize_model_params(body.model_params, body.model_provider)
    if dry_run:
        now = datetime.now(timezone.utc)
        return {**payload, "is_seed": False, "prompt_version": 1, "created_at": now, "updated_at": now}
    return create_definition(**payload)


@router.get("/{agent_type}", response_model=AgentDefinitionOut)
def get_agent_definition(agent_type: str) -> dict[str, Any]:
    definition = get_definition(agent_type)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")
    return definition


@router.put("/{agent_type}", response_model=AgentDefinitionOut)
def update_agent(agent_type: str, body: AgentDefinitionUpdate, dry_run: bool = False) -> dict[str, Any]:
    """`?dry_run=true` valida e devolve o agente como ficaria, sem gravar."""
    current = get_definition(agent_type)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")

    payload = body.model_dump(exclude_unset=True)
    if "knowledge_collection" in payload:
        _validate_collection(payload["knowledge_collection"])
    if body.dependency_fields is not None:
        payload["dependency_fields"] = _normalize_dependency_fields(body.dependency_fields)
    if body.response_schema is not None:
        payload["response_schema"] = _normalize_response_schema(body.response_schema)
    if body.stages is not None:
        payload["stages"] = _normalize_stages(body.stages)
    current_kind = current["kind"] or "conversational"
    if body.kind is not None and body.kind != current_kind and "procedural" in (body.kind, current_kind):
        # As conversas em andamento têm estado em procedure_runs: trocar o tipo
        # deixaria esse estado órfão (ou um agente procedural sem estado nenhum).
        raise HTTPException(
            status_code=422,
            detail="O tipo de um agente procedural não muda (nem vira procedural depois de criado): crie outro agente.",
        )
    # Valida contra o estado final: tools e dependency_fields podem vir juntos ou só um deles.
    if body.tools is not None or body.dependency_fields is not None:
        campos = payload.get("dependency_fields", current["dependency_fields"] or [])
        _validate_tools(
            body.tools if body.tools is not None else (current["tools"] or []),
            {f["name"] for f in campos},
        )
    if body.kind is not None or body.response_schema is not None or body.stages is not None:
        _validate_kind(
            payload.get("kind", current["kind"]),
            payload.get("response_schema", current["response_schema"] or []),
            payload.get("stages", current.get("stages") or []),
        )
    if payload.get("kind", current_kind) == "procedural" and (
        body.stages is not None or body.tools is not None or body.dependency_fields is not None
    ):
        _validate_procedure_tools(
            payload.get("stages", current.get("stages") or []),
            payload.get("tools", current["tools"] or []),
            {f["name"] for f in payload.get("dependency_fields", current["dependency_fields"] or [])},
        )
    _validate_inert_fields(payload.get("kind", current["kind"]), body)
    if "model_params" in payload or "model_provider" in payload:
        # Revalida contra o provedor final: thinking_budget só vale no google.
        payload["model_params"] = _normalize_model_params(
            payload.get("model_params", current.get("model_params")),
            payload.get("model_provider", current.get("model_provider")),
        )
    if dry_run:
        preview = {**current, **payload}
        if payload.get("instructions") not in (None, current["instructions"]):
            preview["prompt_version"] = current["prompt_version"] + 1
        return preview
    try:
        updated = update_definition(agent_type, **payload)
    except DefinitionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado") from exc
    return updated


@router.delete("/{agent_type}", status_code=204)
def delete_agent(agent_type: str) -> None:
    definition = get_definition(agent_type)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")
    if definition["is_seed"]:
        raise HTTPException(status_code=403, detail="Agente semeado pelo sistema não pode ser removido")
    delete_definition(agent_type)
    delete_procedures_of(agent_type)


class RevisionOut(BaseModel):
    version: int
    config_hash: str
    config: dict[str, Any]
    """Tudo o que muda o comportamento: instructions, modelo resolvido, tools (pelo
    hash da config de cada uma), schema, collection, regras de feedback..."""
    created_at: datetime
    current: bool = False


class PromoteIn(BaseModel):
    to: str = Field(..., min_length=1, max_length=64)
    """Agente que recebe a configuração. Se não existir, é criado."""


class PromoteOut(BaseModel):
    agent: AgentDefinitionOut
    source: str
    source_version: int
    previous_version: int | None
    """Versão do destino antes da promoção (`None` se ele acabou de ser criado)."""
    agent_version: int
    unchanged: bool
    """O destino já tinha exatamente esta configuração — nada foi gravado."""


_PROMOTED_FIELDS = (
    "instructions",
    "tools",
    "model_provider",
    "model_id",
    "model_credential_id",
    "knowledge_collection",
    "dependency_fields",
    "memory_backend",
    "session_summary",
    "plain_text",
    "num_history_runs",
    "kind",
    "response_schema",
    "stages",
    "model_params",
    "timeout_seconds",
)


def _current_revision(definition: dict[str, Any]) -> tuple[int, str]:
    return ensure_version(definition["agent_type"], effective_config(definition))


@router.get("/{agent_type}/revisions", response_model=list[RevisionOut])
def get_revisions(agent_type: str) -> list[dict[str, Any]]:
    """Versões da configuração inteira, mais recentes primeiro — a que roda agora
    vem com `current: true`. É o número que cada run grava em `agent_version`.

    `GET /agents/{t}/versions` continua sendo só o histórico das instructions."""
    definition = get_definition(agent_type)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")
    current, _ = _current_revision(definition)
    return [{**v, "current": v["version"] == current} for v in list_versions(agent_type)]


@router.get("/{agent_type}/revisions/{version}", response_model=RevisionOut)
def get_revision(agent_type: str, version: int) -> dict[str, Any]:
    definition = get_definition(agent_type)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")
    revision = get_version(agent_type, version)
    if revision is None:
        raise HTTPException(status_code=404, detail=f"{agent_type!r} não tem a versão {version}")
    current, _ = _current_revision(definition)
    return {**revision, "current": revision["version"] == current}


@router.post("/{agent_type}/promote", response_model=PromoteOut)
def promote_agent(agent_type: str, body: PromoteIn) -> dict[str, Any]:
    """Copia a configuração que muda o comportamento de `agent_type` para `to`:
    instructions, modelo, tools, schema, collection, dependências e as regras de
    feedback. O `name` do destino fica.

    É o fluxo draft → prod: quem integra chama sempre `r8`; as mudanças são
    feitas e avaliadas (`kuro eval`) em `r8-draft`, e só então promovidas. Para
    criar o draft, promova ao contrário: `r8` → `r8-draft`."""
    source = get_definition(agent_type)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")
    if body.to == agent_type:
        raise HTTPException(status_code=422, detail="Origem e destino são o mesmo agente.")
    if not _SLUG_RE.match(body.to):
        raise HTTPException(status_code=422, detail=f"Nome de agente inválido: {body.to!r}")
    source_version, source_hash = _current_revision(source)
    values = {field: source.get(field) for field in _PROMOTED_FIELDS}

    target = get_definition(body.to)
    if target is not None:
        source_kind = source.get("kind") or "conversational"
        target_kind = target.get("kind") or "conversational"
        if source_kind != target_kind and "procedural" in (source_kind, target_kind):
            raise HTTPException(
                status_code=422,
                detail=f"{agent_type!r} é kind={source_kind!r} e {body.to!r} é kind={target_kind!r}: o tipo de um "
                "agente procedural não muda (as conversas em andamento têm estado). Promova para um agente novo.",
            )
    previous_version: int | None = None
    if target is None:
        create_definition(agent_type=body.to, name=source["name"], **values)
    else:
        previous_version, previous_hash = _current_revision(target)
        if previous_hash == source_hash:
            return {
                "agent": target,
                "source": agent_type,
                "source_version": source_version,
                "previous_version": previous_version,
                "agent_version": previous_version,
                "unchanged": True,
            }
        update_definition(body.to, **values)

    if (source.get("kind") or "conversational") != "analysis":
        source_note = get_feedback_note(agent_type)
        target_note = get_feedback_note(body.to)
        rules = source_note["rules"] if source_note else []
        if rules != (target_note["rules"] if target_note else []):
            # Mesmo sem regras na origem, grava uma versão (vazia) em vez de apagar:
            # o histórico do destino é o caminho de volta se a promoção der errado.
            save_feedback_note(body.to, rules, origin="promote")

    promoted = get_definition(body.to)
    assert promoted is not None
    new_version, _ = _current_revision(promoted)
    return {
        "agent": promoted,
        "source": agent_type,
        "source_version": source_version,
        "previous_version": previous_version,
        "agent_version": new_version,
        "unchanged": False,
    }


@router.get("/{agent_type}/versions", response_model=list[PromptVersionOut])
def get_prompt_versions(agent_type: str) -> list[dict[str, Any]]:
    if get_definition(agent_type) is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")
    return list_prompt_versions(agent_type)


_TRANSCRIPT_TURNS = 20
"""Quantas trocas da sessão vão para o merge — o bastante para o contexto, sem
transformar uma conversa longa num prompt gigante."""


def _transcript(agent_type: str, session_id: str, run_id: str | None = None) -> str:
    """A conversa que o merge do feedback lê.

    Vem das execuções registradas da sessão (`run_store.session_turns`): vale para
    qualquer origem — console, CLI, sistema integrado —, e não depende do
    histórico do Agno, que só guarda as últimas trocas. Com `run_id`, a janela
    termina na resposta avaliada e ela vai marcada. Sessão anterior ao trace store
    local (sem runs gravados) cai no histórico do Agno, como antes."""
    from agent_service.observability.run_store import session_turns

    turns = session_turns(session_id, agent_type)
    if run_id is not None:
        position = next((i for i, t in enumerate(turns) if t["run_id"] == run_id), None)
        if position is None:
            raise HTTPException(
                status_code=422, detail=f"O run {run_id!r} não é desta sessão com {agent_type!r}."
            )
        turns = turns[: position + 1]
    turns = turns[-_TRANSCRIPT_TURNS:]
    if turns:
        blocks = []
        for index, turn in enumerate(turns, start=1):
            reply = turn["output"] or f"(sem resposta: {turn['status_message'] or turn['status']})"
            block = f"[{index}] Usuário: {turn['message'] or ''}\n    Agente: {reply}"
            if run_id is not None and turn["run_id"] == run_id:
                block += "\n    ↑ ESTA é a resposta sobre a qual o feedback foi dado."
            blocks.append(block)
        return "\n".join(blocks)

    try:
        agent, _ = get_agent_with_definition(agent_type)
    except (ToolBuildError, UnknownToolError) as exc:
        # Agente com tool quebrada: configuração do serviço, não da chamada.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        messages = agent.get_chat_history(session_id=session_id, last_n_runs=10)
    except Exception:  # noqa: BLE001 - o Agno levanta Exception("Session not found") pra sessão inexistente
        messages = []
    lines = [f"{m.role}: {m.content}" for m in messages if m.content]
    if not lines:
        raise HTTPException(
            status_code=422, detail=f"Sessão {session_id!r} não tem histórico para {agent_type!r}."
        )
    return "\n".join(lines)


@router.post("/{agent_type}/feedback", response_model=FeedbackOut)
def send_feedback(agent_type: str, body: FeedbackIn) -> dict[str, Any]:
    """Mescla um feedback textual sobre uma conversa nas regras de comportamento
    do agente — `agents/registry.py` as concatena nas instructions a partir da
    próxima chamada (sem virar uma prompt_version nova).

    A resposta traz o `diff`: o merge mexe em regras que ninguém releu, então
    dizer o que mudou é parte do resultado, não enfeite."""
    definition = _feedback_target(agent_type)
    transcript = _transcript(agent_type, body.session_id, body.run_id)
    try:
        note, diff = merge_feedback(
            agent_type,
            feedback=body.feedback,
            transcript=transcript,
            model_provider=definition["model_provider"],
            model_id=definition["model_id"],
            credential_id=definition.get("model_credential_id"),
        )
    except FeedbackMergeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {**note, "diff": diff}


@router.put("/{agent_type}/feedback", response_model=FeedbackOut)
def replace_feedback(agent_type: str, body: FeedbackRulesIn) -> dict[str, Any]:
    """Substitui as regras à mão, sem passar pelo modelo — é a saída quando um
    merge sai errado, e o que a UI usa para editar ou apagar uma regra só."""
    _feedback_target(agent_type)
    regras = [
        {"id": r.id or uuid.uuid4().hex[:8], "texto": r.texto.strip()}
        for r in body.rules
        if r.texto.strip()
    ]
    if not regras:
        raise HTTPException(
            status_code=422, detail="Sem regras. Para zerar a nota use DELETE /agents/{tipo}/feedback."
        )
    return save_feedback_note(agent_type, regras, origin="manual")


@router.delete("/{agent_type}/feedback", status_code=204)
def clear_feedback(agent_type: str) -> None:
    """Apaga a nota e o histórico dela: o agente volta a valer só pelas instructions."""
    _feedback_target(agent_type)
    clear_feedback_note(agent_type)


@router.get("/{agent_type}/feedback/versions", response_model=list[FeedbackVersionOut])
def get_feedback_versions(agent_type: str) -> list[dict[str, Any]]:
    _feedback_target(agent_type)
    return list_feedback_versions(agent_type)


@router.post("/{agent_type}/feedback/rollback/{version}", response_model=FeedbackOut)
def rollback_feedback(agent_type: str, version: int) -> dict[str, Any]:
    """Reaplica as regras de uma versão anterior (como uma versão nova, para o
    histórico não perder o caminho de volta)."""
    _feedback_target(agent_type)
    anterior = get_feedback_version(agent_type, version)
    if anterior is None:
        raise HTTPException(status_code=404, detail=f"Versão {version} não existe para {agent_type!r}")
    return save_feedback_note(agent_type, anterior["rules"], origin="rollback")


def _feedback_target(agent_type: str) -> dict[str, Any]:
    definition = get_definition(agent_type)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")
    _reject_feedback_on_analysis(definition)
    return definition


def _reject_feedback_on_analysis(definition: dict[str, Any]) -> None:
    """A nota de feedback só entra nas instructions de agente conversacional
    (`agents/registry.py`). Aceitar feedback num agente `analysis` gravaria uma
    nota que nunca seria aplicada — silêncio pior que erro."""
    if (definition.get("kind") or "conversational") == "analysis":
        raise HTTPException(
            status_code=422,
            detail=f"Agente {definition['agent_type']!r} é kind='analysis' e não usa nota de feedback "
            "(ela só orienta agentes conversacionais). Ajuste as instructions do agente.",
        )


@router.get("/{agent_type}/feedback", response_model=FeedbackOut)
def get_feedback(agent_type: str) -> dict[str, Any]:
    definition = get_definition(agent_type)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")
    _reject_feedback_on_analysis(definition)
    note = get_feedback_note(agent_type)
    if note is None:
        raise HTTPException(status_code=404, detail=f"Nenhum feedback registrado ainda para {agent_type!r}.")
    return note


# -- agente procedural: estado das conversas e funil ---------------------------------


def _procedural_target(agent_type: str) -> dict[str, Any]:
    definition = get_definition(agent_type)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Agente {agent_type!r} não encontrado")
    if (definition.get("kind") or "conversational") != "procedural":
        raise HTTPException(status_code=422, detail=f"Agente {agent_type!r} não é kind='procedural'")
    return definition


@router.get("/{agent_type}/procedures")
def get_procedure_funnel(agent_type: str, include_dry_run: bool = False) -> dict[str, Any]:
    """Funil: quantas conversas estão em cada etapa e quantas concluíram — é onde
    se vê em que ponto as pessoas desistem. Os testes (`dry_run`) ficam de fora."""
    definition = _procedural_target(agent_type)
    rows = procedure_funnel(agent_type, include_dry_run=include_dry_run)
    by_stage = {(r["stage"], r["status"]): r["sessions"] for r in rows}
    stages = [
        {"id": stage["id"], "type": stage["type"], "goal": stage["goal"], "sessions": by_stage.get((stage["id"], "active"), 0)}
        for stage in definition.get("stages") or []
    ]
    known = {s["id"] for s in stages}
    return {
        "agent_type": agent_type,
        "stages": stages,
        "done": by_stage.get((None, "done"), 0),
        # Conversas paradas numa etapa que não existe mais (o agente foi editado).
        "orphaned": sum(n for (stage, status), n in by_stage.items() if status == "active" and stage not in known),
        "total": sum(by_stage.values()),
    }


@router.get("/{agent_type}/procedures/{session_id}")
def get_procedure_state(agent_type: str, session_id: str) -> dict[str, Any]:
    """O estado de uma conversa — o mesmo `state` que o `/chat` devolve. É o que o
    console usa para mostrar as etapas ao reabrir uma conversa."""
    definition = _procedural_target(agent_type)
    row = get_procedure(agent_type, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"A sessão {session_id!r} ainda não começou o procedimento")
    view = state_view(definition.get("stages") or [], row["state"], finished=row["status"] == "done")
    return {**view, "session_id": session_id, "dry_run": row["dry_run"]}
