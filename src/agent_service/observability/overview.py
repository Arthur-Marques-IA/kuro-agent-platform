"""O panorama dos Logs: as perguntas que o dashboard responde, num pedido só.

1. Tem algo quebrado agora? — erros, interrompidos e runs com tool falhando,
   comparados com o período anterior, e quais tools estão falhando e por quê;
2. Para onde vai o dinheiro? — custo por agente, por execução e por conversa;
3. A última versão do agente melhorou? — as mesmas métricas por `agent_version`;
4. Como isso evolui? — série no tempo com os dias (ou horas, ou semanas) sem
   execução preenchidos com zero, no fuso de quem olha.

Só no trace store local. O banco agrega por hora UTC (expressão que vale no
Postgres e no SQLite); daqui para frente é Python: converter cada hora para o fuso
pedido e juntar no balde da granularidade. O resultado tem no máximo agentes ×
versões × horas linhas, então não cresce com o número de execuções. A latência
p95 precisa dos valores, e esses vêm uma coluna só por run.

Os testes do Playground (`metadata.dry_run = "true"`) ficam de fora por padrão:
são tráfego de quem está configurando, não de cliente.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import and_, case, exists, func, not_, select

from agent_service.db import get_db
from agent_service.observability.run_store import FEEDBACK, run_metadata, run_scores, run_spans, runs

Granularity = Literal["hour", "day", "week"]
DEFAULT_TIMEZONE = "America/Sao_Paulo"


class OverviewTotals(BaseModel):
    runs: int = 0
    success: int = 0
    errors: int = 0
    interrupted: int = 0
    tool_failure_runs: int = 0
    """Runs que terminaram como `success` mas com pelo menos uma tool falhando."""
    tool_calls: int = 0
    tool_failures: int = 0
    sessions: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    """Custo dos runs e, no panorama geral (sem `agent_type`), também o da coleta de sites."""
    ingestion_cost_usd: float | None = None
    """Parte de `cost_usd` que foi o agente formatador das fontes de site
    (`knowledge_sync_costs`). Não é de agente nenhum: some quando há `agent_type`."""
    ingestion_tokens: int = 0
    """Tokens do formatador — fora de `total_tokens`, que é só dos runs."""
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    feedback_up: int = 0
    feedback_down: int = 0


class OverviewBucket(BaseModel):
    start: str
    """Início do balde no fuso pedido, ISO 8601 com offset."""
    runs: int = 0
    success: int = 0
    tool_failure_runs: int = 0
    errors: int = 0
    """`error` + `interrupted`."""
    cost_usd: float = 0.0
    """Inclui `ingestion_cost_usd`."""
    ingestion_cost_usd: float = 0.0
    total_tokens: int = 0
    p95_latency_ms: float | None = None


class VersionRow(BaseModel):
    agent_version: int | None
    runs: int
    error_rate: float
    tool_failure_rate: float
    cost_per_run_usd: float | None = None
    p95_latency_ms: float | None = None
    feedback_up: int = 0
    feedback_down: int = 0
    first_seen: datetime
    last_seen: datetime


class AgentRow(BaseModel):
    agent_type: str
    agent_name: str | None = None
    totals: OverviewTotals
    cost_per_session_usd: float | None = None
    last_run_at: datetime
    trend: list[int]
    """Execuções por balde, alinhado com `Overview.buckets`."""
    versions: list[VersionRow]
    """As versões da configuração que rodaram no período, da mais nova para a mais antiga."""


class ToolFailureRow(BaseModel):
    tool_name: str
    failure: str
    count: int
    last_at: datetime
    agent_types: list[str]
    http_status: list[int] = []


class Overview(BaseModel):
    since: datetime
    until: datetime
    timezone: str
    granularity: Granularity
    include_dry_run: bool
    buckets: list[OverviewBucket]
    totals: OverviewTotals
    previous: OverviewTotals | None = None
    """O mesmo intervalo, imediatamente antes — a base das variações."""
    agents: list[AgentRow]
    tool_failures: list[ToolFailureRow]


def auto_granularity(since: datetime, until: datetime) -> Granularity:
    span = until - since
    if span <= timedelta(days=2):
        return "hour"
    if span <= timedelta(days=92):
        return "day"
    return "week"


def _percentile(values: list[float], pct: float) -> float | None:
    """Interpolação linear, como o `percentile_cont` do Postgres."""
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo, hi = math.floor(k), math.ceil(k)
    value = ordered[lo] if lo == hi else ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)
    return round(value, 1)


def _hour_expr(dialect: str) -> Any:
    if dialect == "postgresql":
        return func.date_trunc("hour", func.timezone("UTC", runs.c.started_at))
    return func.strftime("%Y-%m-%d %H:00:00", runs.c.started_at)


def _as_utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _bucket_start(moment: datetime, granularity: Granularity, tz: ZoneInfo) -> datetime:
    local = moment.astimezone(tz)
    if granularity == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "day":
        return day
    return day - timedelta(days=day.weekday())  # semana começa na segunda


def _bucket_starts(since: datetime, until: datetime, granularity: Granularity, tz: ZoneInfo) -> list[datetime]:
    starts = []
    current = _bucket_start(since, granularity, tz)
    step = {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(weeks=1)}[granularity]
    while current < until:
        starts.append(current)
        # Somar no relógio local e normalizar: atravessa mudança de horário sem pular balde.
        current = (current.replace(tzinfo=None) + step).replace(tzinfo=tz)
    return starts


def _conditions(since: datetime, until: datetime, agent_type: str | None, include_dry_run: bool) -> list[Any]:
    conditions = [runs.c.started_at >= since, runs.c.started_at < until]
    if agent_type:
        conditions.append(runs.c.agent_type == agent_type)
    if not include_dry_run:
        conditions.append(
            not_(
                exists().where(
                    run_metadata.c.run_id == runs.c.run_id,
                    run_metadata.c.key == "dry_run",
                    run_metadata.c.value == "true",
                )
            )
        )
    return conditions


def _totals(conn: Any, conditions: list[Any]) -> OverviewTotals:
    is_status = lambda s: func.sum(case((runs.c.status == s, 1), else_=0))  # noqa: E731
    row = conn.execute(
        select(
            func.count().label("runs"),
            is_status("success").label("success"),
            is_status("error").label("errors"),
            is_status("interrupted").label("interrupted"),
            func.sum(case((and_(runs.c.status == "success", runs.c.tool_failures > 0), 1), else_=0)).label("tfr"),
            func.coalesce(func.sum(runs.c.tool_calls), 0).label("tool_calls"),
            func.coalesce(func.sum(runs.c.tool_failures), 0).label("tool_failures"),
            func.count(func.distinct(runs.c.session_id)).label("sessions"),
            func.coalesce(func.sum(runs.c.total_tokens), 0).label("tokens"),
            func.sum(runs.c.cost_usd).label("cost"),
        ).where(*conditions)
    ).one()
    latencies = [v for (v,) in conn.execute(select(runs.c.latency_ms).where(*conditions, runs.c.latency_ms.is_not(None)))]
    up, down = _feedback(conn, conditions)
    return OverviewTotals(
        runs=row.runs,
        success=row.success or 0,
        errors=row.errors or 0,
        interrupted=row.interrupted or 0,
        tool_failure_runs=row.tfr or 0,
        tool_calls=row.tool_calls,
        tool_failures=row.tool_failures,
        sessions=row.sessions,
        total_tokens=row.tokens,
        cost_usd=row.cost,
        p50_latency_ms=_percentile(latencies, 0.5),
        p95_latency_ms=_percentile(latencies, 0.95),
        feedback_up=up,
        feedback_down=down,
    )


def _feedback(conn: Any, conditions: list[Any], group: Any = None) -> Any:
    """Votos de feedback dos runs do recorte; com `group`, um dicionário por chave."""
    columns = [run_scores.c.value] if group is None else [*group, run_scores.c.value]
    rows = conn.execute(
        select(*columns)
        .select_from(run_scores.join(runs, runs.c.run_id == run_scores.c.run_id))
        .where(run_scores.c.name == FEEDBACK, *conditions)
    )
    if group is None:
        up = down = 0
        for (value,) in rows:
            up, down = (up + 1, down) if value else (up, down + 1)
        return up, down
    votes: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for *key, value in rows:
        votes[tuple(key)][0 if value else 1] += 1
    return votes


def _ingestion(since: datetime, until: datetime) -> list[dict[str, Any]]:
    from agent_service.documents import sources_store

    return sources_store.list_sync_costs(since, until)


def _add_ingestion(totals: OverviewTotals, rows: list[dict[str, Any]]) -> None:
    """Soma o gasto do formatador de sites ao total (o custo) e o mostra à parte."""
    if not rows:
        return
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    totals.ingestion_tokens = sum(r["input_tokens"] + r["output_tokens"] for r in rows)
    if costs:
        totals.ingestion_cost_usd = round(sum(costs), 8)
        totals.cost_usd = (totals.cost_usd or 0.0) + totals.ingestion_cost_usd


def build_overview(
    *,
    since: datetime | None,
    until: datetime | None,
    agent_type: str | None = None,
    include_dry_run: bool = False,
    timezone_name: str = DEFAULT_TIMEZONE,
    granularity: Granularity | None = None,
) -> Overview:
    tz = ZoneInfo(timezone_name)
    engine = get_db().db_engine
    until = _as_utc(until) if until else datetime.now(timezone.utc)
    with engine.connect() as conn:
        if since is None:
            first = conn.execute(
                select(func.min(runs.c.started_at)).where(*_conditions(datetime.min.replace(tzinfo=timezone.utc), until, agent_type, include_dry_run))
            ).scalar_one()
            since = _as_utc(first) if first else until - timedelta(days=7)
        since = _as_utc(since)
        granularity = granularity or auto_granularity(since, until)
        conditions = _conditions(since, until, agent_type, include_dry_run)

        totals = _totals(conn, conditions)
        previous = _totals(conn, _conditions(since - (until - since), since, agent_type, include_dry_run))
        # A coleta de sites não é de agente nenhum: só entra no panorama geral.
        ingestion = [] if agent_type else _ingestion(since, until)
        _add_ingestion(totals, ingestion)
        if not agent_type:
            _add_ingestion(previous, _ingestion(since - (until - since), since))

        starts = _bucket_starts(since, until, granularity, tz)
        index = {start: i for i, start in enumerate(starts)}
        buckets = [OverviewBucket(start=start.isoformat()) for start in starts]

        hour = _hour_expr(engine.dialect.name).label("hour")
        failed = case((runs.c.status.in_(("error", "interrupted")), 1), else_=0)
        tool_failed = case((and_(runs.c.status == "success", runs.c.tool_failures > 0), 1), else_=0)
        grouped = conn.execute(
            select(
                runs.c.agent_type,
                runs.c.agent_version,
                hour,
                func.count().label("runs"),
                func.sum(failed).label("errors"),
                func.sum(tool_failed).label("tfr"),
                func.coalesce(func.sum(runs.c.cost_usd), 0).label("cost"),
                func.coalesce(func.sum(runs.c.total_tokens), 0).label("tokens"),
                func.max(runs.c.agent_name).label("agent_name"),
                func.min(runs.c.started_at).label("first"),
                func.max(runs.c.started_at).label("last"),
            )
            .where(*conditions)
            .group_by(runs.c.agent_type, runs.c.agent_version, hour)
        ).all()

        per_agent: dict[str, dict[str, Any]] = {}
        per_version: dict[tuple[str, int | None], dict[str, Any]] = {}
        for row in grouped:
            position = index.get(_bucket_start(_as_utc(row.hour), granularity, tz))
            if position is not None:
                bucket = buckets[position]
                bucket.runs += row.runs
                bucket.errors += row.errors or 0
                bucket.tool_failure_runs += row.tfr or 0
                bucket.success += row.runs - (row.errors or 0) - (row.tfr or 0)
                bucket.cost_usd += float(row.cost or 0)
                bucket.total_tokens += int(row.tokens or 0)
            agent = per_agent.setdefault(row.agent_type, {"name": row.agent_name, "trend": [0] * len(starts), "last": None})
            if position is not None:
                agent["trend"][position] += row.runs
            agent["name"] = agent["name"] or row.agent_name
            agent["last"] = max(filter(None, [agent["last"], _as_utc(row.last)]))
            version = per_version.setdefault(
                (row.agent_type, row.agent_version),
                {"runs": 0, "errors": 0, "tfr": 0, "cost": 0.0, "first": _as_utc(row.first), "last": _as_utc(row.last)},
            )
            version["runs"] += row.runs
            version["errors"] += row.errors or 0
            version["tfr"] += row.tfr or 0
            version["cost"] += float(row.cost or 0)
            version["first"] = min(version["first"], _as_utc(row.first))
            version["last"] = max(version["last"], _as_utc(row.last))

        # Latência: p95 por balde e por versão, dos valores de cada run.
        bucket_latency: dict[int, list[float]] = defaultdict(list)
        version_latency: dict[tuple[str, int | None], list[float]] = defaultdict(list)
        for agent_name, agent_version, started_at, latency in conn.execute(
            select(runs.c.agent_type, runs.c.agent_version, runs.c.started_at, runs.c.latency_ms).where(
                *conditions, runs.c.latency_ms.is_not(None)
            )
        ):
            position = index.get(_bucket_start(_as_utc(started_at), granularity, tz))
            if position is not None:
                bucket_latency[position].append(latency)
            version_latency[(agent_name, agent_version)].append(latency)
        for position, values in bucket_latency.items():
            buckets[position].p95_latency_ms = _percentile(values, 0.95)
        for row in ingestion:
            position = index.get(_bucket_start(_as_utc(row["ended_at"]), granularity, tz))
            if position is not None and row["cost_usd"] is not None:
                buckets[position].ingestion_cost_usd += row["cost_usd"]
                buckets[position].cost_usd += row["cost_usd"]
        for bucket in buckets:
            bucket.cost_usd = round(bucket.cost_usd, 6)
            bucket.ingestion_cost_usd = round(bucket.ingestion_cost_usd, 6)

        version_votes = _feedback(conn, conditions, group=[runs.c.agent_type, runs.c.agent_version])

        agents: list[AgentRow] = []
        for agent_type_key, agent in per_agent.items():
            agent_conditions = [*conditions, runs.c.agent_type == agent_type_key]
            agent_totals = _totals(conn, agent_conditions)
            versions = [
                VersionRow(
                    agent_version=version_key,
                    runs=v["runs"],
                    error_rate=v["errors"] / v["runs"],
                    tool_failure_rate=v["tfr"] / v["runs"],
                    cost_per_run_usd=v["cost"] / v["runs"] if v["cost"] else None,
                    p95_latency_ms=_percentile(version_latency[(agent_type_key, version_key)], 0.95),
                    feedback_up=version_votes.get((agent_type_key, version_key), [0, 0])[0],
                    feedback_down=version_votes.get((agent_type_key, version_key), [0, 0])[1],
                    first_seen=v["first"],
                    last_seen=v["last"],
                )
                for (agent_key, version_key), v in per_version.items()
                if agent_key == agent_type_key
            ]
            versions.sort(key=lambda v: (v.agent_version is not None, v.agent_version or 0, v.last_seen), reverse=True)
            agents.append(
                AgentRow(
                    agent_type=agent_type_key,
                    agent_name=agent["name"],
                    totals=agent_totals,
                    cost_per_session_usd=(
                        agent_totals.cost_usd / agent_totals.sessions
                        if agent_totals.cost_usd and agent_totals.sessions
                        else None
                    ),
                    last_run_at=agent["last"],
                    trend=agent["trend"],
                    versions=versions,
                )
            )
        agents.sort(key=lambda a: a.totals.runs, reverse=True)

        tool_failures = _tool_failures(conn, conditions)

    return Overview(
        since=since,
        until=until,
        timezone=timezone_name,
        granularity=granularity,
        include_dry_run=include_dry_run,
        buckets=buckets,
        totals=totals,
        previous=previous if previous.runs or previous.ingestion_tokens else None,
        agents=agents,
        tool_failures=tool_failures,
    )


def _tool_failures(conn: Any, conditions: list[Any]) -> list[ToolFailureRow]:
    """Quais tools falharam no recorte, por tipo de falha. Poucas linhas: só spans de erro."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for name, metadata, started_at, agent_type in conn.execute(
        select(run_spans.c.name, run_spans.c.metadata, runs.c.started_at, runs.c.agent_type)
        .select_from(run_spans.join(runs, runs.c.run_id == run_spans.c.run_id))
        # WARNING entra também: o 404 não é falha, mas quem cuida da tool quer ver.
        .where(run_spans.c.type == "TOOL", run_spans.c.level.in_(("ERROR", "WARNING")), *conditions)
    ):
        metadata = metadata or {}
        key = (name, metadata.get("failure") or "exception")
        entry = grouped.setdefault(key, {"count": 0, "last": None, "agents": set(), "http": set()})
        entry["count"] += 1
        entry["last"] = max(filter(None, [entry["last"], _as_utc(started_at)]))
        entry["agents"].add(agent_type)
        if metadata.get("http_status"):
            entry["http"].add(int(metadata["http_status"]))
    rows = [
        ToolFailureRow(
            tool_name=name,
            failure=failure,
            count=e["count"],
            last_at=e["last"],
            agent_types=sorted(e["agents"]),
            http_status=sorted(e["http"]),
        )
        for (name, failure), e in grouped.items()
    ]
    return sorted(rows, key=lambda r: (r.count, r.last_at), reverse=True)

