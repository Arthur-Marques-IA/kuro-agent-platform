"""Contrato estável de API que os demais módulos da plataforma consomem.

Fica deliberadamente separado das rotas que o AgentOS expõe automaticamente
(`/agents/{id}/runs`, etc.) — este router é o ponto único e estável que outros
serviços chamam, independente de como o agente é montado por baixo (Agno,
outro framework, etc. no futuro).

Collections de documentos ficam em `api/collections_routes.py`.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from typing import Annotated, Any

from agno.agent import Agent
from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from agno.run.agent import RunEvent

from agent_service.api import idempotency
from agent_service.agents.attachments import AttachmentError, AttachmentIn, build_media, describe_attachments
from agent_service.agents.dependency_fields import DependencyValidationError, validate_dependencies
from agent_service.agents.procedure_runner import (
    ProcedureBusyError,
    ProcedureModelError,
    is_procedural,
    run_procedural_turn,
)
from agent_service.agents.registry import UnknownAgentTypeError, get_agent_with_definition, list_agent_types
from agent_service.config import get_settings
from agent_service.documents.collections import EmbedderError
from agent_service.documents.retrieval import sources_from
from agent_service.observability.tracing import (
    RUN_FAILED,
    RunContext,
    RunTimeoutError,
    structured_output_error,
    traced_run_events,
)
from agent_service.tools.context import set_idempotency_key
from agent_service.tools.registry import ToolBuildError, UnknownToolError

logger = logging.getLogger(__name__)

router = APIRouter()

_slots: asyncio.Semaphore | None = None
_waiting = 0


def _busy() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Serviço no limite de execuções simultâneas. Tente de novo em instantes.",
        headers={"Retry-After": "5"},
    )


@asynccontextmanager
async def _run_slot() -> AsyncIterator[None]:
    """Uma vaga de execução (`MAX_CONCURRENT_RUNS`, por processo). Sem vaga, a
    chamada espera na fila, em ordem de chegada, até `QUEUE_MAX_WAIT_SECONDS`; o
    contrato HTTP não muda — só demora mais. Fila cheia ou espera esgotada: 503
    com `Retry-After`, para quem chama (o daemon do Regente, por exemplo) tentar
    de novo, em vez de acumular conexões até derrubar o serviço."""
    global _slots, _waiting
    settings = get_settings()
    if _slots is None:
        _slots = asyncio.Semaphore(settings.max_concurrent_runs)
    if _slots.locked():
        if settings.queue_max_wait_seconds <= 0 or _waiting >= settings.queue_max_size:
            raise _busy()
        _waiting += 1
        try:
            await asyncio.wait_for(_slots.acquire(), settings.queue_max_wait_seconds)
        except TimeoutError:
            raise _busy() from None
        finally:
            _waiting -= 1
    else:
        await _slots.acquire()
    try:
        yield
    finally:
        _slots.release()


IdempotencyKey = Annotated[
    str | None,
    Header(
        alias=idempotency.HEADER,
        description="Repetir com a mesma chave (e o mesmo corpo) devolve a resposta guardada, sem rodar de novo; "
        "409 se a primeira ainda está em andamento, 422 se o corpo for outro. As tools recebem a chave em "
        "`dependencies.idempotency_key`. Vale por IDEMPOTENCY_TTL_HOURS (24 h).",
    ),
]


async def _idempotent(
    endpoint: str, request: BaseModel, key: str | None, handler: Callable[[], Awaitable[BaseModel]]
) -> Any:
    """Roda `handler` sob o `Idempotency-Key`, se veio (ver `api/idempotency.py`). Qualquer
    falha libera a chave: a repetição roda de verdade, e o destino da tool deduplica."""
    if key is None:
        return await handler()
    try:
        key = idempotency.check_key(key)
        body_hash = idempotency.request_hash(request.model_dump(mode="json"))
        reservation = await run_in_threadpool(idempotency.reserve, endpoint, request.agent_type, key, body_hash)
    except idempotency.IdempotencyError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    if reservation.id is None:
        return JSONResponse(reservation.replay, headers={idempotency.REPLAYED_HEADER: "true"})
    set_idempotency_key(key)
    try:
        response = await handler()
    except BaseException:
        # `shield`: um cancelamento (cliente desistiu) não pode deixar a chave presa em 409.
        await asyncio.shield(run_in_threadpool(idempotency.release, reservation.id))
        raise
    finally:
        set_idempotency_key(None)
    await run_in_threadpool(idempotency.complete, reservation.id, response.model_dump(mode="json"))
    return response


def _timeout_for(definition: dict[str, Any]) -> float:
    return float(definition.get("timeout_seconds") or get_settings().run_timeout_seconds)


MetadataValue = str | int | float | bool


def _normalize_metadata(value: dict[str, Any] | None) -> dict[str, str]:
    """Correlação com o sistema que chama: chave/valor simples, poucos e curtos.
    Valores viram texto — é o que se filtra depois (`?meta=conversation_id=123`)."""
    if not value:
        return {}
    if len(value) > 20:
        raise ValueError("metadata aceita no máximo 20 chaves")
    normalized = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ValueError(f"chave de metadata inválida: {key!r}")
        if isinstance(item, bool):
            item = "true" if item else "false"
        text = str(item)
        if len(text) > 256:
            raise ValueError(f"metadata.{key} passa de 256 caracteres")
        normalized[key] = text
    return normalized


def _run_metadata(value: dict[str, Any] | None, dry_run: bool) -> dict[str, str]:
    metadata = _normalize_metadata(value)
    if dry_run:
        metadata["dry_run"] = "true"
    return metadata


class ChatRequest(BaseModel):
    agent_type: str = "conversational"
    user_id: str
    session_id: str
    message: str
    dependencies: dict[str, Any] | None = None
    """Metadados opcionais (ex: cpf, nome) injetados como contexto estruturado
    no prompt — ver a aba Integração dos agentes no frontend para exemplos."""
    attachments: list[AttachmentIn] = []
    """Imagem, áudio, vídeo ou arquivo (PDF etc.) em base64 (`content_base64`)
    ou por `url`, com `mime_type`/`filename` — o modelo do agente precisa
    suportar o tipo (ex.: Gemini)."""
    metadata: dict[str, MetadataValue] | None = None
    """Correlação com o sistema que chama (ex.: `{"conversation_id": "123"}`):
    não vai para o modelo, fica gravada no run e serve de filtro em `/observability/runs`."""
    dry_run: bool = False
    """Execução de teste: toda chamada HTTP de tool leva `X-Kuro-Dry-Run: true`
    e `dependencies.dry_run` vale true para parâmetros `source="dependency"`.
    Quem implementa a tool decide o que simular. O run fica com
    `metadata.dry_run = "true"` (filtrável em `/observability/runs`)."""

    @field_validator("metadata")
    @classmethod
    def _check_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        _normalize_metadata(value)
        return value


class Source(BaseModel):
    """Um documento (ou página de site) da base de conhecimento que o agente
    consultou no run — a citação da resposta. Os trechos do mesmo documento vêm
    juntos; o texto de cada um está no trace (`GET /observability/runs/{run_id}`)."""

    title: str | None = None
    """O título da página (fonte de site) ou o nome do documento."""
    document: str | None = None
    source_url: str | None = None
    """Só em fonte de site (ou documento que veio de uma URL)."""
    content_id: str | None = None
    """O documento na collection (`GET /collections/{nome}/docs`)."""
    collection: str | None = None
    score: float | None = None
    """O melhor score entre os trechos deste documento (similaridade 0–1 ou o do reranker)."""
    chunk_ids: list[str] = []
    """Os trechos do documento que a busca devolveu, na ordem em que apareceram."""


class ChatResponse(BaseModel):
    agent_type: str
    session_id: str
    content: str
    run_id: str
    """Id do run — use em `POST /observability/scores` para enviar feedback."""
    trace_id: str | None = None
    """Id do trace do run — o mesmo no trace store local e no Langfuse, se ligado."""
    agent_version: int | None = None
    """Versão da configuração inteira que respondeu (`GET /agents/{t}/revisions`)."""
    config_hash: str | None = None
    sources: list[Source] = []
    """Os documentos da base de conhecimento que as buscas do run devolveram,
    deduplicados e do mais relevante para o menos (`[]` sem busca). É o que o
    agente leu, não necessariamente o que ele citou no texto."""
    state: dict[str, Any] | None = None
    """Só em agente `kind="procedural"`: a etapa atual, o que foi coletado, o que
    falta e, quando `done`, o `result`. Quem integra sabe quando acabou sem
    interpretar o texto (ver `agents/procedural.py::state_view`)."""


def _resolve(request: ChatRequest, endpoint: str) -> tuple[Agent, RunContext]:
    """Monta o agente e o contexto do run. Faz I/O síncrono no Postgres (definição,
    nota de feedback, uma consulta por tool): chame via `run_in_threadpool`, nunca
    direto numa rota `async` — travaria o event loop do worker inteiro."""
    try:
        agent, definition = get_agent_with_definition(request.agent_type)
    except UnknownAgentTypeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ToolBuildError, UnknownToolError) as exc:
        # Uma das tools do agente não pôde ser montada (config inválida, dependência
        # opcional ausente, tool Python desligada...) — problema nosso, não de quem chama.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except EmbedderError as exc:
        # A collection do agente perdeu a credencial do embedder: configuração do
        # serviço, não da chamada.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        dependencies = validate_dependencies(definition.get("dependency_fields"), request.dependencies)
    except DependencyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    media = _build_media_or_422(request.attachments)

    run = RunContext(
        endpoint=endpoint,
        agent_type=request.agent_type,
        agent_name=definition["name"],
        prompt_version=definition["prompt_version"],
        agent_version=definition.get("agent_version"),
        config_hash=definition.get("config_hash"),
        timeout_seconds=_timeout_for(definition),
        metadata=_run_metadata(request.metadata, request.dry_run),
        dry_run=request.dry_run,
        user_id=request.user_id,
        session_id=request.session_id,
        message=request.message,
        dependencies=dependencies,
        attachments=tuple(describe_attachments(request.attachments)),
        images=tuple(media["images"]),
        audio=tuple(media["audio"]),
        videos=tuple(media["videos"]),
        files=tuple(media["files"]),
        definition=definition,
    )
    return agent, run


def _build_media_or_422(attachments: list[AttachmentIn]) -> dict[str, list[Any]]:
    try:
        return build_media(attachments)
    except AttachmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/health")
def health() -> dict[str, str]:
    """Aberta de propósito: é o healthcheck do container e de qualquer load
    balancer na frente. Só responde se o processo está de pé — não checa banco:
    com o Postgres fora, reiniciar o container não resolveria nada.

    `auth` aparece aqui porque é a primeira coisa que alguém precisa saber ao
    olhar um serviço que não conhece, e não é segredo: quem não tem chave
    descobre no primeiro request de qualquer jeito.

    `version` é a versão do serviço (`pyproject.toml`), a mesma das notas de
    versão em `docs/notas-de-versao.md` — é por ela que quem integra sabe o que
    já está no ar.

    `model_credentials` diz se dá para cadastrar chaves de provedor: sem
    `CREDENTIALS_ENCRYPTION_KEY`, o `/model-credentials` responde 503. O serviço
    sobe mesmo assim — o google ainda funciona pela `GOOGLE_API_KEY` do ambiente."""
    from agent_service.api.auth import auth_enabled
    from agent_service.models.crypto import encryption_status

    return {
        "status": "ok",
        "version": service_version(),
        "auth": "enabled" if auth_enabled() else "disabled",
        "model_credentials": encryption_status(),
    }


def service_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("agent-service")
    except PackageNotFoundError:  # rodando do código-fonte sem o projeto instalado
        return "desconhecida"


@router.get("/ready")
def ready() -> dict[str, str]:
    """Pronto para executar: o processo está de pé **e** o banco responde. É o que
    um circuit breaker de quem chama deve olhar — `/health` só diz que o processo
    vive. 503 com o motivo quando o banco não responde. Aberta, como `/health`."""
    from sqlalchemy import text

    from agent_service.db import get_db

    try:
        with get_db().db_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # qualquer falha de conexão/consulta é "não pronto"
        logger.warning("Readiness: banco indisponível", exc_info=True)
        raise HTTPException(status_code=503, detail=f"Banco indisponível: {type(exc).__name__}") from exc
    return {"status": "ready"}


@router.get("/agent-types")
def agent_types() -> dict[str, list[str]]:
    return {"agent_types": list_agent_types()}


def _check_input_size(text: str, field: str) -> None:
    """Texto de entrada tem teto. Sem ele, o documento vai inteiro para o modelo e
    a falha aparece como um erro do provedor, sem dizer o que foi grande demais.
    O limite é em caracteres, não em tokens — é o que dá para medir aqui."""
    limit = get_settings().max_input_chars
    if len(text) > limit:
        raise HTTPException(
            status_code=422,
            detail=f"{field} tem {len(text)} caracteres e o limite é {limit} "
            f"(MAX_INPUT_CHARS). Resuma o texto, ou mande o conteúdo como anexo.",
        )


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, idempotency_key: IdempotencyKey = None) -> ChatResponse:
    _check_input_size(request.message, "message")
    return await _idempotent("chat", request, idempotency_key, lambda: _chat(request, idempotency_key))


async def _chat(request: ChatRequest, idempotency_key: str | None) -> ChatResponse:
    agent, run = await run_in_threadpool(_resolve, request, "chat")
    if idempotency_key:
        run.metadata["idempotency_key"] = idempotency_key.strip()
    if is_procedural(run):
        return await _chat_procedural(request, agent, run)

    chunks: list[str] = []
    final_content: str | None = None
    async with _run_slot(), aclosing(traced_run_events(agent, run)) as events:
        try:
            async for event in events:
                if event.event == RunEvent.run_content.value and isinstance(event.content, str):
                    chunks.append(event.content)
                elif event.event == RunEvent.run_completed.value and isinstance(event.content, str):
                    final_content = event.content
                elif event.event == RunEvent.run_error.value:
                    raise HTTPException(status_code=502, detail=event.content or RUN_FAILED)
        except RunTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc

    return ChatResponse(
        agent_type=request.agent_type,
        session_id=request.session_id,
        content=final_content if final_content is not None else "".join(chunks),
        run_id=run.run_id,
        trace_id=run.trace_id,
        agent_version=run.agent_version,
        config_hash=run.config_hash,
        sources=sources_from(run.knowledge_searches),
    )


async def _chat_procedural(request: ChatRequest, agent: Agent, run: RunContext) -> ChatResponse:
    chunks: list[str] = []
    state: dict[str, Any] | None = None
    async with _run_slot():
        try:
            async with aclosing(run_procedural_turn(agent, run)) as events:
                async for kind, data in events:
                    if kind == "message":
                        chunks.append(data)
                    elif kind == "state":
                        state = data
                    elif kind == "error":
                        raise HTTPException(status_code=502, detail=data)
        except RunTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ProcedureBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProcedureModelError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ChatResponse(
        agent_type=request.agent_type,
        session_id=request.session_id,
        content="".join(chunks),
        run_id=run.run_id,
        trace_id=run.trace_id,
        agent_version=run.agent_version,
        config_hash=run.config_hash,
        state=state,
    )


class AnalyzeRequest(BaseModel):
    agent_type: str
    document: str
    dependencies: dict[str, Any] | None = None
    attachments: list[AttachmentIn] = []
    """Mesmo formato do `/chat` — ex.: um PDF direto em vez de texto extraído
    (nesse caso `document` pode ser só uma instrução curta, como 'veja o anexo')."""
    metadata: dict[str, MetadataValue] | None = None
    """Correlação com o sistema que chama — ver `ChatRequest.metadata`."""
    dry_run: bool = False
    """Ver `ChatRequest.dry_run`."""
    session_id: str | None = Field(default=None, max_length=200)
    """Agrupa as decisões de uma mesma conversa em `/observability/sessions`
    (ex.: o `conversation_id` do WhatsApp). Não cria histórico: o analista
    continua one-shot."""
    user_id: str | None = Field(default=None, max_length=200)

    @field_validator("metadata")
    @classmethod
    def _check_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        _normalize_metadata(value)
        return value


class AnalyzeResponse(BaseModel):
    agent_type: str
    result: dict[str, Any]
    """Saída validada contra o `response_schema` do agente."""
    run_id: str
    """Id da execução — serve de id da decisão na auditoria de quem chama."""
    trace_id: str | None = None
    agent_version: int | None = None
    """Versão da configuração inteira que decidiu (`GET /agents/{t}/revisions`)."""
    config_hash: str | None = None
    sources: list[Source] = []
    """Ver `ChatResponse.sources`."""


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest, idempotency_key: IdempotencyKey = None) -> AnalyzeResponse:
    """One-shot: sem sessão/histórico — devolve o `document` analisado como
    objeto estruturado (`response_schema` do agente), não texto."""
    _check_input_size(request.document, "document")
    return await _idempotent("analyze", request, idempotency_key, lambda: _analyze(request, idempotency_key))


async def _analyze(request: AnalyzeRequest, idempotency_key: str | None) -> AnalyzeResponse:
    try:
        # I/O síncrono no Postgres — fora do event loop (ver `_resolve`).
        agent, definition = await run_in_threadpool(get_agent_with_definition, request.agent_type)
    except UnknownAgentTypeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ToolBuildError, UnknownToolError, *EmbedderError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if (definition.get("kind") or "conversational") != "analysis":
        raise HTTPException(
            status_code=422, detail=f"Agente {request.agent_type!r} não é do tipo 'analysis' (veja GET /agents)."
        )

    try:
        dependencies = validate_dependencies(definition.get("dependency_fields"), request.dependencies)
    except DependencyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    media = _build_media_or_422(request.attachments)

    run = RunContext(
        endpoint="analyze",
        agent_type=request.agent_type,
        agent_name=definition["name"],
        prompt_version=definition["prompt_version"],
        agent_version=definition.get("agent_version"),
        config_hash=definition.get("config_hash"),
        timeout_seconds=_timeout_for(definition),
        metadata=_run_metadata(request.metadata, request.dry_run)
        | ({"idempotency_key": idempotency_key.strip()} if idempotency_key else {}),
        dry_run=request.dry_run,
        definition=definition,
        user_id=request.user_id or "analysis",
        session_id=request.session_id or f"analyze-{uuid.uuid4().hex}",
        message=request.document,
        dependencies=dependencies,
        attachments=tuple(describe_attachments(request.attachments)),
        images=tuple(media["images"]),
        audio=tuple(media["audio"]),
        videos=tuple(media["videos"]),
        files=tuple(media["files"]),
    )

    final_content: Any = None
    async with _run_slot(), aclosing(traced_run_events(agent, run)) as events:
        try:
            async for event in events:
                if event.event == RunEvent.run_completed.value:
                    final_content = event.content
                elif event.event == RunEvent.run_error.value:
                    raise HTTPException(status_code=502, detail=event.content or RUN_FAILED)
        except RunTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc

    # O run já foi gravado como erro com esta mesma mensagem (`_recorded_events`).
    if (problem := structured_output_error(run, final_content)) is not None:
        raise HTTPException(status_code=502, detail=problem)

    return AnalyzeResponse(
        agent_type=request.agent_type,
        result=final_content.model_dump(mode="json"),
        run_id=run.run_id,
        trace_id=run.trace_id,
        agent_version=run.agent_version,
        config_hash=run.config_hash,
        sources=sources_from(run.knowledge_searches),
    )


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest, idempotency_key: IdempotencyKey = None) -> EventSourceResponse:
    """Streaming via POST (não GET): a mensagem do usuário vai no corpo, não
    na query string — evita acabar em log de acesso. `EventSource` do browser
    só faz GET, então o cliente precisa consumir isso com `fetch` + leitura
    manual do stream (ver `frontend/`), não com a API `EventSource`.

    Eventos: `run` ({run_id, trace_id}, antes de tudo), `message` ({content}, a
    cada trecho), `usage` (métricas do run), `error` ({message}), `state` (só em
    agente procedural: o mesmo `state` do `/chat`, antes do `done`), `sources`
    ({sources}: o mesmo `sources` do `/chat`, antes do `done`, só quando o run
    terminou sem erro) e `done`.

    Não aceita `Idempotency-Key` (422): a resposta sai aos pedaços e não há o que guardar
    para devolver a quem repete — ignorar o header daria uma proteção que não existe.
    """
    if idempotency_key is not None:
        raise HTTPException(
            status_code=422,
            detail=f"{idempotency.HEADER} não vale no /chat/stream: use o /chat para uma chamada que pode ser repetida.",
        )
    agent, run = await run_in_threadpool(_resolve, request, "chat.stream")
    trace_id = run.trace_id

    run_event = {
        "run_id": run.run_id,
        "trace_id": trace_id,
        "agent_version": run.agent_version,
        "config_hash": run.config_hash,
    }

    async def procedural_events():
        async with aclosing(run_procedural_turn(agent, run)) as events:
            async for kind, data in events:
                if kind == "message":
                    yield {"event": "message", "data": json.dumps({"content": data})}
                elif kind == "usage":
                    yield {"event": "usage", "data": json.dumps(data)}
                elif kind == "error":
                    yield {"event": "error", "data": json.dumps({"message": data})}
                elif kind == "state":
                    yield {"event": "state", "data": json.dumps(data, default=str)}

    async def event_generator():
        yield {"event": "run", "data": json.dumps(run_event)}
        try:
            if is_procedural(run):
                async with _run_slot():
                    async for item in procedural_events():
                        yield item
                yield {"event": "done", "data": "{}"}
                return
            failed = False
            async with _run_slot(), aclosing(traced_run_events(agent, run)) as events:
                async for event in events:
                    if event.event == RunEvent.run_content.value and event.content:
                        yield {"event": "message", "data": json.dumps({"content": event.content})}
                    elif event.event == RunEvent.run_completed.value and event.metrics:
                        yield {"event": "usage", "data": json.dumps(event.metrics.to_dict())}
                    elif event.event == RunEvent.run_error.value:
                        failed = True
                        yield {"event": "error", "data": json.dumps({"message": event.content or RUN_FAILED})}
            if not failed:
                sources = sources_from(run.knowledge_searches)
                yield {"event": "sources", "data": json.dumps({"sources": sources}, default=str)}
        except HTTPException as exc:  # sem vaga: o status já saiu (200), então vira evento
            yield {"event": "error", "data": json.dumps({"message": exc.detail, "status": exc.status_code})}
        except RunTimeoutError as exc:
            yield {"event": "error", "data": json.dumps({"message": str(exc), "status": 504})}
        except ProcedureBusyError as exc:
            yield {"event": "error", "data": json.dumps({"message": str(exc), "status": 409})}
        except ProcedureModelError as exc:
            yield {"event": "error", "data": json.dumps({"message": str(exc), "status": 502})}
        except Exception:
            logger.exception("Falha no streaming do agente %r", request.agent_type)
            yield {"event": "error", "data": json.dumps({"message": RUN_FAILED})}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_generator())
