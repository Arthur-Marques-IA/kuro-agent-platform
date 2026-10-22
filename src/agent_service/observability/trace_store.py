"""Leitura dos traces: o console e outros módulos veem os runs sem abrir o Langfuse.

`TraceStore` é o contrato. O backend padrão é o `DbTraceStore` (`run_store.py`,
no Postgres do serviço), que funciona sem Langfuse. `LangfuseTraceStore` o
implementa sobre a API pública do Langfuse v4 (as chaves ficam só no servidor),
para quem escolhe `TRACE_STORE_BACKEND=langfuse`. As respostas usam os
modelos daqui, nunca o JSON do Langfuse — trocar o backend de traces (Cloud,
Postgres próprio...) não muda o contrato de quem consome.

Como os dados estão no Langfuse (ver `tracing.py`):

- cada run tem uma observação raiz (`isRootObservation=true`, com
  `traceName=agent_type`, `version=prompt-v{N}`, `level` = status e o `run_id`
  no metadata). Atenção: a raiz pode ter `parentObservationId` não nulo;
- tokens e custo ficam nos filhos (GENERATION) — por isso somam-se todas as
  observações do trace, não a raiz;
- a ingestão é assíncrona: um run recém-terminado leva alguns segundos para
  aparecer.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel

from agent_service.config import get_settings
from agent_service.observability.tracing import FEEDBACK, get_langfuse, trace_id_for_run

RunStatus = Literal["success", "error", "interrupted"]
RunSort = Literal["recent", "cost", "latency"]
RagFilter = Literal["searched", "empty", "flagged"]

_LEVEL_BY_STATUS: dict[RunStatus, str] = {"success": "DEFAULT", "error": "ERROR", "interrupted": "WARNING"}
_STATUS_BY_LEVEL: dict[str, RunStatus] = {"ERROR": "error", "WARNING": "interrupted"}
# Metadata que o OpenTelemetry/Langfuse anexam a todo span — ruído para quem lê o trace.
_NOISY_METADATA_PREFIXES = ("resourceAttributes.", "scope.", "attributes.langfuse.", "attributes.session.", "attributes.user.")
_OBSERVATIONS_PAGE = 1000
_SCORES_PAGE = 100
_MAX_SCORE_PAGES = 5
_VERSION_PREFIX = "prompt-v"
_AGGREGATE_SCAN_LIMIT = 1000
"""Sessões e estatísticas não paginam de verdade — a API do Langfuse não agrupa
por sessão nem por dia. Varre até este tanto de execuções-raiz (mais recentes
primeiro) e agrega em memória; `scanned` na resposta avisa quando a janela
pode estar cortando dados mais antigos."""


class TraceStoreError(RuntimeError):
    """O backend de traces falhou ou está inacessível."""


class RunSummary(BaseModel):
    run_id: str
    trace_id: str
    agent_type: str
    agent_name: str | None = None
    prompt_version: int | None = None
    agent_version: int | None = None
    """Versão da configuração inteira (`agents/versions.py`) — `None` em runs antigos."""
    config_hash: str | None = None
    knowledge_collection: str | None = None
    """Collection que o agente consultava e a versão da configuração de busca dela
    (`documents/versions.py`) — `None` sem base ou em runs antigos."""
    collection_version: int | None = None
    collection_config_hash: str | None = None
    endpoint: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    environment: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    latency_ms: float | None = None
    status: RunStatus
    status_message: str | None = None
    message: str | None = None
    output: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    feedback_up: int | None = 0
    feedback_down: int | None = 0
    """`None` quando havia avaliações demais para contar com segurança na listagem — veja o trace."""
    metadata: dict[str, str] = {}
    """Correlação mandada por quem chamou (ex.: `conversation_id`)."""
    attachments: list[dict[str, Any]] = []
    """Anexos da mensagem: `{filename, mime_type, kind, size_bytes?, url?}` — sem o conteúdo."""
    tool_calls: int | None = None
    """Chamadas de tool no run. `None` = não informado (backend Langfuse)."""
    tool_failures: int | None = None
    """Chamadas que falharam (HTTP 4xx/5xx, rede, exceção) — o `status` do run não
    muda por isso. `None` = não informado; 0 = nenhuma falha."""
    complexity: int | None = None
    """1 = nenhuma tool de negócio; 2 = uma ou duas distintas; 3 = três ou mais (`tools/failures.py`)."""


class RunPage(BaseModel):
    items: list[RunSummary]
    next_cursor: str | None = None


class SpanOut(BaseModel):
    id: str
    parent_id: str | None = None
    type: str
    name: str
    started_at: datetime
    ended_at: datetime | None = None
    latency_ms: float | None = None
    level: str
    status_message: str | None = None
    input: Any = None
    output: Any = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    metadata: dict[str, Any] = {}


class ScoreOut(BaseModel):
    id: str
    name: str
    value: float | bool | str | None
    data_type: str
    source: str
    comment: str | None = None
    user_id: str | None = None
    timestamp: datetime


class RunTrace(BaseModel):
    run: RunSummary
    spans: list[SpanOut]
    """Todas as observações do trace, em ordem de início; a árvore sai de `parent_id`."""
    scores: list[ScoreOut]
    reference: Any = None
    """Decisão de referência gravada para o run (ex.: a do agente legado no shadow)."""
    procedure_state: dict[str, Any] | None = None
    """Só em turno de agente procedural: o `state` como o turno terminou (etapa,
    collected, missing, invalid, done) — o mesmo que o `/chat` devolveu."""


class SessionSummary(BaseModel):
    session_id: str
    user_id: str | None = None
    agent_types: list[str] = []
    run_count: int
    started_at: datetime
    last_activity: datetime
    total_tokens: int = 0
    cost_usd: float | None = None
    error_count: int = 0
    feedback_up: int | None = 0
    feedback_down: int | None = 0
    """`None` quando havia avaliações demais para contar com segurança — ver `RunSummary.feedback_up`."""


class SessionPage(BaseModel):
    items: list[SessionSummary]
    scanned: int
    """Quantas execuções-raiz foram varridas para montar esta lista — ver `_AGGREGATE_SCAN_LIMIT`."""


class StatsBucket(BaseModel):
    date: str
    """Dia UTC, `YYYY-MM-DD`."""
    runs: int
    errors: int
    total_tokens: int
    cost_usd: float | None = None


class RunStats(BaseModel):
    buckets: list[StatsBucket]
    """Um por dia com pelo menos uma execução, em ordem cronológica."""
    status_counts: dict[str, int]
    total_runs: int
    total_tokens: int
    total_cost_usd: float | None = None
    avg_latency_ms: float | None = None
    scanned: int
    """Quantas execuções-raiz foram varridas para montar este resumo — ver `_AGGREGATE_SCAN_LIMIT`."""


@dataclass(frozen=True)
class RunQuery:
    agent_type: str | None = None
    """`None` lista todos os agentes (para a página `/observability` do console)."""
    prompt_version: int | None = None
    agent_version: int | None = None
    knowledge_collection: str | None = None
    collection_version: int | None = None
    """Só runs com esta versão da configuração de busca (combine com `knowledge_collection`)."""
    status: RunStatus | None = None
    user_id: str | None = None
    session_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 50
    cursor: str | None = None
    tenant_id: str | None = None
    """Reservado para isolar clientes da API: filtra `metadata.tenant_id`."""
    search: str | None = None
    """Trecho do `session_id`, `user_id` ou de um valor de metadata (ex.: `conversation_id`) — só no trace store local."""
    metadata: tuple[tuple[str, str], ...] = ()
    """Pares chave/valor que o run precisa ter (todos) — só no trace store local."""
    # Fila de revisão (só no trace store local): o que vale um humano olhar.
    complexity: tuple[int, ...] = ()
    """Só estes níveis (1–3, ver `tools/failures.py`)."""
    tool_failed: bool | None = None
    """`True`: só runs com alguma tool falhando; `False`: só sem."""
    side_effect: bool | None = None
    """`True`: só runs que chamaram uma tool com `side_effect=true`; `False`: só os que não."""
    min_message_chars: int | None = None
    """Esconde mensagens curtas ("ok", "oi"): só runs com a mensagem pelo menos deste tamanho."""
    feedback: str | None = None
    """`down`: com algum 👎; `up`: com algum 👍; `none`: sem voto."""
    exclude_dry_run: bool = False
    """Tira os testes do Playground (`metadata.dry_run = "true"`)."""
    sample: int | None = None
    """Amostra aleatória deste tamanho, em vez das mais recentes (sem paginação)."""
    agent_types: tuple[str, ...] = ()
    """Só estes agentes (qualquer um deles). Soma-se a `agent_type`, que fica para quem filtra um só."""
    statuses: tuple[RunStatus, ...] = ()
    """Só estes status (qualquer um deles) — ex.: `error` + `interrupted`, o "falharam" do panorama."""
    sort: RunSort = "recent"
    rag: RagFilter | None = None
    """Só runs que buscaram na base (`searched`), com alguma busca sem trecho nenhum
    (`empty`) ou com trecho marcado errado e ainda aberto (`flagged`) — só no trace store local."""
    """`cost`/`latency`: as mais caras/lentas primeiro, sem os runs sem esse valor — só no trace store local."""


class TraceStore(Protocol):
    def list_runs(self, query: RunQuery) -> RunPage: ...

    def get_run(self, run_id: str, *, tenant_id: str | None = None) -> RunTrace | None: ...

    def list_sessions(self, query: RunQuery) -> SessionPage: ...

    def get_stats(self, query: RunQuery) -> RunStats: ...


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_io(value: Any) -> Any:
    """A API devolve input/output como string; JSON vira objeto para o console formatar."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except ValueError:
            return value
    return value


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ms(seconds: Any) -> float | None:
    return round(seconds * 1000, 1) if isinstance(seconds, (int, float)) else None


class _Usage:
    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.cost_usd: float | None = None
        self.model: str | None = None

    def add(self, observation: dict[str, Any]) -> None:
        self.input_tokens += observation.get("inputUsage") or 0
        self.output_tokens += observation.get("outputUsage") or 0
        self.total_tokens += observation.get("totalUsage") or 0
        cost = observation.get("totalCost")
        if cost is not None:
            self.cost_usd = (self.cost_usd or 0) + cost
        if observation.get("model"):
            self.model = observation["model"]


def _root_filters(query: RunQuery) -> list[dict[str, Any]]:
    """Os filtros de `RunQuery` que a API de observações do Langfuse entende. Os da fila de
    revisão, a busca e a metadata ficam de fora: só existem no trace store local."""
    filters: list[dict[str, Any]] = []
    if query.agent_type:
        filters.append({"type": "string", "column": "traceName", "operator": "=", "value": query.agent_type})
    # Um valor só segue com `=`, o filtro de sempre; `any of` fica para quando há vários.
    if len(query.agent_types) == 1:
        filters.append({"type": "string", "column": "traceName", "operator": "=", "value": query.agent_types[0]})
    elif query.agent_types:
        filters.append({"type": "stringOptions", "column": "traceName", "operator": "any of", "value": list(query.agent_types)})
    if query.prompt_version is not None:
        filters.append({"type": "string", "column": "version", "operator": "=", "value": f"{_VERSION_PREFIX}{query.prompt_version}"})
    if query.status:
        filters.append({"type": "string", "column": "level", "operator": "=", "value": _LEVEL_BY_STATUS[query.status]})
    levels = [_LEVEL_BY_STATUS[status] for status in query.statuses]
    if len(levels) == 1:
        filters.append({"type": "string", "column": "level", "operator": "=", "value": levels[0]})
    elif levels:
        filters.append({"type": "stringOptions", "column": "level", "operator": "any of", "value": levels})
    if query.user_id:
        filters.append({"type": "string", "column": "userId", "operator": "=", "value": query.user_id})
    if query.session_id:
        filters.append({"type": "string", "column": "sessionId", "operator": "=", "value": query.session_id})
    if query.since:
        filters.append({"type": "datetime", "column": "startTime", "operator": ">=", "value": _iso(query.since)})
    if query.until:
        filters.append({"type": "datetime", "column": "startTime", "operator": "<", "value": _iso(query.until)})
    return filters


class LangfuseTraceStore:
    def __init__(
        self,
        *,
        base_url: str,
        public_key: str,
        secret_key: str,
        environment: str | None,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._environment = environment
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(public_key, secret_key),
            timeout=timeout,
            transport=transport,
        )

    # -- API do Langfuse ---------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as exc:
            raise TraceStoreError(f"Langfuse inacessível: {exc}") from exc
        if response.status_code >= 400:
            raise TraceStoreError(f"Langfuse respondeu {response.status_code}: {response.text[:300]}")
        return response.json()

    def _observations(self, filters: list[dict[str, Any]], *, fields: str, limit: int, cursor: str | None = None) -> dict[str, Any]:
        # `filter` tem precedência sobre os filtros por query param — tudo vai nele.
        return self._get(
            "/api/public/v2/observations",
            {"filter": json.dumps(filters), "fields": fields, "limit": limit, "cursor": cursor},
        )

    def _all_observations(self, filters: list[dict[str, Any]], *, fields: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self._observations(filters, fields=fields, limit=_OBSERVATIONS_PAGE, cursor=cursor)
            items.extend(page.get("data", []))
            cursor = page.get("meta", {}).get("cursor")
            if not cursor or not page.get("data"):
                return items

    def _scores(self, **params: Any) -> tuple[list[dict[str, Any]], bool]:
        """Scores que casam com os filtros e se a lista está completa (não bateu no limite de páginas)."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_SCORE_PAGES):
            page = self._get(
                "/api/public/v3/scores",
                {**params, "fields": "details,subject", "limit": _SCORES_PAGE, "cursor": cursor},
            )
            items.extend(page.get("data", []))
            cursor = page.get("meta", {}).get("cursor")
            if not cursor:
                return items, True
        return items, False

    def _scope_filters(self, tenant_id: str | None) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = []
        if self._environment:
            filters.append({"type": "stringOptions", "column": "environment", "operator": "any of", "value": [self._environment]})
        if tenant_id:
            filters.append({"type": "stringObject", "column": "metadata", "key": "tenant_id", "operator": "=", "value": tenant_id})
        return filters

    # -- TraceStore --------------------------------------------------------

    def list_runs(self, query: RunQuery) -> RunPage:
        filters = self._scope_filters(query.tenant_id) + [
            {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
        ]
        filters.extend(_root_filters(query))

        page = self._observations(filters, fields="basic,io,metadata,metrics", limit=query.limit, cursor=query.cursor)
        roots = [r for r in page.get("data", []) if (r.get("metadata") or {}).get("run_id")]
        next_cursor = page.get("meta", {}).get("cursor") if page.get("data") else None
        if not roots:
            return RunPage(items=[], next_cursor=next_cursor)

        trace_ids = [r["traceId"] for r in roots]
        usage = {trace_id: _Usage() for trace_id in trace_ids}
        for observation in self._all_observations(
            [{"type": "stringOptions", "column": "traceId", "operator": "any of", "value": trace_ids}],
            fields="usage,model",
        ):
            if observation["traceId"] in usage:
                usage[observation["traceId"]].add(observation)

        # A API de scores não filtra vários traces de uma vez: busca o feedback do
        # projeto desde o run mais antigo da página e cruza aqui. Se passar do limite
        # de páginas, as contagens ficam desconhecidas (None) — nunca uma soma parcial.
        votes = {trace_id: [0, 0] for trace_id in trace_ids}
        oldest = min(r["startTime"] for r in roots)
        scores, complete = self._scores(name=FEEDBACK, fromTimestamp=oldest, dataType="BOOLEAN")
        for score in scores:
            subject = score.get("subject") or {}
            trace_id = subject.get("traceId") or subject.get("id")
            if trace_id in votes:
                votes[trace_id][0 if score.get("value") else 1] += 1

        items = []
        for root in roots:
            summary = self._summary(root, usage[root["traceId"]])
            if complete:
                summary.feedback_up, summary.feedback_down = votes[root["traceId"]]
            else:
                summary.feedback_up = summary.feedback_down = None
            items.append(summary)
        return RunPage(items=items, next_cursor=next_cursor)

    def get_run(self, run_id: str, *, tenant_id: str | None = None) -> RunTrace | None:
        trace_id = trace_id_for_run(run_id)
        observations = self._all_observations(
            self._scope_filters(tenant_id)
            + [{"type": "string", "column": "traceId", "operator": "=", "value": trace_id}],
            fields="core,basic,io,metadata,model,usage,metrics",
        )
        root = next((o for o in observations if o.get("isRootObservation")), None)
        if root is None:
            return None

        usage = _Usage()
        for observation in observations:
            usage.add(observation)
        spans = sorted((self._span(o, root) for o in observations), key=lambda s: s.started_at)
        trace_scores, complete = self._scores(traceId=trace_id)
        scores = [self._score(s) for s in trace_scores]
        run = self._summary(root, usage)
        feedback = [s for s in scores if s.name == FEEDBACK and s.data_type == "BOOLEAN"]
        if complete:
            run.feedback_up = sum(1 for s in feedback if s.value)
            run.feedback_down = len(feedback) - run.feedback_up
        else:
            run.feedback_up = run.feedback_down = None
        return RunTrace(run=run, spans=spans, scores=scores)

    def _scan_roots(self, query: RunQuery, *, include_session: bool = False) -> tuple[list[dict[str, Any]], int]:
        """Execuções-raiz que casam os filtros (até `_AGGREGATE_SCAN_LIMIT`), mais recentes primeiro."""
        filters = self._scope_filters(query.tenant_id) + [
            {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
        ]
        filters.extend(_root_filters(query))

        page = self._observations(filters, fields="basic,metadata,metrics", limit=_AGGREGATE_SCAN_LIMIT)
        data = page.get("data", [])
        roots = [r for r in data if (r.get("metadata") or {}).get("run_id") and (not include_session or r.get("sessionId"))]
        return roots, len(data)

    def _usage_by_trace(self, trace_ids: list[str]) -> dict[str, "_Usage"]:
        usage = {trace_id: _Usage() for trace_id in trace_ids}
        if not trace_ids:
            return usage
        for observation in self._all_observations(
            [{"type": "stringOptions", "column": "traceId", "operator": "any of", "value": trace_ids}],
            fields="usage,model",
        ):
            if observation["traceId"] in usage:
                usage[observation["traceId"]].add(observation)
        return usage

    def list_sessions(self, query: RunQuery) -> SessionPage:
        roots, scanned = self._scan_roots(query, include_session=True)
        if not roots:
            return SessionPage(items=[], scanned=scanned)

        trace_ids = [r["traceId"] for r in roots]
        usage = self._usage_by_trace(trace_ids)
        trace_to_session = {r["traceId"]: r["sessionId"] for r in roots}

        grouped: dict[str, dict[str, Any]] = {}
        for root in roots:
            session_id = root["sessionId"]
            bucket = grouped.setdefault(
                session_id,
                {
                    "session_id": session_id,
                    "user_id": root.get("userId") or None,
                    "agent_types": set(),
                    "run_count": 0,
                    "started_at": root["startTime"],
                    "last_activity": root["startTime"],
                    "total_tokens": 0,
                    "cost_usd": None,
                    "error_count": 0,
                    "feedback_up": 0,
                    "feedback_down": 0,
                },
            )
            bucket["run_count"] += 1
            agent_type = (root.get("metadata") or {}).get("agent_type") or root.get("traceName")
            if agent_type:
                bucket["agent_types"].add(agent_type)
            bucket["started_at"] = min(bucket["started_at"], root["startTime"])
            bucket["last_activity"] = max(bucket["last_activity"], root["startTime"])
            trace_usage = usage[root["traceId"]]
            bucket["total_tokens"] += trace_usage.total_tokens
            if trace_usage.cost_usd is not None:
                bucket["cost_usd"] = (bucket["cost_usd"] or 0) + trace_usage.cost_usd
            if _STATUS_BY_LEVEL.get(root.get("level") or "", "success") == "error":
                bucket["error_count"] += 1

        oldest = min(r["startTime"] for r in roots)
        scores, complete = self._scores(name=FEEDBACK, fromTimestamp=oldest, dataType="BOOLEAN")
        if complete:
            for score in scores:
                subject = score.get("subject") or {}
                trace_id = subject.get("traceId") or subject.get("id")
                session_id = trace_to_session.get(trace_id)
                if session_id:
                    grouped[session_id]["feedback_up" if score.get("value") else "feedback_down"] += 1

        items = [
            SessionSummary(
                session_id=bucket["session_id"],
                user_id=bucket["user_id"],
                agent_types=sorted(bucket["agent_types"]),
                run_count=bucket["run_count"],
                started_at=bucket["started_at"],
                last_activity=bucket["last_activity"],
                total_tokens=bucket["total_tokens"],
                cost_usd=bucket["cost_usd"],
                error_count=bucket["error_count"],
                feedback_up=bucket["feedback_up"] if complete else None,
                feedback_down=bucket["feedback_down"] if complete else None,
            )
            for bucket in grouped.values()
        ]
        items.sort(key=lambda s: s.last_activity, reverse=True)
        return SessionPage(items=items[: query.limit], scanned=scanned)

    def get_stats(self, query: RunQuery) -> RunStats:
        roots, scanned = self._scan_roots(query)
        if not roots:
            return RunStats(buckets=[], status_counts={}, total_runs=0, total_tokens=0, scanned=scanned)

        trace_ids = [r["traceId"] for r in roots]
        usage = self._usage_by_trace(trace_ids)

        buckets: dict[str, dict[str, Any]] = {}
        status_counts: dict[str, int] = {}
        total_tokens = 0
        total_cost: float | None = None
        latencies: list[float] = []
        for root in roots:
            day = str(root["startTime"])[:10]
            bucket = buckets.setdefault(day, {"date": day, "runs": 0, "errors": 0, "total_tokens": 0, "cost_usd": None})
            bucket["runs"] += 1
            status = _STATUS_BY_LEVEL.get(root.get("level") or "", "success")
            status_counts[status] = status_counts.get(status, 0) + 1
            if status == "error":
                bucket["errors"] += 1
            trace_usage = usage[root["traceId"]]
            bucket["total_tokens"] += trace_usage.total_tokens
            total_tokens += trace_usage.total_tokens
            if trace_usage.cost_usd is not None:
                bucket["cost_usd"] = (bucket["cost_usd"] or 0) + trace_usage.cost_usd
                total_cost = (total_cost or 0) + trace_usage.cost_usd
            latency = _ms(root.get("latency"))
            if latency is not None:
                latencies.append(latency)

        return RunStats(
            buckets=[StatsBucket(**buckets[day]) for day in sorted(buckets)],
            status_counts=status_counts,
            total_runs=len(roots),
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            avg_latency_ms=round(sum(latencies) / len(latencies), 1) if latencies else None,
            scanned=scanned,
        )

    # -- conversão ---------------------------------------------------------

    @staticmethod
    def _summary(root: dict[str, Any], usage: _Usage) -> RunSummary:
        metadata = root.get("metadata") or {}
        run_input = _parse_io(root.get("input"))
        message = run_input.get("message") if isinstance(run_input, dict) else run_input
        version = _to_int(metadata.get("prompt_version"))
        if version is None and str(root.get("version") or "").startswith(_VERSION_PREFIX):
            version = _to_int(root["version"][len(_VERSION_PREFIX) :])
        return RunSummary(
            run_id=str(metadata["run_id"]),
            trace_id=root["traceId"],
            agent_type=str(metadata.get("agent_type") or root.get("traceName") or ""),
            agent_name=metadata.get("agent_name"),
            prompt_version=version,
            agent_version=_to_int(metadata.get("agent_version")),
            config_hash=metadata.get("config_hash") if metadata.get("config_hash") not in (None, "None") else None,
            endpoint=metadata.get("endpoint") or root.get("name"),
            user_id=root.get("userId") or None,
            session_id=root.get("sessionId") or None,
            environment=root.get("environment") or None,
            started_at=root["startTime"],
            ended_at=root.get("endTime"),
            latency_ms=_ms(root.get("latency")),
            status=_STATUS_BY_LEVEL.get(root.get("level") or "", "success"),
            status_message=root.get("statusMessage") or None,
            message=message if isinstance(message, str) else None,
            output=root.get("output") if isinstance(root.get("output"), str) else None,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cost_usd=usage.cost_usd,
        )

    @staticmethod
    def _span(observation: dict[str, Any], root: dict[str, Any]) -> SpanOut:
        is_root = observation["id"] == root["id"]
        metadata = {
            key.removeprefix("attributes."): value
            for key, value in (observation.get("metadata") or {}).items()
            if not key.startswith(_NOISY_METADATA_PREFIXES)
        }
        return SpanOut(
            id=observation["id"],
            # A raiz pode apontar para um pai externo (o contexto remoto do trace): no console ela é a raiz.
            parent_id=None if is_root else observation.get("parentObservationId"),
            type=observation.get("type") or "SPAN",
            name=observation.get("name") or "",
            started_at=observation["startTime"],
            ended_at=observation.get("endTime"),
            latency_ms=_ms(observation.get("latency")),
            level=observation.get("level") or "DEFAULT",
            status_message=observation.get("statusMessage") or None,
            input=_parse_io(observation.get("input")),
            output=_parse_io(observation.get("output")),
            model=observation.get("model") or None,
            input_tokens=observation.get("inputUsage") or 0,
            output_tokens=observation.get("outputUsage") or 0,
            total_tokens=observation.get("totalUsage") or 0,
            cost_usd=observation.get("totalCost"),
            metadata=metadata,
        )

    @staticmethod
    def _score(score: dict[str, Any]) -> ScoreOut:
        metadata = score.get("metadata") or {}
        return ScoreOut(
            id=score["id"],
            name=score["name"],
            value=score.get("value"),
            data_type=score.get("dataType") or "NUMERIC",
            source=score.get("source") or "API",
            comment=score.get("comment") or None,
            user_id=metadata.get("user_id") if isinstance(metadata, dict) else None,
            timestamp=score["timestamp"],
        )


_store: TraceStore | None = None


def get_trace_store() -> TraceStore | None:
    """O trace store local por padrão. Com `TRACE_STORE_BACKEND=langfuse`, `None`
    enquanto o Langfuse estiver desligado — as rotas respondem 503."""
    global _store
    if _store is not None:
        return _store
    settings = get_settings()
    if settings.trace_store_backend == "db":
        from agent_service.observability.run_store import DbTraceStore

        _store = DbTraceStore()
    elif get_langfuse() is not None:
        _store = LangfuseTraceStore(
            base_url=settings.langfuse_base_url,
            public_key=settings.langfuse_public_key or "",
            secret_key=settings.langfuse_secret_key or "",
            environment=settings.langfuse_environment,
            timeout=settings.langfuse_timeout_seconds,
        )
    return _store


def set_trace_store(store: TraceStore | None) -> None:
    """Para testes."""
    global _store
    _store = store
