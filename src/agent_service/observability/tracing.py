"""Registro das execuções: trace store local sempre, Langfuse opcional.

Toda execução passa por `traced_run_events()` e é gravada no trace store local
(`run_store.py`, no Postgres do serviço) — é o que alimenta `kuro runs` e as
rotas `/observability/*`, com ou sem Langfuse.

O Langfuse é um exportador adicional, para quem quer a análise profunda: só é
importado com `LANGFUSE_ENABLED=true` e o extra `[observability]` instalado.
Ligado, `configure_tracing()` (startup, ver `main.py`) cria o cliente do Langfuse e
liga o `AgnoInstrumentor` (OpenInference): cada execução do Agno vira spans
OpenTelemetry — o run do agente, cada chamada ao modelo (prompt, resposta,
tokens; o Langfuse calcula o custo) e cada tool call. O exportador do SDK
envia os spans em lote, em background, sem atrasar a resposta.

`traced_run_events()` envolve cada run de `/chat` e `/chat/stream` numa
observação raiz com usuário, sessão, agente e versão do prompt. O `run_id` do
Agno é gerado aqui, antes do run, e o trace usa um id derivado dele
(`trace_id_for_run`) — assim qualquer resposta, inclusive as reidratadas do
histórico, aponta para o próprio trace e pode receber feedback (`score_run`).

Sem `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (ou com `LANGFUSE_ENABLED=false`)
o exportador vira no-op; o registro local continua.
"""

import asyncio
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import TYPE_CHECKING, Any, AsyncIterator
from uuid import uuid4

from agno.agent import Agent
from agno.run.agent import RunEvent, RunOutputEvent

from agent_service.config import get_settings
from agent_service.documents import retrieval
from agent_service.tools import failures
from agent_service.tools.context import set_dependencies, set_dry_run

if TYPE_CHECKING:
    from langfuse import Langfuse

logger = logging.getLogger(__name__)

FEEDBACK = "feedback"
RUN_FAILED = "Falha ao executar o agente."
RUN_INTERRUPTED = (
    "Execução interrompida antes de terminar: quem chamou desconectou ou cancelou "
    "(ex.: timeout do cliente menor que o timeout_seconds do agente)."
)


class RunTimeoutError(RuntimeError):
    """A execução passou do tempo limite do agente — 504 para quem chama."""
PROJECT_LOOKUP_RETRY_SECONDS = 60

_client: "Langfuse | None" = None
_project_url: str | None = None
_project_lookup_failed_at: float | None = None
_END = object()


@dataclass(frozen=True)
class RunContext:
    """Uma execução de agente e o que o trace precisa saber dela."""

    endpoint: str
    agent_type: str
    agent_name: str
    prompt_version: int
    user_id: str
    session_id: str
    message: str
    dependencies: dict[str, Any] | None = None
    # Mídia anexada (objetos `agno.media` já prontos — ver `agents/attachments.py`).
    images: tuple[Any, ...] = ()
    audio: tuple[Any, ...] = ()
    videos: tuple[Any, ...] = ()
    files: tuple[Any, ...] = ()
    run_id: str = field(default_factory=lambda: str(uuid4()))
    agent_version: int | None = None
    """Versão da configuração inteira (`agents/versions.py`)."""
    config_hash: str | None = None
    timeout_seconds: float | None = None
    """Tempo limite da execução inteira; `None` = sem limite."""
    metadata: dict[str, str] = field(default_factory=dict)
    """Correlação de quem chamou (ex.: `conversation_id`), gravada com o run."""
    dry_run: bool = False
    """Execução de teste — avisada às tools (ver `tools/context.py`)."""

    @property
    def trace_id(self) -> str:
        return trace_id_for_run(self.run_id)

    attachments: tuple[dict[str, Any], ...] = ()
    """Nome, tipo e tamanho de cada anexo (`describe_attachments`) — sem o conteúdo."""

    definition: dict[str, Any] | None = field(default=None, compare=False, repr=False)
    """A definição do agente que vai rodar — a rota decide por ela o caminho
    (um agente procedural não é um run só do modelo)."""

    extra_spans: tuple[Any, ...] = ()
    """Spans que aconteceram fora do run do Agno e fazem parte da mesma execução
    (a extração e a ação de um agente procedural), gravados junto no run."""

    @property
    def attachment_counts(self) -> dict[str, int]:
        """Quantos anexos de cada tipo — vai pro Langfuse no lugar do conteúdo."""
        counts = {
            "images": len(self.images),
            "audio": len(self.audio),
            "videos": len(self.videos),
            "files": len(self.files),
        }
        return {k: v for k, v in counts.items() if v}


def trace_id_for_run(run_id: str) -> str:
    """Id do trace para um `run_id` do Agno — determinístico.

    Mesma derivação de `Langfuse.create_trace_id(seed=...)`, sem importar o SDK:
    o id bate com o do Langfuse quando ele está ligado."""
    return sha256(run_id.encode("utf-8")).digest()[:16].hex()


def configure_tracing() -> None:
    global _client
    if _client is not None:
        return
    settings = get_settings()
    if not settings.langfuse_enabled:
        logger.info("Langfuse desligado (LANGFUSE_ENABLED=false): runs só no trace store local.")
        return
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        logger.warning("LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY ausentes: runs só no trace store local.")
        return

    try:
        from langfuse import Langfuse
        from openinference.instrumentation.agno import AgnoInstrumentor
        from opentelemetry import trace as otel_trace
    except ImportError:
        logger.warning(
            "LANGFUSE_ENABLED=true, mas o extra `observability` não está instalado "
            "(uv sync --extra observability): runs só no trace store local."
        )
        return

    _client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        environment=settings.langfuse_environment,
        timeout=settings.langfuse_timeout_seconds,
    )
    # O Langfuse registra o TracerProvider global; o instrumentor do Agno publica nele.
    AgnoInstrumentor().instrument(tracer_provider=otel_trace.get_tracer_provider())


def get_langfuse() -> "Langfuse | None":
    return _client


def project_url() -> str | None:
    """Projeto na UI do Langfuse (links do console), ou `None` se indisponível."""
    global _project_url, _project_lookup_failed_at
    client = _client
    if client is None:
        return None
    if _project_url is not None:
        return _project_url

    settings = get_settings()
    project_id = settings.langfuse_project_id
    if not project_id:
        # Sem LANGFUSE_PROJECT_ID, pergunta à API — mas sem martelar um Langfuse fora do ar.
        failed_at = _project_lookup_failed_at
        if failed_at is not None and time.monotonic() - failed_at < PROJECT_LOOKUP_RETRY_SECONDS:
            return None
        try:
            projects = client.api.projects.get()
            project_id = projects.data[0].id if projects.data else None
        except Exception:
            logger.warning("Não foi possível descobrir o projeto do Langfuse", exc_info=True)
        if not project_id:
            _project_lookup_failed_at = time.monotonic()
            return None

    base_url = (settings.langfuse_public_url or settings.langfuse_base_url).rstrip("/")
    _project_url = f"{base_url}/project/{project_id}"
    return _project_url


def _agent_events(agent: Agent, run: RunContext) -> AsyncIterator[RunOutputEvent]:
    # Além de irem para o contexto do prompt, as dependências ficam disponíveis para
    # as tools com parâmetros `source="dependency"` (ver tools/context.py).
    set_dependencies(run.dependencies)
    set_dry_run(run.dry_run)
    failures.start_run()
    retrieval.start_run()
    return agent.arun(
        run.message,
        user_id=run.user_id,
        session_id=run.session_id,
        run_id=run.run_id,
        stream=True,
        stream_events=True,
        dependencies=run.dependencies,
        add_dependencies_to_context=True if run.dependencies else None,
        images=list(run.images) or None,
        audio=list(run.audio) or None,
        videos=list(run.videos) or None,
        files=list(run.files) or None,
    )


def _utc(timestamp: float | None) -> datetime | None:
    return datetime.fromtimestamp(timestamp, timezone.utc) if timestamp else None


def _ms(seconds: float | None) -> float | None:
    return round(seconds * 1000, 1) if seconds else None


def _model_spans(metrics: Any, started_at: datetime) -> list[Any]:
    """Uma span por modelo chamado, das métricas agregadas do Agno (`RunMetrics.details`)."""
    from agent_service.observability.run_store import SpanRecord

    from agent_service.models.pricing import estimate_cost

    spans = []
    for model_type, entries in (getattr(metrics, "details", None) or {}).items():
        for entry in entries or []:
            provider = getattr(entry, "provider", "") or "model"
            cost = getattr(entry, "cost", None)
            if cost is None:
                cost = estimate_cost(
                    provider=provider,
                    model_id=getattr(entry, "id", None),
                    input_tokens=getattr(entry, "input_tokens", 0) or 0,
                    output_tokens=getattr(entry, "output_tokens", 0) or 0,
                    reasoning_tokens=getattr(entry, "reasoning_tokens", 0) or 0,
                )
            spans.append(
                SpanRecord(
                    type="GENERATION",
                    name=f"{provider}.{model_type}",
                    started_at=started_at,
                    model=getattr(entry, "id", None) or None,
                    input_tokens=getattr(entry, "input_tokens", 0) or 0,
                    output_tokens=getattr(entry, "output_tokens", 0) or 0,
                    total_tokens=getattr(entry, "total_tokens", 0) or 0,
                    cost_usd=cost,
                    latency_ms=_ms(getattr(entry, "duration", None)),
                    metadata=_cache_metadata(entry),
                )
            )
    return spans


def _cache_metadata(entry: Any) -> dict[str, int]:
    """Tokens servidos do cache de prompt (implícito no Gemini e no OpenAI; o do
    Claude com `model_params.prompt_cache`). É o jeito de ver se o prefixo — as
    instructions e as tools — está mesmo sendo reaproveitado entre chamadas."""
    return {
        name: value
        for name in ("cache_read_tokens", "cache_write_tokens")
        if (value := getattr(entry, name, 0) or 0)
    }


def _tool_output(tool: Any) -> Any:
    result = getattr(tool, "result", None)
    # O gerenciador de memória do Agno devolve "No response from model" quando o
    # modelo só chamou as funções de gravar — é o caso de sucesso, não um erro.
    if getattr(tool, "tool_name", None) == "update_user_memory" and result == "No response from model":
        from agent_service.memory.managers import MEMORY_TOOL_OUTPUT

        return MEMORY_TOOL_OUTPUT
    return result


def _tool_span(event: Any, now: datetime) -> Any:
    from agent_service.observability.run_store import SpanRecord

    tool = event.tool
    tool_metrics = getattr(tool, "metrics", None)
    name = getattr(tool, "tool_name", None) or "tool"
    output = _tool_output(tool)
    raised = event.event == RunEvent.tool_call_error.value or bool(getattr(tool, "tool_call_error", False))
    # A tool de API devolve o erro como texto (o modelo precisa ler), e para o
    # Agno isso é sucesso — o registro de `tools/failures.py` é que diz que falhou.
    registered = failures.lookup_failure(name, getattr(tool, "result", None))
    metadata: dict[str, Any] = {}
    status_message = None
    if registered is not None:
        kind, http_status = registered
        metadata = {"failure": kind, **({"http_status": http_status} if http_status else {})}
        status_message = str(getattr(tool, "result", "") or "")[:500]
    elif raised:
        metadata = {"failure": "exception"}
        status_message = getattr(event, "error", None) or str(getattr(tool, "result", "") or "")[:500] or None
    if name == retrieval.SEARCH_TOOL:
        # Os trechos que a busca devolveu, com documento, posição e score — o
        # texto cru continua no `output`, que é o que o modelo leu.
        search = retrieval.take_search((getattr(tool, "tool_args", None) or {}).get("query"))
        if search is not None:
            metadata["references"] = search
    return SpanRecord(
        type="TOOL",
        name=name,
        started_at=_utc(getattr(tool_metrics, "start_time", None)) or now,
        ended_at=_utc(getattr(tool_metrics, "end_time", None)),
        level=failures.span_level(metadata["failure"]) if "failure" in metadata else "DEFAULT",
        status_message=status_message,
        input=getattr(tool, "tool_args", None),
        output=output,
        latency_ms=_ms(getattr(tool_metrics, "duration", None)),
        metadata=metadata,
    )


async def _recorded_events(agent: Agent, run: RunContext) -> AsyncIterator[RunOutputEvent]:
    """Eventos do run, gravados no trace store local ao terminar — sempre com status
    terminal: sucesso, erro ou interrompido (cliente saiu / cancelamento)."""
    from agent_service.observability import run_store

    record = run_store.RunRecord(
        run_id=run.run_id,
        trace_id=run.trace_id,
        agent_type=run.agent_type,
        agent_name=run.agent_name,
        prompt_version=run.prompt_version,
        agent_version=run.agent_version,
        config_hash=run.config_hash,
        endpoint=run.endpoint,
        user_id=run.user_id,
        session_id=run.session_id,
        message=run.message,
        started_at=datetime.now(timezone.utc),
        metadata=dict(run.metadata),
        input={
            "message": run.message,
            "dependencies": run.dependencies,
            **({"attachments": list(run.attachments) or run.attachment_counts} if run.attachment_counts else {}),
        },
    )
    chunks: list[str] = []
    final_content: str | None = None
    events = _agent_events(agent, run)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + run.timeout_seconds if run.timeout_seconds else None
    try:
        while True:
            try:
                if deadline is None:
                    event = await anext(events)
                else:
                    # O prazo vale por evento, dentro deste generator: um `asyncio.timeout`
                    # em volta do consumo atravessaria os `yield` e cancelaria o código
                    # de quem consome (ex. o envio do SSE).
                    event = await asyncio.wait_for(anext(events), max(deadline - loop.time(), 0))
            except StopAsyncIteration:
                break
            except TimeoutError:
                message = f"Tempo limite de {run.timeout_seconds:g}s excedido."
                record.status, record.status_message = "error", message
                raise RunTimeoutError(message) from None
            if event.event == RunEvent.run_content.value and isinstance(event.content, str):
                chunks.append(event.content)
            elif event.event == RunEvent.run_completed.value:
                if isinstance(event.content, str):
                    final_content = event.content
                elif hasattr(event.content, "model_dump_json"):
                    final_content = event.content.model_dump_json()
                metrics = getattr(event, "metrics", None)
                if metrics is not None:
                    record.input_tokens = metrics.input_tokens or 0
                    record.output_tokens = metrics.output_tokens or 0
                    record.total_tokens = metrics.total_tokens or 0
                    record.spans.extend(_model_spans(metrics, record.started_at))
                    # Custo do Agno quando ele informa; senão, a soma estimada por modelo.
                    span_costs = [s.cost_usd for s in record.spans if s.type == "GENERATION" and s.cost_usd is not None]
                    record.cost_usd = metrics.cost if metrics.cost is not None else (sum(span_costs) if span_costs else None)
            elif event.event == RunEvent.run_error.value:
                record.status = "error"
                record.status_message = event.content or RUN_FAILED
            elif event.event in (RunEvent.tool_call_completed.value, RunEvent.tool_call_error.value):
                if getattr(event, "tool", None) is not None:
                    record.spans.append(_tool_span(event, datetime.now(timezone.utc)))
            if record.model is None and isinstance(getattr(event, "model", None), str):
                record.model = event.model or None
            yield event
    except (asyncio.CancelledError, GeneratorExit):
        record.status = "interrupted"
        record.status_message = RUN_INTERRUPTED
        raise
    except RunTimeoutError:
        raise
    except Exception as exc:
        record.status = "error"
        record.status_message = str(exc) or type(exc).__name__
        raise
    finally:
        try:
            await events.aclose()
        except (Exception, asyncio.CancelledError):
            pass  # o run do Agno já terminou ou foi cancelado; fechar é só limpeza
        output = final_content if final_content is not None else "".join(chunks)
        record.output = output or None
        if record.model is None:
            record.model = next((span.model for span in record.spans if span.model), None)
        _add_extra_spans(record, run.extra_spans)
        record.ended_at = datetime.now(timezone.utc)
        run_store.record_run(record)


def _add_extra_spans(record: Any, spans: tuple[Any, ...]) -> None:
    """Põe na frente os spans de fora do Agno e soma os tokens e o custo deles."""
    if not spans:
        return
    record.spans[:0] = list(spans)
    record.input_tokens += sum(s.input_tokens for s in spans)
    record.output_tokens += sum(s.output_tokens for s in spans)
    record.total_tokens += sum(s.total_tokens for s in spans)
    costs = [s.cost_usd for s in spans if s.cost_usd is not None]
    if costs:
        record.cost_usd = (record.cost_usd or 0.0) + sum(costs)


def record_run_without_model(
    run: RunContext, output: str | None, *, status: str = "success", status_message: str | None = None
) -> None:
    """Grava uma execução cuja resposta o servidor montou sem chamar o modelo para
    redigi-la (a confirmação de um agente procedural), ou que falhou antes de chegar
    ao modelo da resposta (a extração deu erro). Ela precisa existir como as outras:
    é dela que vêm a transcrição do feedback e a conversa nos Logs."""
    from agent_service.observability import run_store

    started = datetime.now(timezone.utc)
    record = run_store.RunRecord(
        run_id=run.run_id,
        trace_id=run.trace_id,
        agent_type=run.agent_type,
        agent_name=run.agent_name,
        prompt_version=run.prompt_version,
        agent_version=run.agent_version,
        config_hash=run.config_hash,
        endpoint=run.endpoint,
        user_id=run.user_id,
        session_id=run.session_id,
        message=run.message,
        started_at=min((s.started_at for s in run.extra_spans), default=started),
        metadata=dict(run.metadata),
        input={"message": run.message, "dependencies": run.dependencies},
        output=output,
        status=status,
        status_message=status_message,
    )
    _add_extra_spans(record, run.extra_spans)
    record.model = next((s.model for s in record.spans if s.model), None)
    record.ended_at = datetime.now(timezone.utc)
    run_store.record_run(record)


async def traced_run_events(agent: Agent, run: RunContext) -> AsyncIterator[RunOutputEvent]:
    """Eventos do run, registrados no trace store local (e no Langfuse, se ligado).
    Consuma dentro de `contextlib.aclosing`.

    Com o Langfuse, o run executa numa task própria que alimenta uma fila: o
    contexto do OpenTelemetry (a observação raiz) é aberto e fechado dentro dessa
    task, em vez de atravessar os `yield` deste generator — o que quebraria a
    árvore de spans quando o consumidor (ex. o SSE) intercala seu próprio código.
    """
    client = _client
    if client is None:
        async with aclosing(_recorded_events(agent, run)) as events:
            async for event in events:
                yield event
        return

    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)
    producer = asyncio.create_task(_produce(client, agent, run, queue))
    try:
        while True:
            item = await queue.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        # Consumidor saiu antes do fim (cliente desconectou): cancela o run, que
        # fecha a observação raiz como interrompida.
        if not producer.done():
            producer.cancel()


async def _produce(client: "Langfuse", agent: Agent, run: RunContext, queue: asyncio.Queue[Any]) -> None:
    from langfuse import propagate_attributes

    chunks: list[str] = []
    final_content: str | None = None
    error: str | None = None
    try:
        with (
            propagate_attributes(
                user_id=run.user_id,
                session_id=run.session_id,
                trace_name=run.agent_type,
                tags=[run.agent_type, run.endpoint],
                version=f"prompt-v{run.prompt_version}",
                metadata={
                    "agent_type": run.agent_type,
                    "agent_name": run.agent_name,
                    "prompt_version": str(run.prompt_version),
                    "agent_version": str(run.agent_version),
                    "config_hash": str(run.config_hash),
                    "endpoint": run.endpoint,
                    "run_id": run.run_id,
                },
            ),
            client.start_as_current_observation(
                trace_context={"trace_id": run.trace_id},
                name=run.endpoint,
                input={
                    "message": run.message,
                    "dependencies": run.dependencies,
                    **({"attachments": list(run.attachments) or run.attachment_counts} if run.attachment_counts else {}),
                },
            ) as root,
        ):
            try:
                async with aclosing(_recorded_events(agent, run)) as events:
                    async for event in events:
                        if event.event == RunEvent.run_content.value and isinstance(event.content, str):
                            chunks.append(event.content)
                        elif event.event == RunEvent.run_completed.value and isinstance(event.content, str):
                            final_content = event.content
                        elif event.event == RunEvent.run_error.value:
                            error = event.content or RUN_FAILED
                        await queue.put(event)
            except asyncio.CancelledError:
                root.update(output="".join(chunks) or None, level="WARNING", status_message=RUN_INTERRUPTED)
                raise
            except Exception as exc:
                root.update(level="ERROR", status_message=str(exc) or type(exc).__name__)
                raise

            output = final_content if final_content is not None else "".join(chunks)
            if error:
                root.update(output=output or None, level="ERROR", status_message=error)
            else:
                root.update(output=output)
    except asyncio.CancelledError:
        # Cancelamento vindo de fora (shutdown do servidor): sinaliza o fim para
        # não deixar o consumidor esperando um evento que não vem mais.
        if not queue.full():
            queue.put_nowait(_END)
        raise
    except Exception as exc:
        await queue.put(exc)
        return
    await queue.put(_END)


def score_run(
    *,
    run_id: str,
    name: str,
    value: float,
    comment: str | None = None,
    user_id: str | None = None,
) -> str:
    """Registra um score do run no trace store local (e no Langfuse, se ligado). Devolve o `trace_id`."""
    from agent_service.observability import run_store

    trace_id = trace_id_for_run(run_id)
    run_store.save_score(run_id=run_id, name=name, value=value, comment=comment, user_id=user_id)
    client = _client
    if client is not None:
        client.create_score(
            trace_id=trace_id,
            name=name,
            value=value,
            data_type="BOOLEAN" if name == FEEDBACK else "NUMERIC",
            comment=comment,
            # Um score por trace/nome/usuário: votar de novo substitui o voto anterior.
            score_id=trace_id_for_run(f"{trace_id}:{name}:{user_id}") if user_id else None,
            metadata={"user_id": user_id} if user_id else None,
        )
    return trace_id
