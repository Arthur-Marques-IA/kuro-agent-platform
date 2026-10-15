"""Trace store local: cada execução gravada no próprio Postgres do serviço.

É a fonte das rotas `/observability/*` e do `kuro runs` — funciona com o
Langfuse desligado, que passa a ser só um exportador opcional para análise
profunda (ver `tracing.py`). Três tabelas, fora do namespace `agno_*`:

- `runs`: uma linha por execução (`/chat`, `/chat/stream`, `/analyze`), com
  agente, versão do prompt, status terminal, tokens, custo e latência;
- `run_spans`: o que aconteceu dentro dela — uma linha por modelo chamado
  (tokens/custo por modelo, das métricas do Agno) e uma por tool call;
- `run_scores`: feedback e notas de avaliação (um voto por run/nome/usuário).

A gravação sai do caminho da resposta: `record_run` manda o INSERT para uma
thread (`_submit`) e não espera — um banco lento atrasa o registro, não o
usuário. Sessões e estatísticas são `GROUP BY` sobre o histórico inteiro,
sem a varredura em memória que a API do Langfuse obrigava.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    case,
    delete,
    exists,
    func,
    insert,
    not_,
    or_,
    select,
)

from agent_service.db import get_db
from agent_service.tools.failures import complexity
from agent_service.observability.trace_store import (
    RunPage,
    RunQuery,
    RunStats,
    RunTrace,
    ScoreOut,
    SessionPage,
    SessionSummary,
    SpanOut,
    StatsBucket,
    RunSummary,
)

logger = logging.getLogger(__name__)

FEEDBACK = "feedback"
DEFAULT_TENANT = "default"

metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("trace_id", String, nullable=False),
    Column("tenant_id", String, nullable=False, default=DEFAULT_TENANT, index=True),
    Column("agent_type", String, nullable=False, index=True),
    Column("agent_name", String, nullable=True),
    Column("prompt_version", Integer, nullable=True),
    # Configuração que rodou (instructions, modelo, tools, schema, nota de
    # feedback): hash e número da versão em `agent_versions` (`agents/versions.py`).
    Column("config_hash", String, nullable=True),
    Column("agent_version", Integer, nullable=True),
    Column("endpoint", String, nullable=True),
    Column("user_id", String, nullable=True),
    Column("session_id", String, nullable=True, index=True),
    Column("status", String, nullable=False),
    Column("status_message", Text, nullable=True),
    Column("message", Text, nullable=True),
    Column("output", Text, nullable=True),
    Column("model", String, nullable=True),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("total_tokens", Integer, nullable=False, default=0),
    Column("cost_usd", Float, nullable=True),
    Column("latency_ms", Float, nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, index=True),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    # Resumo das tools do run (ver `tools/failures.py`): quantas chamadas, quantas
    # falharam (o run continua `success` se o agente respondeu) e a complexidade
    # 1–3 pelas tools de negócio distintas. `None` em runs anteriores à 0006 sem tools.
    Column("tool_calls", Integer, nullable=True),
    Column("tool_failures", Integer, nullable=True),
    Column("complexity", Integer, nullable=True),
)

run_spans = Table(
    "run_spans",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, nullable=False, index=True),
    Column("type", String, nullable=False),
    Column("name", String, nullable=False),
    Column("level", String, nullable=False, default="DEFAULT"),
    Column("status_message", Text, nullable=True),
    Column("input", JSON, nullable=True),
    Column("output", JSON, nullable=True),
    Column("model", String, nullable=True),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("total_tokens", Integer, nullable=False, default=0),
    Column("cost_usd", Float, nullable=True),
    Column("latency_ms", Float, nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("metadata", JSON, nullable=True),
)

run_metadata = Table(
    "run_metadata",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("value", String, nullable=False),
)

run_references = Table(
    "run_references",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("reference", JSON, nullable=False),
    Column("source", String, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

run_scores = Table(
    "run_scores",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, nullable=False, index=True),
    Column("name", String, nullable=False),
    Column("value", Float, nullable=False),
    Column("data_type", String, nullable=False),
    Column("comment", Text, nullable=True),
    Column("user_id", String, nullable=True),
    Column("timestamp", DateTime(timezone=True), nullable=False),
)


# -- gravação ------------------------------------------------------------------


@dataclass
class SpanRecord:
    type: str
    name: str
    started_at: datetime
    ended_at: datetime | None = None
    level: str = "DEFAULT"
    status_message: str | None = None
    input: Any = None
    output: Any = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunRecord:
    run_id: str
    trace_id: str
    agent_type: str
    agent_name: str | None
    prompt_version: int | None
    endpoint: str
    user_id: str | None
    session_id: str | None
    message: str | None
    started_at: datetime
    input: dict[str, Any] = field(default_factory=dict)
    status: str = "success"
    status_message: str | None = None
    output: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    ended_at: datetime | None = None
    spans: list[SpanRecord] = field(default_factory=list)
    tenant_id: str = DEFAULT_TENANT
    agent_version: int | None = None
    config_hash: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


def _write_run(record: RunRecord) -> None:
    ended_at = record.ended_at or datetime.now(timezone.utc)
    latency = round((ended_at - record.started_at).total_seconds() * 1000, 1)
    root_id = f"{record.run_id}:root"
    span_rows = [
        {
            "id": root_id,
            "run_id": record.run_id,
            "type": "SPAN",
            "name": record.endpoint,
            "level": _LEVEL_BY_STATUS[record.status],
            "status_message": record.status_message,
            "input": record.input,
            "output": record.output,
            "model": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": None,
            "latency_ms": latency,
            "started_at": record.started_at,
            "ended_at": ended_at,
            "metadata": {"parent_id": None},
        }
    ]
    for index, span in enumerate(record.spans):
        span_rows.append(
            {
                "id": f"{record.run_id}:{index}",
                "run_id": record.run_id,
                "type": span.type,
                "name": span.name,
                "level": span.level,
                "status_message": span.status_message,
                "input": span.input,
                "output": span.output,
                "model": span.model,
                "input_tokens": span.input_tokens,
                "output_tokens": span.output_tokens,
                "total_tokens": span.total_tokens,
                "cost_usd": span.cost_usd,
                "latency_ms": span.latency_ms,
                "started_at": span.started_at,
                "ended_at": span.ended_at,
                "metadata": {"parent_id": root_id, **span.metadata},
            }
        )
    with get_db().db_engine.begin() as conn:
        conn.execute(
            insert(runs).values(
                run_id=record.run_id,
                trace_id=record.trace_id,
                tenant_id=record.tenant_id,
                agent_type=record.agent_type,
                agent_name=record.agent_name,
                prompt_version=record.prompt_version,
                config_hash=record.config_hash,
                agent_version=record.agent_version,
                endpoint=record.endpoint,
                user_id=record.user_id,
                session_id=record.session_id,
                status=record.status,
                status_message=record.status_message,
                message=record.message,
                output=record.output,
                model=record.model,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                total_tokens=record.total_tokens,
                cost_usd=record.cost_usd,
                latency_ms=latency,
                started_at=record.started_at,
                ended_at=ended_at,
                **_tool_summary(record.spans),
            )
        )
        conn.execute(insert(run_spans), span_rows)
        if record.metadata:
            conn.execute(
                insert(run_metadata),
                [{"run_id": record.run_id, "key": k, "value": v} for k, v in record.metadata.items()],
            )


def _tool_summary(spans: list[SpanRecord]) -> dict[str, int]:
    tools = [span for span in spans if span.type == "TOOL"]
    return {
        "tool_calls": len(tools),
        "tool_failures": sum(1 for span in tools if span.level == "ERROR"),
        "complexity": complexity([span.name for span in tools]),
    }


def _submit(fn: Callable[[], None]) -> None:
    """Roda `fn` fora do event loop, sem esperar. Os testes trocam por uma chamada direta."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        fn()
        return
    loop.run_in_executor(None, fn)


def record_run(record: RunRecord) -> None:
    """Grava a execução em background. Uma falha aqui vira log — nunca derruba o run."""

    def write() -> None:
        try:
            _write_run(record)
        except Exception:
            logger.exception("Falha ao gravar o run %s no trace store local", record.run_id)

    _submit(write)


def save_score(
    *,
    run_id: str,
    name: str,
    value: float,
    comment: str | None = None,
    user_id: str | None = None,
) -> None:
    """Um score por run/nome/usuário: votar de novo substitui o voto anterior."""
    score_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{name}:{user_id}")) if user_id else str(uuid.uuid4())
    with get_db().db_engine.begin() as conn:
        conn.execute(delete(run_scores).where(run_scores.c.id == score_id))
        conn.execute(
            insert(run_scores).values(
                id=score_id,
                run_id=run_id,
                name=name,
                value=value,
                data_type="BOOLEAN" if name == FEEDBACK else "NUMERIC",
                comment=comment,
                user_id=user_id,
                timestamp=datetime.now(timezone.utc),
            )
        )


# -- leitura -------------------------------------------------------------------

_LEVEL_BY_STATUS = {"success": "DEFAULT", "error": "ERROR", "interrupted": "WARNING"}


def _filters(query: RunQuery, *, with_session: bool = False) -> list[Any]:
    conditions: list[Any] = []
    if query.tenant_id:
        conditions.append(runs.c.tenant_id == query.tenant_id)
    if query.agent_type:
        conditions.append(runs.c.agent_type == query.agent_type)
    if query.agent_types:
        conditions.append(runs.c.agent_type.in_(query.agent_types))
    if query.prompt_version is not None:
        conditions.append(runs.c.prompt_version == query.prompt_version)
    if query.agent_version is not None:
        conditions.append(runs.c.agent_version == query.agent_version)
    if query.status:
        conditions.append(runs.c.status == query.status)
    if query.statuses:
        conditions.append(runs.c.status.in_(query.statuses))
    if query.user_id:
        conditions.append(runs.c.user_id == query.user_id)
    if query.session_id:
        conditions.append(runs.c.session_id == query.session_id)
    if query.since:
        conditions.append(runs.c.started_at >= query.since)
    if query.until:
        conditions.append(runs.c.started_at < query.until)
    if with_session:
        conditions.append(runs.c.session_id.is_not(None))
    if query.search and (term := query.search.strip()):
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        conditions.append(
            or_(
                runs.c.session_id.ilike(like, escape="\\"),
                runs.c.user_id.ilike(like, escape="\\"),
                exists().where(run_metadata.c.run_id == runs.c.run_id, run_metadata.c.value.ilike(like, escape="\\")),
            )
        )
    for key, value in query.metadata:
        conditions.append(
            exists().where(run_metadata.c.run_id == runs.c.run_id, run_metadata.c.key == key, run_metadata.c.value == value)
        )
    conditions.extend(_review_filters(query))
    return conditions


def _review_filters(query: RunQuery) -> list[Any]:
    """Os filtros da fila de revisão. Efeito colateral é resolvido na hora, pela
    classificação atual das tools — classificar uma tool depois vale também para
    os runs antigos."""
    from agent_service.tools.store import tool_definitions

    conditions: list[Any] = []
    if query.complexity:
        conditions.append(runs.c.complexity.in_(query.complexity))
    if query.tool_failed is not None:
        failed = runs.c.tool_failures > 0
        conditions.append(failed if query.tool_failed else or_(runs.c.tool_failures.is_(None), runs.c.tool_failures == 0))
    if query.side_effect is not None:
        called = exists().where(
            run_spans.c.run_id == runs.c.run_id,
            run_spans.c.type == "TOOL",
            run_spans.c.name.in_(select(tool_definitions.c.tool_name).where(tool_definitions.c.side_effect.is_(True))),
        )
        conditions.append(called if query.side_effect else not_(called))
    if query.min_message_chars:
        conditions.append(func.length(func.trim(func.coalesce(runs.c.message, ""))) >= query.min_message_chars)
    if query.feedback in ("up", "down"):
        voted = run_scores.c.value > 0 if query.feedback == "up" else run_scores.c.value <= 0
        conditions.append(exists().where(run_scores.c.run_id == runs.c.run_id, run_scores.c.name == FEEDBACK, voted))
    elif query.feedback == "none":
        conditions.append(not_(exists().where(run_scores.c.run_id == runs.c.run_id, run_scores.c.name == FEEDBACK)))
    if query.exclude_dry_run:
        conditions.append(
            not_(
                exists().where(
                    run_metadata.c.run_id == runs.c.run_id, run_metadata.c.key == "dry_run", run_metadata.c.value == "true"
                )
            )
        )
    return conditions


def _metadata_of(conn: Any, run_ids: list[str]) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {run_id: {} for run_id in run_ids}
    if run_ids:
        for run_id, key, value in conn.execute(
            select(run_metadata.c.run_id, run_metadata.c.key, run_metadata.c.value).where(run_metadata.c.run_id.in_(run_ids))
        ):
            found[run_id][key] = value
    return found


def _encode_cursor(started_at: datetime, run_id: str) -> str:
    return f"{started_at.isoformat()}|{run_id}"


def _decode_cursor(cursor: str) -> tuple[datetime, str] | None:
    started, _, run_id = cursor.partition("|")
    try:
        return datetime.fromisoformat(started), run_id
    except ValueError:
        return None


# Ordenações além da padrão (mais recentes): a coluna e o prefixo do cursor, que carrega o valor
# do último item — o cursor de uma ordenação não serve para outra.
_SORT_COLUMNS = {"cost": runs.c.cost_usd, "latency": runs.c.latency_ms}


def _encode_sort_cursor(sort: str, value: float, run_id: str) -> str:
    return f"{sort}:{value!r}|{run_id}"


def _decode_sort_cursor(sort: str, cursor: str) -> tuple[float, str] | None:
    prefix, _, rest = cursor.partition(":")
    value, _, run_id = rest.partition("|")
    if prefix != sort or not run_id:
        return None
    try:
        return float(value), run_id
    except ValueError:
        return None


def _feedback_votes(conn: Any, run_ids: list[str]) -> dict[str, list[int]]:
    votes = {run_id: [0, 0] for run_id in run_ids}
    if not run_ids:
        return votes
    rows = conn.execute(
        select(run_scores.c.run_id, run_scores.c.value).where(
            run_scores.c.name == FEEDBACK, run_scores.c.run_id.in_(run_ids)
        )
    )
    for run_id, value in rows:
        votes[run_id][0 if value else 1] += 1
    return votes


def _attachments_of(conn: Any, run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Os anexos de cada run, lidos da entrada da span raiz. Runs antigos só têm a
    contagem (`{"files": 1}`): viram itens sem nome, para a mensagem ao menos
    mostrar que houve anexo."""
    found: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in run_ids}
    if not run_ids:
        return found
    rows = conn.execute(
        select(run_spans.c.run_id, run_spans.c.input).where(run_spans.c.id.in_([f"{r}:root" for r in run_ids]))
    )
    for run_id, span_input in rows:
        attachments = (span_input or {}).get("attachments") if isinstance(span_input, dict) else None
        if isinstance(attachments, list):
            found[run_id] = [a for a in attachments if isinstance(a, dict)]
        elif isinstance(attachments, dict):
            kinds = {"images": "image", "audio": "audio", "videos": "video", "files": "file"}
            found[run_id] = [
                {"filename": None, "mime_type": None, "kind": kinds.get(k, "file")}
                for k, n in attachments.items()
                for _ in range(int(n or 0))
            ]
    return found


def _summary(
    row: Any,
    votes: list[int] | None = None,
    meta: dict[str, str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> RunSummary:
    up, down = votes or (0, 0)
    return RunSummary(
        run_id=row.run_id,
        trace_id=row.trace_id,
        agent_type=row.agent_type,
        agent_name=row.agent_name,
        prompt_version=row.prompt_version,
        agent_version=row.agent_version,
        config_hash=row.config_hash,
        endpoint=row.endpoint,
        user_id=row.user_id,
        session_id=row.session_id,
        started_at=row.started_at,
        ended_at=row.ended_at,
        latency_ms=row.latency_ms,
        status=row.status,
        status_message=row.status_message,
        message=row.message,
        output=row.output,
        model=row.model,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        total_tokens=row.total_tokens,
        cost_usd=row.cost_usd,
        feedback_up=up,
        feedback_down=down,
        metadata=meta or {},
        attachments=attachments or [],
        tool_calls=row.tool_calls,
        tool_failures=row.tool_failures,
        complexity=row.complexity,
    )


class DbTraceStore:
    """`TraceStore` sobre as tabelas acima — o backend padrão do serviço."""

    def list_runs(self, query: RunQuery) -> RunPage:
        stmt = select(runs).where(*_filters(query))
        if query.sample:
            # Amostra: o que nenhum filtro apontaria também precisa ser visto às vezes.
            with get_db().db_engine.connect() as conn:
                page = list(conn.execute(stmt.order_by(func.random()).limit(query.sample)))
                votes = _feedback_votes(conn, [r.run_id for r in page])
                meta = _metadata_of(conn, [r.run_id for r in page])
                files = _attachments_of(conn, [r.run_id for r in page])
            return RunPage(items=[_summary(r, votes[r.run_id], meta[r.run_id], files[r.run_id]) for r in page])
        column = _SORT_COLUMNS.get(query.sort)
        if column is not None:
            # As mais caras (ou lentas) primeiro. Run sem o valor (custo desconhecido) não tem lugar
            # nessa fila: fica de fora, e o cursor de chave nunca precisa comparar com NULL.
            stmt = stmt.where(column.is_not(None))
            if query.cursor and (sort_position := _decode_sort_cursor(query.sort, query.cursor)):
                value, run_id = sort_position
                stmt = stmt.where(or_(column < value, and_(column == value, runs.c.run_id < run_id)))
            stmt = stmt.order_by(column.desc(), runs.c.run_id.desc())
        else:
            if query.cursor and (position := _decode_cursor(query.cursor)):
                started_at, run_id = position
                stmt = stmt.where(
                    or_(runs.c.started_at < started_at, and_(runs.c.started_at == started_at, runs.c.run_id < run_id))
                )
            stmt = stmt.order_by(runs.c.started_at.desc(), runs.c.run_id.desc())
        stmt = stmt.limit(query.limit + 1)
        with get_db().db_engine.connect() as conn:
            rows = list(conn.execute(stmt))
            page, more = rows[: query.limit], len(rows) > query.limit
            votes = _feedback_votes(conn, [r.run_id for r in page])
            meta = _metadata_of(conn, [r.run_id for r in page])
            files = _attachments_of(conn, [r.run_id for r in page])
        items = [_summary(r, votes[r.run_id], meta[r.run_id], files[r.run_id]) for r in page]
        next_cursor = None
        if more:
            last = page[-1]
            next_cursor = (
                _encode_sort_cursor(query.sort, last._mapping[column.name], last.run_id)
                if column is not None
                else _encode_cursor(last.started_at, last.run_id)
            )
        return RunPage(items=items, next_cursor=next_cursor)

    def get_run(self, run_id: str, *, tenant_id: str | None = None) -> RunTrace | None:
        conditions = [runs.c.run_id == run_id]
        if tenant_id:
            conditions.append(runs.c.tenant_id == tenant_id)
        with get_db().db_engine.connect() as conn:
            row = conn.execute(select(runs).where(*conditions)).first()
            if row is None:
                return None
            span_rows = list(
                conn.execute(select(run_spans).where(run_spans.c.run_id == run_id).order_by(run_spans.c.started_at))
            )
            score_rows = list(
                conn.execute(select(run_scores).where(run_scores.c.run_id == run_id).order_by(run_scores.c.timestamp))
            )
            run_meta = _metadata_of(conn, [run_id])[run_id]
            run_files = _attachments_of(conn, [run_id])[run_id]
            reference = conn.execute(select(run_references.c.reference).where(run_references.c.run_id == run_id)).scalar()
        scores = [
            ScoreOut(
                id=s.id,
                name=s.name,
                value=bool(s.value) if s.data_type == "BOOLEAN" else s.value,
                data_type=s.data_type,
                source="API",
                comment=s.comment,
                user_id=s.user_id,
                timestamp=s.timestamp,
            )
            for s in score_rows
        ]
        feedback = [s for s in scores if s.name == FEEDBACK]
        up = sum(1 for s in feedback if s.value)
        spans = []
        for s in span_rows:
            meta = dict(s.metadata or {})
            parent_id = meta.pop("parent_id", None)
            spans.append(
                SpanOut(
                    id=s.id,
                    parent_id=parent_id,
                    type=s.type,
                    name=s.name,
                    started_at=s.started_at,
                    ended_at=s.ended_at,
                    latency_ms=s.latency_ms,
                    level=s.level,
                    status_message=s.status_message,
                    input=s.input,
                    output=s.output,
                    model=s.model,
                    input_tokens=s.input_tokens,
                    output_tokens=s.output_tokens,
                    total_tokens=s.total_tokens,
                    cost_usd=s.cost_usd,
                    metadata=meta,
                )
            )
        # Root primeiro, mesmo empatando no horário com o primeiro filho.
        spans.sort(key=lambda s: (s.parent_id is not None, s.started_at))
        return RunTrace(run=_summary(row, [up, len(feedback) - up], run_meta, run_files), spans=spans, scores=scores, reference=reference)

    def list_sessions(self, query: RunQuery) -> SessionPage:
        conditions = _filters(query, with_session=True)
        is_error = case((runs.c.status == "error", 1), else_=0)
        stmt = (
            select(
                runs.c.session_id,
                func.min(runs.c.user_id).label("user_id"),
                func.count().label("run_count"),
                func.min(runs.c.started_at).label("started_at"),
                func.max(runs.c.started_at).label("last_activity"),
                func.coalesce(func.sum(runs.c.total_tokens), 0).label("total_tokens"),
                func.sum(runs.c.cost_usd).label("cost_usd"),
                func.sum(is_error).label("error_count"),
            )
            .where(*conditions)
            .group_by(runs.c.session_id)
            .order_by(func.max(runs.c.started_at).desc())
            .limit(query.limit)
        )
        with get_db().db_engine.connect() as conn:
            groups = list(conn.execute(stmt))
            session_ids = [g.session_id for g in groups]
            agents: dict[str, set[str]] = {sid: set() for sid in session_ids}
            votes: dict[str, list[int]] = {sid: [0, 0] for sid in session_ids}
            scanned = conn.execute(select(func.count()).select_from(runs).where(*conditions)).scalar_one()
            if session_ids:
                in_page = runs.c.session_id.in_(session_ids)
                for sid, agent_type in conn.execute(
                    select(runs.c.session_id, runs.c.agent_type).where(*conditions, in_page).distinct()
                ):
                    agents[sid].add(agent_type)
                for sid, value in conn.execute(
                    select(runs.c.session_id, run_scores.c.value)
                    .join(run_scores, run_scores.c.run_id == runs.c.run_id)
                    .where(*conditions, in_page, run_scores.c.name == FEEDBACK)
                ):
                    votes[sid][0 if value else 1] += 1
        items = [
            SessionSummary(
                session_id=g.session_id,
                user_id=g.user_id,
                agent_types=sorted(agents[g.session_id]),
                run_count=g.run_count,
                started_at=g.started_at,
                last_activity=g.last_activity,
                total_tokens=g.total_tokens,
                cost_usd=g.cost_usd,
                error_count=g.error_count or 0,
                feedback_up=votes[g.session_id][0],
                feedback_down=votes[g.session_id][1],
            )
            for g in groups
        ]
        return SessionPage(items=items, scanned=scanned)

    def get_stats(self, query: RunQuery) -> RunStats:
        conditions = _filters(query)
        day = func.date(runs.c.started_at)
        is_error = case((runs.c.status == "error", 1), else_=0)
        with get_db().db_engine.connect() as conn:
            daily = list(
                conn.execute(
                    select(
                        day.label("day"),
                        func.count().label("runs"),
                        func.sum(is_error).label("errors"),
                        func.coalesce(func.sum(runs.c.total_tokens), 0).label("total_tokens"),
                        func.sum(runs.c.cost_usd).label("cost_usd"),
                    )
                    .where(*conditions)
                    .group_by(day)
                    .order_by(day)
                )
            )
            status_counts = {
                status: count
                for status, count in conn.execute(
                    select(runs.c.status, func.count()).where(*conditions).group_by(runs.c.status)
                )
            }
            avg_latency = conn.execute(select(func.avg(runs.c.latency_ms)).where(*conditions)).scalar_one()
        total_runs = sum(status_counts.values())
        costs = [d.cost_usd for d in daily if d.cost_usd is not None]
        return RunStats(
            buckets=[
                StatsBucket(
                    date=str(d.day)[:10],
                    runs=d.runs,
                    errors=d.errors or 0,
                    total_tokens=d.total_tokens,
                    cost_usd=d.cost_usd,
                )
                for d in daily
            ],
            status_counts=status_counts,
            total_runs=total_runs,
            total_tokens=sum(d.total_tokens for d in daily),
            total_cost_usd=sum(costs) if costs else None,
            avg_latency_ms=round(float(avg_latency), 1) if avg_latency is not None else None,
            scanned=total_runs,
        )


# -- referência (shadow), concordância e export ---------------------------------------


class RunNotFoundError(LookupError):
    pass


def save_reference(run_id: str, reference: dict[str, Any], *, source: str = "legacy") -> None:
    """Grava (ou substitui) a decisão de referência de um run."""
    with get_db().db_engine.begin() as conn:
        if conn.execute(select(runs.c.run_id).where(runs.c.run_id == run_id)).first() is None:
            raise RunNotFoundError(run_id)
        conn.execute(delete(run_references).where(run_references.c.run_id == run_id))
        conn.execute(insert(run_references).values(run_id=run_id, reference=reference, source=source))


def _parse_output(output: str | None) -> dict[str, Any] | None:
    import json

    if not output:
        return None
    try:
        parsed = json.loads(output)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def referenced_runs(query: RunQuery, *, limit: int = 5000) -> list[dict[str, Any]]:
    """Runs com referência gravada, mais recentes primeiro: saída, referência,
    entrada (`message` + `dependencies`) e versão — o material da concordância e do export."""
    stmt = (
        select(runs, run_references.c.reference, run_spans.c.input.label("root_input"))
        .join(run_references, run_references.c.run_id == runs.c.run_id)
        .outerjoin(run_spans, run_spans.c.id == runs.c.run_id + ":root")
        .where(*_filters(query))
        .order_by(runs.c.started_at.desc())
        .limit(limit)
    )
    with get_db().db_engine.connect() as conn:
        rows = list(conn.execute(stmt))
    items = []
    for row in rows:
        root_input = row.root_input or {}
        items.append(
            {
                "run_id": row.run_id,
                "agent_version": row.agent_version,
                "status": row.status,
                "message": row.message,
                "dependencies": root_input.get("dependencies") if isinstance(root_input, dict) else None,
                "output": _parse_output(row.output),
                "reference": row.reference,
                "started_at": row.started_at,
            }
        )
    return items


def session_turns(session_id: str, agent_type: str) -> list[dict[str, Any]]:
    """As trocas de uma sessão com um agente, em ordem: `{run_id, message,
    output, status, status_message}`. É a conversa como ela chegou ao agente,
    de qualquer origem (console, CLI, API) — base da transcrição do feedback."""
    with get_db().db_engine.connect() as conn:
        rows = conn.execute(
            select(runs.c.run_id, runs.c.message, runs.c.output, runs.c.status, runs.c.status_message)
            .where(runs.c.session_id == session_id, runs.c.agent_type == agent_type)
            .order_by(runs.c.started_at)
        ).all()
    return [dict(r._mapping) for r in rows]

