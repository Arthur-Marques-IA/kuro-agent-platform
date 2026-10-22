"""Observabilidade exposta pelo serviço: execuções, traces e scores.

As execuções ficam no trace store local (`observability/run_store.py`), e o
Langfuse, quando ligado, é só um exportador a mais. Ninguém precisa abrir o Langfuse: estas rotas são o contrato estável para o console e
para outros módulos — listar as execuções de um agente, ler o trace completo
de um `run_id` e registrar avaliações (feedback do usuário final, notas de evals).
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from agent_service.observability import tracing
from agent_service.observability.overview import DEFAULT_TIMEZONE, Granularity, Overview, build_overview
from agent_service.observability.trace_store import (
    RagFilter,
    RunPage,
    RunQuery,
    RunSort,
    RunStats,
    RunStatus,
    RunSummary,
    RunTrace,
    SessionPage,
    TraceStore,
    TraceStoreError,
    get_trace_store,
)

router = APIRouter(prefix="/observability", tags=["observability"])


class ObservabilityConfigOut(BaseModel):
    enabled: bool
    """As execuções estão sendo registradas e podem ser lidas (`/observability/runs`...).
    Sempre `true` com o trace store local, o padrão."""
    langfuse: bool = False
    """O exportador do Langfuse está ligado."""
    project_url: str | None = None
    """Projeto na UI do Langfuse — `None` se desligado ou ainda inacessível."""
    correlation_keys: list[str] = []
    """Chaves de metadata que ligam runs de um mesmo fluxo (`CORRELATION_METADATA_KEYS`)."""


class RunTraceOut(BaseModel):
    run_id: str
    trace_id: str
    trace_url: str | None = None


class ScoreIn(BaseModel):
    run_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="run_id do Agno — vem em `ChatResponse.run_id` ou no `event: run` do stream",
    )
    name: str = Field(tracing.FEEDBACK, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    value: float = Field(..., description="`feedback`: 1 = positivo, 0 = negativo; demais nomes: numérico")
    comment: str | None = Field(default=None, max_length=2000)
    user_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def feedback_is_boolean(self) -> "ScoreIn":
        if self.name == tracing.FEEDBACK and self.value not in (0, 1):
            raise ValueError("`feedback` aceita apenas 1 (positivo) ou 0 (negativo)")
        return self


class ScoreOut(BaseModel):
    run_id: str
    trace_id: str
    name: str
    value: float


def _is_local(store: TraceStore) -> bool:
    from agent_service.observability.run_store import DbTraceStore

    return isinstance(store, DbTraceStore)


def _store() -> TraceStore:
    store = get_trace_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="TRACE_STORE_BACKEND=langfuse, mas o Langfuse não está configurado neste serviço.",
        )
    return store


AgentTypes = Annotated[
    list[str] | None,
    Query(description="Só estes agentes (repetível: `agent_type=r8&agent_type=suporte`)."),
]
Statuses = Annotated[
    list[RunStatus] | None,
    Query(description="Só estes status (repetível: `status=error&status=interrupted`)."),
]


def _many(value: Any) -> tuple[Any, ...]:
    """Parâmetro repetível como tupla. Chamada direta (testes, outros módulos) pode passar um valor só."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(dict.fromkeys(value))


def _parse_meta(meta: list[str] | None) -> tuple[tuple[str, str], ...]:
    pairs = []
    for item in meta or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise HTTPException(status_code=422, detail=f"meta deve ser chave=valor, veio {item!r}")
        pairs.append((key, value))
    return tuple(pairs)


def _list_runs(
    *,
    agent_type: str | list[str] | None,
    prompt_version: int | None,
    status: RunStatus | list[RunStatus] | None,
    user_id: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    cursor: str | None,
    agent_version: int | None = None,
    meta: list[str] | None = None,
    search: str | None = None,
    review: dict[str, Any] | None = None,
    sort: RunSort = "recent",
) -> RunPage:
    store = _store()
    if sort != "recent" and not _is_local(store):
        raise HTTPException(
            status_code=501,
            detail="Ordenar por custo ou latência só existe com o trace store local (TRACE_STORE_BACKEND=db).",
        )
    query = RunQuery(
        **(review or {}),
        agent_types=_many(agent_type),
        prompt_version=prompt_version,
        agent_version=agent_version,
        statuses=_many(status),
        sort=sort,
        user_id=user_id,
        session_id=session_id,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
        metadata=_parse_meta(meta),
        search=search,
    )
    try:
        return store.list_runs(query)
    except TraceStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/runs", response_model=RunPage)
def list_runs(
    agent_type: AgentTypes = None,
    prompt_version: Annotated[int | None, Query(ge=1)] = None,
    status: Statuses = None,
    user_id: str | None = None,
    session_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    agent_version: Annotated[int | None, Query(ge=1)] = None,
    meta: Annotated[list[str] | None, Query(description="Filtro por metadata, `chave=valor` (repetível).")] = None,
    search: Annotated[str | None, Query(max_length=200, description="Trecho do session_id, user_id ou de um valor de metadata, ou o run_id/trace_id inteiro.")] = None,
    complexity: Annotated[
        list[Annotated[int, Field(ge=1, le=3)]] | None, Query(description="Só estes níveis de complexidade (repetível).")
    ] = None,
    tool_failed: Annotated[bool | None, Query(description="true: só com tool falhando; false: só sem.")] = None,
    side_effect: Annotated[bool | None, Query(description="true: só os que chamaram tool com efeito colateral.")] = None,
    min_message_chars: Annotated[int | None, Query(ge=1, le=1000, description="Esconde mensagens mais curtas que isto.")] = None,
    feedback: Annotated[Literal["up", "down", "none"] | None, Query()] = None,
    include_dry_run: Annotated[bool, Query(description="false tira os testes do Playground.")] = True,
    sample: Annotated[int | None, Query(ge=1, le=100, description="Amostra aleatória deste tamanho, sem paginação.")] = None,
    sort: Annotated[
        RunSort, Query(description="`cost`/`latency`: as mais caras/lentas primeiro (sem os runs sem esse valor).")
    ] = "recent",
    knowledge_collection: Annotated[str | None, Query(description="Só runs de agentes que consultavam esta collection.")] = None,
    collection_version: Annotated[
        int | None, Query(ge=1, description="Só runs com esta versão da configuração de busca da collection.")
    ] = None,
    rag: Annotated[
        RagFilter | None,
        Query(description="searched: buscou na base; empty: alguma busca voltou vazia; flagged: trecho marcado errado e aberto."),
    ] = None,
) -> RunPage:
    """Execuções de todos os agentes (ou só dos `agent_type` pedidos), mais recentes
    primeiro, com tokens, custo e feedback — a página de Execuções do console usa isto.

    `agent_type` e `status` são repetíveis (qualquer um dos valores). `sort=cost|latency`
    ordena pelas mais caras/lentas — os KPIs de custo e latência do Panorama levam para cá;
    com `sample`, a amostra continua aleatória. Só no trace store local (501 no Langfuse).

    Um run aparece logo depois de terminar (a gravação sai do caminho da resposta).

    Os filtros de revisão (`complexity`, `tool_failed`, `side_effect`,
    `min_message_chars`, `feedback`, `include_dry_run`, `sample`) montam a fila do
    que vale um humano olhar. Só no trace store local.
    """
    return _list_runs(
        agent_type=agent_type,
        prompt_version=prompt_version,
        status=status,
        user_id=user_id,
        session_id=session_id,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
        agent_version=agent_version,
        meta=meta,
        search=search,
        review={
            "complexity": tuple(complexity or ()),
            "tool_failed": tool_failed,
            "side_effect": side_effect,
            "min_message_chars": min_message_chars,
            "feedback": feedback,
            "exclude_dry_run": not include_dry_run,
            "sample": sample,
            "knowledge_collection": knowledge_collection,
            "collection_version": collection_version,
            "rag": rag,
        },
        sort=sort,
    )


@router.get("/agents/{agent_type}/runs", response_model=RunPage)
def list_agent_runs(
    agent_type: str,
    prompt_version: Annotated[int | None, Query(ge=1)] = None,
    status: RunStatus | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    agent_version: Annotated[int | None, Query(ge=1)] = None,
    meta: Annotated[list[str] | None, Query(description="Filtro por metadata, `chave=valor` (repetível).")] = None,
) -> RunPage:
    """Execuções do agente, mais recentes primeiro, com tokens, custo e feedback.

    Um run aparece logo depois de terminar (a gravação sai do caminho da resposta).
    """
    return _list_runs(
        agent_type=agent_type,
        prompt_version=prompt_version,
        status=status,
        user_id=user_id,
        session_id=session_id,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
        agent_version=agent_version,
        meta=meta,
    )


@router.get("/sessions", response_model=SessionPage)
def list_sessions(
    agent_type: AgentTypes = None,
    status: Statuses = None,
    user_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    search: Annotated[str | None, Query(max_length=200, description="Trecho do session_id, user_id ou de um valor de metadata, ou o run_id/trace_id inteiro.")] = None,
) -> SessionPage:
    """Sessões recentes (execuções agrupadas por `session_id`), com tokens, custo e feedback somados.

    No trace store local é um `GROUP BY` sobre o histórico inteiro; `scanned` diz
    quantas execuções entraram. Com `TRACE_STORE_BACKEND=langfuse` a API não agrupa
    por sessão, e isto varre só um lote das execuções mais recentes.
    """
    query = RunQuery(
        agent_types=_many(agent_type),
        statuses=_many(status),
        user_id=user_id,
        since=since,
        until=until,
        limit=limit,
        search=search,
    )
    try:
        return _store().list_sessions(query)
    except TraceStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/overview", response_model=Overview)
def overview(
    since: datetime | None = None,
    until: datetime | None = None,
    agent_type: AgentTypes = None,
    include_dry_run: bool = False,
    tz: Annotated[str, Query(max_length=64, description="Fuso dos baldes da série (IANA).")] = DEFAULT_TIMEZONE,
    granularity: Granularity | None = None,
) -> Overview:
    """O panorama do dashboard dos Logs (`observability/overview.py`): totais com o
    período anterior, série no tempo sem buracos, uma linha por agente com as versões
    da configuração, e as tools que estão falhando. Sem `since`, desde o primeiro run.
    `agent_type` é repetível; o período anterior é a janela do mesmo tamanho logo antes de `since`.

    Os testes do Playground (`dry_run`) ficam de fora, a menos que `include_dry_run`.
    Só no trace store local: com `TRACE_STORE_BACKEND=langfuse` responde 501."""
    from zoneinfo import ZoneInfoNotFoundError

    if not _is_local(_store()):
        raise HTTPException(status_code=501, detail="O panorama só existe com o trace store local (TRACE_STORE_BACKEND=db).")
    if since and until and since >= until:
        raise HTTPException(status_code=422, detail="since precisa ser anterior a until")
    try:
        return build_overview(
            since=since,
            until=until,
            agent_type=_many(agent_type),
            include_dry_run=include_dry_run,
            timezone_name=tz,
            granularity=granularity,
        )
    except OverflowError as exc:
        # O período anterior (`since` menos a duração) caiu antes do ano 1: datas fora do plausível.
        raise HTTPException(status_code=422, detail="Intervalo de datas fora do suportado") from exc
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Fuso desconhecido: {tz!r}") from exc


@router.get("/stats", response_model=RunStats)
def run_stats(
    agent_type: AgentTypes = None,
    prompt_version: Annotated[int | None, Query(ge=1)] = None,
    status: Statuses = None,
    user_id: str | None = None,
    session_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    include_dry_run: Annotated[bool, Query(description="false tira os testes do Playground.")] = True,
    knowledge_collection: str | None = None,
    rag: RagFilter | None = None,
) -> RunStats:
    """Série diária (execuções, erros, tokens, custo) mais contagem por status — os gráficos do console.

    No trace store local cobre o histórico inteiro; com `TRACE_STORE_BACKEND=langfuse`,
    só um lote das execuções mais recentes (ver `list_sessions`).
    """
    query = RunQuery(
        agent_types=_many(agent_type),
        prompt_version=prompt_version,
        statuses=_many(status),
        user_id=user_id,
        session_id=session_id,
        since=since,
        until=until,
        search=search,
        exclude_dry_run=not include_dry_run,
        knowledge_collection=knowledge_collection,
        rag=rag,
    )
    try:
        return _store().get_stats(query)
    except TraceStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/runs/{run_id}/trace", response_model=RunTrace)
def get_run_trace(run_id: str) -> RunTrace:
    """Trace completo do run: resumo, todos os spans (árvore via `parent_id`) e scores.

    404 também enquanto o run ainda está sendo indexado — tente de novo em alguns segundos.
    """
    try:
        trace = _store().get_run(run_id)
    except TraceStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if trace is None:
        raise HTTPException(
            status_code=404,
            detail="Trace não encontrado. Se o run acabou de terminar, ele ainda pode estar sendo indexado.",
        )
    return trace


@router.get("/config", response_model=ObservabilityConfigOut)
def observability_config() -> ObservabilityConfigOut:
    langfuse = tracing.get_langfuse() is not None
    return ObservabilityConfigOut(
        enabled=get_trace_store() is not None,
        langfuse=langfuse,
        project_url=tracing.project_url() if langfuse else None,
        correlation_keys=correlation_keys(),
    )


def correlation_keys() -> list[str]:
    from agent_service.config import get_settings

    raw = get_settings().correlation_metadata_keys or ""
    return list(dict.fromkeys(k.strip() for k in raw.split(",") if k.strip()))


RELATED_LIMIT = 100


class RelatedRuns(BaseModel):
    key: str | None = None
    """A chave de correlação que ligou os runs (a primeira configurada que o run tem)."""
    value: str | None = None
    items: list[RunSummary] = []
    """Os runs com o mesmo valor, de qualquer agente, do mais antigo ao mais novo — o
    próprio run incluído. Até 100."""


@router.get("/runs/{run_id}/related", response_model=RelatedRuns)
def related_runs(run_id: str) -> RelatedRuns:
    """A linha do tempo do fluxo: os runs que compartilham com este uma chave de
    correlação (`CORRELATION_METADATA_KEYS`, padrão `ciclo_id`) — ex.: triagem →
    redação → consulta à base no ciclo do moderador. Sem chave, `items` vem vazio."""
    store = _store()
    try:
        trace = store.get_run(run_id)
    except TraceStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} não encontrado.")
    meta = trace.run.metadata or {}
    key = next((k for k in correlation_keys() if meta.get(k)), None)
    if key is None:
        return RelatedRuns()
    try:
        page = store.list_runs(RunQuery(metadata=((key, meta[key]),), limit=RELATED_LIMIT))
    except TraceStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    items = sorted(page.items, key=lambda r: (r.started_at, r.run_id))
    return RelatedRuns(key=key, value=meta[key], items=items)


class RunFeedbackIn(BaseModel):
    value: Literal[0, 1] = Field(..., description="1 = positivo, 0 = negativo")
    comment: str | None = Field(default=None, max_length=2000)
    user_id: str | None = Field(default=None, max_length=200)
    reference: dict[str, Any] | None = None
    """A saída que deveria ter saído (agente `analysis`): vira a referência do run —
    caso de eval em `GET /observability/export` — e não regra no prompt."""


class RunFeedbackOut(BaseModel):
    run_id: str
    value: int
    reference_saved: bool


@router.post("/runs/{run_id}/feedback", response_model=RunFeedbackOut, status_code=201)
def run_feedback(run_id: str, body: RunFeedbackIn) -> RunFeedbackOut:
    """👍/👎 de um run, com comentário e, opcionalmente, a saída correta.

    É o feedback de agente `analysis`, que não tem nota de comportamento: o voto
    vira o score `feedback` e a saída corrigida vira a referência do run
    (`source="feedback"`), que `kuro runs export` transforma em caso do
    `kuro eval`. A correção chega ao prompt pelo fluxo draft → eval → promote,
    não como regra global."""
    from agent_service.observability import run_store

    if body.reference is not None and body.value != 0:
        raise HTTPException(status_code=422, detail="A saída correta só acompanha um feedback negativo (value=0).")
    try:
        trace = _store().get_run(run_id)
    except TraceStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} não encontrado.")
    if body.reference is not None:
        try:
            run_store.save_reference(run_id, body.reference, source="feedback")
        except run_store.RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} não encontrado.") from exc
    tracing.score_run(
        run_id=run_id, name=tracing.FEEDBACK, value=body.value, comment=body.comment, user_id=body.user_id
    )
    return RunFeedbackOut(run_id=run_id, value=body.value, reference_saved=body.reference is not None)


@router.get("/runs/{run_id}", response_model=RunTraceOut)
def run_trace(run_id: str) -> RunTraceOut:
    trace_id = tracing.trace_id_for_run(run_id)
    project_url = tracing.project_url()
    return RunTraceOut(
        run_id=run_id,
        trace_id=trace_id,
        trace_url=f"{project_url}/traces/{trace_id}" if project_url else None,
    )


@router.post("/scores", response_model=ScoreOut, status_code=202)
def create_score(body: ScoreIn) -> ScoreOut:
    """Grava o score no trace store local (e o enfileira para o Langfuse, se ligado)."""
    trace_id = tracing.score_run(
        run_id=body.run_id,
        name=body.name,
        value=body.value,
        comment=body.comment,
        user_id=body.user_id,
    )
    return ScoreOut(run_id=body.run_id, trace_id=trace_id, name=body.name, value=body.value)


# -- modo shadow: referência, concordância e export -----------------------------------


class ReferenceIn(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=200)
    reference: dict[str, Any]
    """A decisão que deveria ter saído — no shadow, a do agente legado, no mesmo
    formato do `result` do `/analyze`."""
    source: str = Field("legacy", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")


class ReferenceOut(BaseModel):
    run_id: str
    source: str


class FieldAgreement(BaseModel):
    field: str
    compared: int
    matched: int
    rate: float


class VersionAgreement(BaseModel):
    agent_version: int | None
    runs: int
    full_match: int
    rate: float


class Disagreement(BaseModel):
    run_id: str
    agent_version: int | None
    mismatches: list[dict[str, Any]]


class AgreementOut(BaseModel):
    agent_type: str
    runs: int
    """Runs com referência e saída estruturada — os que entram na conta."""
    skipped: int
    """Runs com referência mas sem saída comparável (erro, timeout)."""
    full_match: int
    rate: float
    """Fração de runs em que todos os campos compararam iguais."""
    fields: list[FieldAgreement]
    by_version: list[VersionAgreement]
    disagreements: list[Disagreement]
    """Os mais recentes em que discordou (até 20)."""


class ExportCase(BaseModel):
    id: str
    input: str
    dependencies: dict[str, Any] | None = None
    expected: dict[str, Any]
    agent_version: int | None = None


@router.post("/references", response_model=ReferenceOut, status_code=201)
def save_reference(body: ReferenceIn) -> ReferenceOut:
    """Grava a decisão de referência de um run (substitui se já houver).

    No shadow, quem chama executa o agente legado e o Kuro na mesma entrada e
    manda aqui a decisão do legado, com o `run_id` que o `/analyze` devolveu.
    Escopo `runtime`, como os scores."""
    from agent_service.observability import run_store

    try:
        run_store.save_reference(body.run_id, body.reference, source=body.source)
    except run_store.RunNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"Run {body.run_id!r} não encontrado (a gravação leva um instante após a resposta)."
        ) from exc
    return ReferenceOut(run_id=body.run_id, source=body.source)


def _referenced(agent_type: str, since: datetime | None, until: datetime | None, agent_version: int | None):
    from agent_service.observability import run_store

    return run_store.referenced_runs(
        RunQuery(agent_type=agent_type, since=since, until=until, agent_version=agent_version)
    )


@router.get("/agreement", response_model=AgreementOut)
def agreement(
    agent_type: str,
    since: datetime | None = None,
    until: datetime | None = None,
    agent_version: Annotated[int | None, Query(ge=1)] = None,
    fields: Annotated[str | None, Query(description="Só estes campos, separados por vírgula.")] = None,
    tolerance: Annotated[float, Query(ge=0)] = 0.01,
) -> AgreementOut:
    """Quanto a decisão do agente concorda com a referência (ex.: o legado no shadow),
    campo a campo e por versão da configuração — a mesma comparação do `kuro eval`.

    É o número que diz quando promover: "a v7 concorda em 97% com o legado"."""
    from agent_service.evaluation import compare, flatten

    wanted = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
    per_field: dict[str, list[int]] = {}
    per_version: dict[int | None, list[int]] = {}
    disagreements: list[Disagreement] = []
    compared = skipped = full = 0
    for item in _referenced(agent_type, since, until, agent_version):
        if item["output"] is None:
            skipped += 1
            continue
        compared += 1
        mismatches = compare(item["reference"], item["output"], wanted, tolerance)
        wrong = {m["field"] for m in mismatches}
        for field in wanted or flatten(item["reference"]).keys():
            counts = per_field.setdefault(field, [0, 0])
            counts[0] += 1
            counts[1] += field not in wrong
        version = per_version.setdefault(item["agent_version"], [0, 0])
        version[0] += 1
        if not mismatches:
            full += 1
            version[1] += 1
        elif len(disagreements) < 20:
            disagreements.append(
                Disagreement(run_id=item["run_id"], agent_version=item["agent_version"], mismatches=mismatches)
            )
    return AgreementOut(
        agent_type=agent_type,
        runs=compared,
        skipped=skipped,
        full_match=full,
        rate=round(full / compared, 4) if compared else 0.0,
        fields=[
            FieldAgreement(field=f, compared=c, matched=m, rate=round(m / c, 4))
            for f, (c, m) in sorted(per_field.items())
        ],
        by_version=[
            VersionAgreement(agent_version=v, runs=c, full_match=m, rate=round(m / c, 4))
            for v, (c, m) in sorted(per_version.items(), key=lambda kv: (kv[0] is None, kv[0] or 0), reverse=True)
        ],
        disagreements=disagreements,
    )


@router.get("/export", response_model=list[ExportCase])
def export_cases(
    agent_type: str,
    since: datetime | None = None,
    until: datetime | None = None,
    agent_version: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> list[ExportCase]:
    """Runs com referência no formato de caso do `kuro eval`: a entrada real e a
    decisão de referência como `expected`. `kuro runs export` grava isto em JSONL."""
    cases = []
    for item in _referenced(agent_type, since, until, agent_version)[:limit]:
        cases.append(
            ExportCase(
                id=item["run_id"],
                input=item["message"] or "",
                dependencies=item["dependencies"],
                expected=item["reference"],
                agent_version=item["agent_version"],
            )
        )
    return cases



# -- feedback por trecho recuperado (RAG) ---------------------------------------------


class ChunkFeedbackIn(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=200)
    chunk_id: str = Field(..., min_length=1, max_length=200, description="`chunk_id` do trecho em `metadata.references`")
    label: Literal["relevant", "irrelevant", "wrong"] = Field(
        ..., description="`relevant` (era o trecho certo), `irrelevant` (não respondia) ou `wrong` (conteúdo errado/desatualizado)"
    )
    span_id: str | None = Field(
        default=None, max_length=300, description="A span da busca; sem ela, vale a primeira busca do run com o trecho"
    )
    comment: str | None = Field(default=None, max_length=2000)
    created_by: str | None = Field(default=None, max_length=200, description="Quem marcou (operador)")


class ChunkFeedbackOut(BaseModel):
    id: str
    run_id: str
    span_id: str
    agent_type: str | None = None
    query: str
    collection: str | None = None
    content_id: str | None = None
    chunk_id: str
    document: str | None = None
    rank: int | None = None
    label: str
    comment: str | None = None
    created_by: str | None = None
    created_at: datetime
    document_revision: int | None = Field(default=None, description="Revisão do documento em que o trecho foi visto")
    status: str = Field(default="open", description="`open`, `resolved` ou `dismissed`")
    resolved_in_revision: int | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    current_revision: int | None = Field(default=None, description="Revisão atual do documento")
    stale: bool = Field(default=False, description="Aberta, mas vista numa revisão que já foi substituída")


class ChunkFeedbackStatusIn(BaseModel):
    status: Literal["open", "resolved", "dismissed"] = Field(
        ..., description="`dismissed` (a marcação não procedia), `resolved` (corrigido sem editar aqui) ou `open` (reabrir)"
    )
    note: str | None = Field(default=None, max_length=2000)
    by: str | None = Field(default=None, max_length=200)


class FlaggedDocument(BaseModel):
    content_id: str | None = None
    document: str | None = None
    collection: str | None = None
    count: int
    last_at: datetime
    run_ids: list[str]
    comments: list[str]
    feedback_ids: list[str] = []
    current_revision: int | None = None
    stale: bool = Field(default=False, description="Todas as marcações são de revisões anteriores à atual")


class RagCase(BaseModel):
    query: str
    collection: str | None = None
    relevant_chunk_ids: list[str]
    relevant_content_ids: list[str]
    run_ids: list[str]


@router.post("/chunk-feedback", response_model=ChunkFeedbackOut, status_code=201)
def create_chunk_feedback(body: ChunkFeedbackIn) -> ChunkFeedbackOut:
    """Marca um trecho consultado num run (substitui a marcação anterior do mesmo trecho
    na mesma busca). Pergunta, collection e documento vêm da span, não do cliente.
    Só registra: nenhum documento é escondido ou despublicado por causa disto."""
    from agent_service.observability import chunk_feedback, run_store

    try:
        row = chunk_feedback.mark(
            body.run_id,
            body.chunk_id,
            body.label,
            span_id=body.span_id,
            comment=body.comment,
            created_by=body.created_by,
        )
    except run_store.RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Run {body.run_id!r} não encontrado.") from exc
    except chunk_feedback.ChunkNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"O trecho {body.chunk_id!r} não aparece nas buscas do run {body.run_id!r}."
        ) from exc
    return ChunkFeedbackOut(**row)


@router.get("/chunk-feedback", response_model=list[ChunkFeedbackOut])
def list_chunk_feedback(
    run_id: str | None = None,
    collection: str | None = None,
    label: Literal["relevant", "irrelevant", "wrong"] | None = None,
    content_id: str | None = None,
    status: Literal["open", "resolved", "dismissed"] | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> list[ChunkFeedbackOut]:
    """Marcações de trecho, mais recentes primeiro."""
    from agent_service.observability import chunk_feedback

    rows = chunk_feedback.list_feedback(
        run_id=run_id, collection=collection, label=label, content_id=content_id, status=status, limit=limit
    )
    return [ChunkFeedbackOut(**r) for r in rows]


@router.post("/chunk-feedback/{feedback_id}/status", response_model=ChunkFeedbackOut)
def set_chunk_feedback_status(feedback_id: str, body: ChunkFeedbackStatusIn) -> ChunkFeedbackOut:
    """Dispensa uma marcação que não procedia, resolve sem editar o documento por aqui
    (ex.: corrigido na fonte de um site) ou reabre. Corrigir editando o documento é o
    `PUT /collections/{nome}/documents/{content_id}`, que resolve na revisão nova."""
    from agent_service.observability import chunk_feedback

    row = chunk_feedback.set_status(feedback_id, body.status, by=body.by, note=body.note)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Marcação {feedback_id!r} não encontrada.")
    return ChunkFeedbackOut(**row)


@router.delete("/chunk-feedback/{feedback_id}", status_code=204)
def delete_chunk_feedback(feedback_id: str) -> None:
    from agent_service.observability import chunk_feedback

    if not chunk_feedback.remove(feedback_id):
        raise HTTPException(status_code=404, detail=f"Marcação {feedback_id!r} não encontrada.")


@router.get("/chunk-feedback/flagged", response_model=list[FlaggedDocument])
def flagged_documents(
    collection: str | None = None,
    label: Literal["relevant", "irrelevant", "wrong"] = "wrong",
    status: Literal["open", "resolved", "dismissed", "all"] = "open",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[FlaggedDocument]:
    """Documentos com mais marcações (padrão: `wrong`, errado/desatualizado, só as abertas),
    com os runs. Só aponta: o que fazer com o documento é decisão de quem opera a base."""
    from agent_service.observability import chunk_feedback

    groups = chunk_feedback.flagged_documents(
        collection=collection, label=label, status=None if status == "all" else status, limit=limit
    )
    return [FlaggedDocument(**g) for g in groups]


@router.get("/chunk-feedback/export", response_model=list[RagCase])
def export_chunk_feedback(
    collection: str | None = None,
    agent_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[RagCase]:
    """Gabarito de um eval de RAG: uma linha por pergunta, com os trechos e documentos
    marcados `relevant`. `kuro runs chunk-export` grava isto em JSONL."""
    from agent_service.observability import chunk_feedback

    rows = chunk_feedback.export_relevant(collection=collection, agent_type=agent_type, since=since, until=until)
    return [RagCase(**r) for r in rows]
