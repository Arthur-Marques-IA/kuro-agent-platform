"""Testes da leitura de traces (`LangfuseTraceStore`) e das rotas que a expõem.

A API do Langfuse é simulada com `httpx.MockTransport`, no formato verificado
contra um Langfuse v4 real (observations v2 e scores v3).
"""

import json
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from agent_service.api.observability_routes import get_run_trace, list_agent_runs, list_runs, list_sessions, run_stats
from agent_service.observability import trace_store
from agent_service.observability.trace_store import LangfuseTraceStore, RunQuery, TraceStoreError
from agent_service.observability.tracing import trace_id_for_run

RUN_ID = "485e27d0-a3d3-426b-8159-99b949fe791d"
TRACE_ID = trace_id_for_run(RUN_ID)


def observation(**overrides: Any) -> dict[str, Any]:
    base = {
        "traceId": TRACE_ID,
        "startTime": "2026-09-16T15:48:03.744Z",
        "endTime": "2026-09-16T15:48:20.025Z",
        "level": "DEFAULT",
        "statusMessage": "",
        "version": "prompt-v2",
        "environment": "development",
        "userId": "u1",
        "sessionId": "s1",
        "isRootObservation": False,
        "traceName": "conversational",
        "metadata": {},
        "inputUsage": 0,
        "outputUsage": 0,
        "totalUsage": 0,
        "totalCost": None,
        "model": "",
    }
    return {**base, **overrides}


ROOT = observation(
    id="root",
    # A raiz real aponta para um pai externo (contexto remoto do trace).
    parentObservationId="external",
    isRootObservation=True,
    type="SPAN",
    name="chat",
    input=json.dumps({"message": "oi", "dependencies": None}),
    output="olá",
    latency=16.281,
    metadata={
        "run_id": RUN_ID,
        "agent_type": "conversational",
        "agent_name": "Agente Conversacional",
        "prompt_version": 2,
        "endpoint": "chat",
        "resourceAttributes.service.name": "unknown_service",
    },
)
AGENT = observation(
    id="agent",
    parentObservationId="root",
    type="AGENT",
    name="arun",
    startTime="2026-09-16T15:48:06.338Z",
    input="oi",
    output="olá",
    latency=13.6,
)
GENERATION = observation(
    id="gen",
    parentObservationId="agent",
    type="GENERATION",
    name="Gemini.ainvoke_stream",
    startTime="2026-09-16T15:48:18.503Z",
    input=json.dumps({"messages": [{"role": "user", "content": "oi"}]}),
    output="não é json",
    latency=1.358,
    model="gemini-2.5-flash",
    inputUsage=367,
    outputUsage=1,
    totalUsage=368,
    totalCost=0.0001126,
    metadata={"attributes.llm.provider": "Google", "attributes.langfuse.version": "prompt-v2"},
)
TOOL = observation(
    id="tool", parentObservationId="agent", type="TOOL", name="buscar", latency=0.2, totalUsage=10, totalCost=0.00001
)
RUN_ID_2 = "6e1e3b0a-6b8b-4b8a-9c8a-6b2f6e9f6a10"
TRACE_ID_2 = trace_id_for_run(RUN_ID_2)
ROOT_2 = observation(
    id="root2",
    traceId=TRACE_ID_2,
    parentObservationId="external",
    isRootObservation=True,
    type="SPAN",
    name="chat",
    startTime="2026-09-16T16:10:00.000Z",
    endTime="2026-09-16T16:10:05.000Z",
    latency=5.0,
    level="ERROR",
    statusMessage="falhou",
    sessionId="s1",
    input=json.dumps({"message": "de novo", "dependencies": None}),
    metadata={"run_id": RUN_ID_2, "agent_type": "conversational", "endpoint": "chat"},
)
GENERATION_2 = observation(
    id="gen2",
    traceId=TRACE_ID_2,
    parentObservationId="root2",
    type="GENERATION",
    name="Gemini.ainvoke_stream",
    startTime="2026-09-16T16:10:02.000Z",
    model="gemini-2.5-flash",
    inputUsage=100,
    outputUsage=20,
    totalUsage=120,
    totalCost=0.00005,
)
SCORES = [
    {
        "id": "s-up",
        "name": "feedback",
        "value": True,
        "dataType": "BOOLEAN",
        "source": "API",
        "timestamp": "2026-09-16T15:48:21Z",
        "metadata": {"user_id": "u1"},
        "subject": {"kind": "trace", "id": TRACE_ID},
    },
    {
        "id": "s-down",
        "name": "feedback",
        "value": False,
        "dataType": "BOOLEAN",
        "source": "API",
        "timestamp": "2026-09-16T15:49:00Z",
        "metadata": {"user_id": "u2"},
        "subject": {"kind": "trace", "id": TRACE_ID},
    },
    {
        "id": "s-other",
        "name": "feedback",
        "value": True,
        "dataType": "BOOLEAN",
        "source": "API",
        "timestamp": "2026-09-16T15:50:00Z",
        "subject": {"kind": "trace", "id": "outro-trace"},
    },
]


class FakeLangfuse:
    """Responde como a API do Langfuse e guarda os filtros recebidos."""

    def __init__(
        self,
        observations: list[dict[str, Any]],
        scores: list[dict[str, Any]],
        status: int = 200,
        endless_scores: bool = False,
    ) -> None:
        self.observations = observations
        self.scores = scores
        self.status = status
        self.endless_scores = endless_scores  # sempre devolve cursor: simula milhares de scores
        self.requests: list[httpx.Request] = []

    def filters(self, index: int) -> list[dict[str, Any]]:
        return json.loads(self.requests[index].url.params["filter"])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="boom")
        if request.url.path == "/api/public/v2/observations":
            filters = json.loads(request.url.params["filter"])
            items = [o for o in self.observations if all(self._matches(o, f) for f in filters)]
            return httpx.Response(200, json={"data": items, "meta": {}})
        if request.url.path == "/api/public/v3/scores":
            trace_id = request.url.params.get("traceId")
            items = [s for s in self.scores if trace_id is None or s["subject"]["id"] == trace_id]
            meta = {"limit": 100, **({"cursor": "mais"} if self.endless_scores else {})}
            return httpx.Response(200, json={"data": items, "meta": meta})
        return httpx.Response(404)

    @staticmethod
    def _matches(item: dict[str, Any], condition: dict[str, Any]) -> bool:
        column, operator, value = condition["column"], condition["operator"], condition.get("value")
        if column == "metadata":
            return (item.get("metadata") or {}).get(condition["key"]) == value
        actual = {"level": item.get("level"), "startTime": item.get("startTime")}.get(column, item.get(column))
        if operator == "any of":
            return actual in value
        if operator == ">=":
            return actual >= value
        if operator == "<":
            return actual < value
        return actual == value


def make_store(fake: FakeLangfuse, environment: str | None = "development") -> LangfuseTraceStore:
    return LangfuseTraceStore(
        base_url="http://langfuse.test",
        public_key="pk",
        secret_key="sk",
        environment=environment,
        timeout=5,
        transport=httpx.MockTransport(fake),
    )


def test_list_runs_sums_usage_from_children_and_counts_feedback() -> None:
    fake = FakeLangfuse([ROOT, AGENT, GENERATION, TOOL], SCORES)
    page = make_store(fake).list_runs(RunQuery(agent_type="conversational"))

    [run] = page.items
    assert run.run_id == RUN_ID
    assert run.trace_id == TRACE_ID
    assert run.prompt_version == 2
    assert run.message == "oi"
    assert run.output == "olá"
    assert run.status == "success"
    assert run.latency_ms == 16281.0
    # Custo e tokens estão nos filhos, nunca na raiz.
    assert run.total_tokens == 378
    assert run.cost_usd == pytest.approx(0.0001226)
    assert run.model == "gemini-2.5-flash"
    assert (run.feedback_up, run.feedback_down) == (1, 1)

    roots_filter = fake.filters(0)
    assert {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True} in roots_filter
    assert {"type": "string", "column": "traceName", "operator": "=", "value": "conversational"} in roots_filter
    assert {"type": "stringOptions", "column": "environment", "operator": "any of", "value": ["development"]} in roots_filter
    # Uma única consulta de uso para a página inteira.
    assert fake.filters(1) == [{"type": "stringOptions", "column": "traceId", "operator": "any of", "value": [TRACE_ID]}]


def test_feedback_counts_are_unknown_when_scores_exceed_the_page_limit() -> None:
    fake = FakeLangfuse([ROOT, GENERATION], SCORES, endless_scores=True)
    store = make_store(fake)

    [run] = store.list_runs(RunQuery(agent_type="conversational")).items
    # Uma soma parcial seria um número errado; melhor "desconhecido".
    assert (run.feedback_up, run.feedback_down) == (None, None)
    assert run.total_tokens == 368  # o resto do resumo não é afetado

    trace = store.get_run(RUN_ID)
    assert trace is not None
    assert (trace.run.feedback_up, trace.run.feedback_down) == (None, None)


def test_list_runs_without_agent_type_omits_the_trace_name_filter() -> None:
    fake = FakeLangfuse([ROOT, GENERATION], SCORES)
    page = make_store(fake).list_runs(RunQuery(agent_type=None))

    assert [r.run_id for r in page.items] == [RUN_ID]
    assert not any(f["column"] == "traceName" for f in fake.filters(0))


def test_list_runs_translates_filters() -> None:
    fake = FakeLangfuse([], [])
    make_store(fake, environment=None).list_runs(
        RunQuery(agent_type="conversational", prompt_version=3, status="error", user_id="u9", tenant_id="acme")
    )

    filters = fake.filters(0)
    assert {"type": "string", "column": "version", "operator": "=", "value": "prompt-v3"} in filters
    assert {"type": "string", "column": "level", "operator": "=", "value": "ERROR"} in filters
    assert {"type": "string", "column": "userId", "operator": "=", "value": "u9"} in filters
    assert {"type": "stringObject", "column": "metadata", "key": "tenant_id", "operator": "=", "value": "acme"} in filters
    assert not any(f["column"] == "environment" for f in filters)
    assert len(fake.requests) == 1  # sem runs, não busca uso nem feedback


def test_list_runs_maps_levels_to_status() -> None:
    failed = {**ROOT, "level": "ERROR", "statusMessage": "quota"}
    page = make_store(FakeLangfuse([failed], [])).list_runs(RunQuery(agent_type="conversational"))
    assert page.items[0].status == "error"
    assert page.items[0].status_message == "quota"

    interrupted = {**ROOT, "level": "WARNING"}
    page = make_store(FakeLangfuse([interrupted], [])).list_runs(RunQuery(agent_type="conversational"))
    assert page.items[0].status == "interrupted"


def test_get_run_builds_tree_and_cleans_metadata() -> None:
    trace = make_store(FakeLangfuse([GENERATION, AGENT, ROOT], SCORES)).get_run(RUN_ID)

    assert trace is not None
    assert [s.id for s in trace.spans] == ["root", "agent", "gen"]  # ordem de início
    spans = {s.id: s for s in trace.spans}
    assert spans["root"].parent_id is None  # o pai externo da raiz é descartado
    assert spans["gen"].parent_id == "agent"
    assert spans["root"].input == {"message": "oi", "dependencies": None}
    assert spans["gen"].output == "não é json"
    assert "resourceAttributes.service.name" not in spans["root"].metadata
    assert spans["gen"].metadata == {"llm.provider": "Google"}
    assert trace.run.total_tokens == 368
    assert [s.id for s in trace.scores] == ["s-up", "s-down"]
    assert trace.scores[0].user_id == "u1"
    assert (trace.run.feedback_up, trace.run.feedback_down) == (1, 1)


def test_get_run_returns_none_while_not_indexed() -> None:
    assert make_store(FakeLangfuse([], [])).get_run(RUN_ID) is None


def test_langfuse_errors_become_trace_store_errors() -> None:
    with pytest.raises(TraceStoreError, match="401"):
        make_store(FakeLangfuse([], [], status=401)).list_runs(RunQuery(agent_type="conversational"))


@pytest.fixture
def installed_store():
    def install(store):
        trace_store.set_trace_store(store)
        return store

    yield install
    trace_store.set_trace_store(None)


def test_routes_return_503_when_the_langfuse_backend_is_chosen_but_disabled(installed_store, monkeypatch) -> None:
    installed_store(None)
    monkeypatch.setattr(trace_store, "get_langfuse", lambda: None)
    monkeypatch.setattr(trace_store.get_settings(), "trace_store_backend", "langfuse")
    for call in (lambda: list_agent_runs("conversational"), lambda: get_run_trace(RUN_ID)):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 503


def test_routes_map_upstream_failures_and_missing_traces(installed_store) -> None:
    installed_store(make_store(FakeLangfuse([], [], status=500)))
    with pytest.raises(HTTPException) as exc:
        list_agent_runs("conversational", limit=10)
    assert exc.value.status_code == 502

    installed_store(make_store(FakeLangfuse([], [])))
    with pytest.raises(HTTPException) as exc:
        get_run_trace(RUN_ID)
    assert exc.value.status_code == 404
    assert "indexado" in exc.value.detail

    installed_store(make_store(FakeLangfuse([ROOT, GENERATION], [])))
    assert get_run_trace(RUN_ID).run.total_tokens == 368


def test_list_runs_route_without_agent_type_returns_all_agents(installed_store) -> None:
    installed_store(make_store(FakeLangfuse([ROOT, GENERATION], [])))
    page = list_runs()
    assert [r.run_id for r in page.items] == [RUN_ID]


def test_several_agents_and_statuses_become_any_of_filters() -> None:
    fake = FakeLangfuse([ROOT, GENERATION], SCORES)
    query = RunQuery(agent_types=("conversational", "outro"), statuses=("error", "interrupted"))
    make_store(fake).list_runs(query)

    filters = fake.filters(0)
    assert {"type": "stringOptions", "column": "traceName", "operator": "any of", "value": ["conversational", "outro"]} in filters
    assert {"type": "stringOptions", "column": "level", "operator": "any of", "value": ["ERROR", "WARNING"]} in filters

    # As agregações (sessões, estatísticas) varrem com os mesmos filtros.
    stats = make_store(FakeLangfuse([ROOT, GENERATION], [])).get_stats(RunQuery(agent_types=("conversational", "outro")))
    assert stats.total_runs == 1


def test_a_single_agent_or_status_from_the_route_keeps_the_equals_filter(installed_store) -> None:
    fake = FakeLangfuse([ROOT, GENERATION], [])
    installed_store(make_store(fake))
    list_runs(agent_type=["conversational"], status=["error"])

    filters = fake.filters(0)
    assert {"type": "string", "column": "traceName", "operator": "=", "value": "conversational"} in filters
    assert {"type": "string", "column": "level", "operator": "=", "value": "ERROR"} in filters
    assert not any(f["type"] == "stringOptions" and f["column"] in ("traceName", "level") for f in filters)


def test_sort_by_cost_is_local_only(installed_store) -> None:
    installed_store(make_store(FakeLangfuse([ROOT, GENERATION], [])))
    with pytest.raises(HTTPException) as exc:
        list_runs(sort="cost")
    assert exc.value.status_code == 501


def test_list_sessions_groups_runs_of_the_same_session_and_sums_usage() -> None:
    fake = FakeLangfuse([ROOT, GENERATION, TOOL, ROOT_2, GENERATION_2], SCORES)
    page = make_store(fake).list_sessions(RunQuery(agent_type="conversational"))

    [session] = page.items  # ROOT e ROOT_2 são a mesma sessão "s1"
    assert session.session_id == "s1"
    assert session.run_count == 2
    assert session.agent_types == ["conversational"]
    assert session.total_tokens == 378 + 120
    assert session.cost_usd == pytest.approx(0.0001126 + 0.00001 + 0.00005)
    assert session.error_count == 1
    assert session.started_at.isoformat() == "2026-09-16T15:48:03.744000+00:00"
    assert session.last_activity.isoformat() == "2026-09-16T16:10:00+00:00"
    # Scores só existem para o trace de ROOT: contam para a sessão como um todo.
    assert (session.feedback_up, session.feedback_down) == (1, 1)
    assert page.scanned == 2  # só as raízes (GENERATION/TOOL não são isRootObservation)


def test_list_sessions_keeps_different_sessions_apart_and_sorts_by_recency() -> None:
    other_session_root = {**ROOT_2, "id": "root3", "sessionId": "s2", "traceId": trace_id_for_run("outro-run")}
    fake = FakeLangfuse([ROOT, other_session_root], [])

    page = make_store(fake).list_sessions(RunQuery(agent_type="conversational"))

    assert [s.session_id for s in page.items] == ["s2", "s1"]  # s2 é mais recente


def test_get_stats_buckets_by_day_and_sums_status() -> None:
    fake = FakeLangfuse([ROOT, GENERATION, TOOL, ROOT_2, GENERATION_2], [])

    stats = make_store(fake).get_stats(RunQuery(agent_type="conversational"))

    assert stats.scanned == 2
    assert stats.total_runs == 2
    assert stats.total_tokens == 378 + 120
    assert stats.status_counts == {"success": 1, "error": 1}
    [bucket] = stats.buckets  # ROOT e ROOT_2 caem no mesmo dia UTC
    assert bucket.date == "2026-09-16"
    assert bucket.runs == 2
    assert bucket.errors == 1
    assert bucket.total_tokens == 378 + 120


def test_sessions_and_stats_routes(installed_store) -> None:
    installed_store(make_store(FakeLangfuse([ROOT, GENERATION], [])))

    assert [s.session_id for s in list_sessions().items] == ["s1"]
    assert run_stats().total_runs == 1
