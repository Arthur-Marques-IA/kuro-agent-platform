"""O panorama do dashboard (`observability/overview.py`): série sem buracos no fuso
pedido, período anterior, uma linha por agente com as versões, tools que falham
e os testes do Playground (`dry_run`) fora por padrão."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from agent_service.api.observability_routes import overview
from agent_service.observability import run_store, trace_store
from agent_service.observability.overview import _percentile, auto_granularity, build_overview
from agent_service.observability.run_store import RunRecord, SpanRecord, save_score

UTC = timezone.utc
# Uma janela só deste arquivo: o banco da suíte é compartilhado.
BASE = datetime(2031, 3, 10, tzinfo=UTC)


@pytest.fixture(autouse=True)
def local_backend():
    trace_store.set_trace_store(None)
    yield
    trace_store.set_trace_store(None)


@pytest.fixture
def agente() -> str:
    return f"painel-{uuid4().hex[:8]}"


def gravar(agent_type, started_at, *, status="success", version=1, latency_ms=1000.0, cost=0.01,
           session=None, tool_failures=0, tools=(), metadata=None) -> str:
    run_id = str(uuid4())
    spans = [
        SpanRecord(type="TOOL", name=name, started_at=started_at,
                   level="ERROR" if i < tool_failures else "DEFAULT",
                   metadata={"failure": "unavailable", "http_status": 500} if i < tool_failures else {})
        for i, name in enumerate(tools)
    ]
    run_store._write_run(RunRecord(
        run_id=run_id, trace_id=run_id.replace("-", ""), agent_type=agent_type, agent_name=agent_type.upper(),
        prompt_version=1, endpoint="chat", user_id="u", session_id=session or f"s-{uuid4().hex[:6]}",
        message="oi", started_at=started_at, ended_at=started_at + timedelta(milliseconds=latency_ms),
        status=status, cost_usd=cost, total_tokens=100, agent_version=version, spans=spans, metadata=metadata or {},
    ))
    return run_id


def painel(agente, since, until, **kwargs):
    return build_overview(since=since, until=until, agent_type=agente, **kwargs)


def test_dias_sem_execucao_viram_zero_e_o_fuso_decide_o_dia(agente):
    # 01:30 UTC do dia 11 ainda é dia 10 em São Paulo (UTC-3).
    gravar(agente, BASE + timedelta(days=1, hours=1, minutes=30))
    gravar(agente, BASE + timedelta(days=4, hours=15))
    p = painel(agente, BASE, BASE + timedelta(days=7))

    assert p.granularity == "day"
    assert len(p.buckets) == 8  # 10 a 17 no fuso local: o dia 10 começa às 03:00 UTC
    por_dia = {b.start[:10]: b.runs for b in p.buckets}
    assert por_dia["2031-03-10"] == 1, "01:30 UTC conta no dia anterior em São Paulo"
    assert por_dia["2031-03-11"] == 0
    assert por_dia["2031-03-14"] == 1
    assert sum(por_dia.values()) == 2


def test_granularidade_automatica():
    assert auto_granularity(BASE, BASE + timedelta(hours=24)) == "hour"
    assert auto_granularity(BASE, BASE + timedelta(days=30)) == "day"
    assert auto_granularity(BASE, BASE + timedelta(days=180)) == "week"


def test_totais_separam_erro_interrompido_e_tool_falhando(agente):
    t = BASE + timedelta(hours=12)
    gravar(agente, t)
    gravar(agente, t, status="error")
    gravar(agente, t, status="interrupted")
    gravar(agente, t, tools=("ficha", "lead"), tool_failures=1)
    p = painel(agente, BASE, BASE + timedelta(days=1))

    assert (p.totals.runs, p.totals.success, p.totals.errors, p.totals.interrupted) == (4, 2, 1, 1)
    assert p.totals.tool_failure_runs == 1
    assert (p.totals.tool_calls, p.totals.tool_failures) == (2, 1)
    bucket = next(b for b in p.buckets if b.runs)
    assert (bucket.success, bucket.tool_failure_runs, bucket.errors) == (1, 1, 2)


def test_testes_do_playground_ficam_de_fora_por_padrao(agente):
    t = BASE + timedelta(hours=12)
    gravar(agente, t)
    gravar(agente, t, metadata={"dry_run": "true"})
    assert painel(agente, BASE, BASE + timedelta(days=1)).totals.runs == 1
    assert painel(agente, BASE, BASE + timedelta(days=1), include_dry_run=True).totals.runs == 2


def test_periodo_anterior_para_as_variacoes(agente):
    gravar(agente, BASE + timedelta(days=-3))
    gravar(agente, BASE + timedelta(days=-2))
    gravar(agente, BASE + timedelta(days=1))
    p = painel(agente, BASE, BASE + timedelta(days=7))
    assert p.totals.runs == 1
    assert p.previous is not None and p.previous.runs == 2


def test_versoes_do_agente_com_metricas_proprias(agente):
    t = BASE + timedelta(hours=12)
    for _ in range(3):
        gravar(agente, t, version=1, cost=0.02)
    gravar(agente, t, version=1, status="error", cost=0.02)
    for _ in range(2):
        gravar(agente, t + timedelta(hours=2), version=2, cost=0.01)
    run_id = gravar(agente, t + timedelta(hours=2), version=2, cost=0.01)
    save_score(run_id=run_id, name="feedback", value=0, user_id="avaliador")

    [linha] = painel(agente, BASE, BASE + timedelta(days=1)).agents
    v2, v1 = linha.versions
    assert (v2.agent_version, v2.runs, v2.error_rate, v2.feedback_down) == (2, 3, 0.0, 1)
    assert (v1.agent_version, v1.runs, v1.error_rate) == (1, 4, 0.25)
    assert v1.cost_per_run_usd == pytest.approx(0.02)
    assert linha.totals.runs == 7
    assert sum(linha.trend) == 7


def test_custo_por_conversa(agente):
    t = BASE + timedelta(hours=12)
    gravar(agente, t, session="conversa-1", cost=0.03)
    gravar(agente, t, session="conversa-1", cost=0.01)
    gravar(agente, t, session="conversa-2", cost=0.02)
    [linha] = painel(agente, BASE, BASE + timedelta(days=1)).agents
    assert linha.totals.sessions == 2
    assert linha.cost_per_session_usd == pytest.approx(0.03)


def test_tools_que_falham_agrupadas_por_tipo(agente):
    t = BASE + timedelta(hours=12)
    gravar(agente, t, tools=("ficha",), tool_failures=1)
    gravar(agente, t + timedelta(hours=1), tools=("ficha",), tool_failures=1)
    [falha] = painel(agente, BASE, BASE + timedelta(days=1)).tool_failures
    assert (falha.tool_name, falha.failure, falha.count, falha.http_status) == ("ficha", "unavailable", 2, [500])
    assert falha.agent_types == [agente]


def test_p95_por_interpolacao():
    assert _percentile([], 0.95) is None
    assert _percentile([100.0] * 19 + [2000.0], 0.95) == pytest.approx(195.0)


def test_rota_recusa_fuso_invalido_e_intervalo_invertido():
    with pytest.raises(HTTPException) as fuso:
        overview(since=BASE, until=BASE + timedelta(days=1), tz="Marte/Olympus")
    assert fuso.value.status_code == 422
    with pytest.raises(HTTPException) as invertido:
        overview(since=BASE + timedelta(days=1), until=BASE)
    assert invertido.value.status_code == 422


def test_rota_recusa_datas_implausiveis_sem_estourar():
    # O período anterior de uma janela que começa no ano 1 cairia antes do ano 1 (OverflowError).
    with pytest.raises(HTTPException) as antigo:
        overview(since=datetime(1, 1, 2, tzinfo=UTC), until=BASE)
    assert antigo.value.status_code == 422


# -- fila de revisão (filtros de /observability/runs) ---------------------------------


def listar(agente, **filtros):
    from agent_service.api.observability_routes import list_runs

    base = dict(agent_type=agente, prompt_version=None, status=None, user_id=None, session_id=None,
                since=None, until=None, limit=50, cursor=None, agent_version=None, meta=None, search=None,
                complexity=None, tool_failed=None, side_effect=None, min_message_chars=None, feedback=None,
                include_dry_run=True, sample=None)
    return {r.run_id for r in list_runs(**{**base, **filtros}).items}


def test_fila_de_revisao(agente):
    from agent_service.api.tools_routes import ToolIn, create_tool, delete_tool

    t = BASE + timedelta(hours=12)
    trivial = gravar(agente, t)  # mensagem "oi", sem tool
    falhou = gravar(agente, t, tools=("grava_ficha",), tool_failures=1)
    complexo = gravar(agente, t, tools=("grava_ficha", "consulta", "outra"))
    teste = gravar(agente, t, metadata={"dry_run": "true"})
    run_store._write_run(RunRecord(
        run_id=(longo := str(uuid4())), trace_id="x", agent_type=agente, agent_name=None, prompt_version=1,
        endpoint="chat", user_id="u", session_id="s", message="quero parcelar a mensalidade em 6 vezes",
        started_at=t, status="success",
    ))
    save_score(run_id=longo, name="feedback", value=0, user_id="avaliador")

    create_tool(ToolIn(tool_name="grava_ficha", kind="api", label="Grava", side_effect=True,
                       config={"method": "POST", "url": "https://exemplo.test/ficha", "parameters": []}))
    try:
        assert listar(agente, complexity=[3]) == {complexo}
        assert listar(agente, complexity=[2, 3]) == {falhou, complexo}
        assert listar(agente, tool_failed=True) == {falhou}
        assert listar(agente, side_effect=True) == {falhou, complexo}
        assert listar(agente, min_message_chars=10) == {longo}
        assert listar(agente, feedback="down") == {longo}
        assert teste not in listar(agente, include_dry_run=False)
        assert trivial in listar(agente, include_dry_run=False)
        amostra = listar(agente, sample=2)
        assert len(amostra) == 2 and amostra <= {trivial, falhou, complexo, teste, longo}
    finally:
        delete_tool("grava_ficha")


def test_filtros_de_revisao_pela_http(agente):
    """A validação de parâmetro repetido (`complexity=2&complexity=3`) só aparece pela HTTP."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent_service.api.observability_routes import router

    t = BASE + timedelta(hours=12)
    gravar(agente, t)
    dois = gravar(agente, t, tools=("a",))
    tres = gravar(agente, t, tools=("a", "b", "c"))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resposta = client.get("/observability/runs", params=[("agent_type", agente), ("complexity", 2), ("complexity", 3)])
    assert resposta.status_code == 200
    assert {r["run_id"] for r in resposta.json()["items"]} == {dois, tres}
    assert client.get("/observability/runs", params={"complexity": 4}).status_code == 422


def test_coleta_de_sites_entra_no_custo_do_panorama_geral(agente):
    from agent_service.documents import sources_store

    # Uma janela só deste teste: o panorama geral soma o banco inteiro.
    inicio = datetime(2033, 5, 1, 12, tzinfo=UTC)
    gravar(agente, inicio + timedelta(days=1), cost=0.01)
    sources_store.record_sync_cost(source_id="s1", collection="docs", model="gemini-2.5-flash", input_tokens=200,
                                   output_tokens=100, cost_usd=0.002, ended_at=inicio + timedelta(days=2))
    sources_store.record_sync_cost(source_id="s1", collection="docs", model="gemini-2.5-flash", input_tokens=10,
                                   output_tokens=5, cost_usd=0.004, ended_at=inicio - timedelta(days=3))

    geral = build_overview(since=inicio, until=inicio + timedelta(days=7))
    assert geral.totals.cost_usd == pytest.approx(0.012)
    assert (geral.totals.ingestion_cost_usd, geral.totals.ingestion_tokens) == (0.002, 300)
    assert geral.totals.total_tokens == 100, "tokens do formatador não entram no total dos runs"
    assert sum(b.cost_usd for b in geral.buckets) == pytest.approx(0.012)
    assert sum(b.ingestion_cost_usd for b in geral.buckets) == pytest.approx(0.002)
    # Só a coleta no período anterior: ele existe e tem o custo dela.
    assert geral.previous is not None and geral.previous.cost_usd == pytest.approx(0.004)

    # Filtrado por agente, a coleta (que não é de agente nenhum) fica de fora.
    do_agente = painel(agente, inicio, inicio + timedelta(days=7))
    assert do_agente.totals.cost_usd == pytest.approx(0.01)
    assert do_agente.totals.ingestion_cost_usd is None


# -- vários agentes, vários status e ordenação (os KPIs do Panorama levam às Execuções) ---


def _cliente():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent_service.api.observability_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_panorama_de_varios_agentes(agente):
    outro, fora = f"{agente}-b", f"{agente}-c"
    t = BASE + timedelta(hours=12)
    gravar(agente, t)
    gravar(outro, t, status="error")
    gravar(fora, t)

    p = build_overview(since=BASE, until=BASE + timedelta(days=1), agent_type=[agente, outro])
    assert p.totals.runs == 2 and p.totals.errors == 1
    assert {a.agent_type for a in p.agents} == {agente, outro}

    # Pela HTTP, `agent_type` repetido; um só continua valendo.
    client = _cliente()
    params = [("since", BASE.isoformat()), ("until", (BASE + timedelta(days=1)).isoformat())]
    varios = client.get("/observability/overview", params=[*params, ("agent_type", agente), ("agent_type", outro)])
    assert varios.status_code == 200 and varios.json()["totals"]["runs"] == 2
    um = client.get("/observability/overview", params=[*params, ("agent_type", fora)])
    assert um.json()["totals"]["runs"] == 1


def test_execucoes_sessoes_e_estatisticas_de_varios_agentes_e_status(agente):
    outro = f"{agente}-b"
    t = BASE + timedelta(days=2)
    ok = gravar(agente, t)
    erro = gravar(agente, t, status="error")
    interrompida = gravar(outro, t, status="interrupted")
    gravar(f"{agente}-fora", t, status="error")
    client = _cliente()
    agentes = [("agent_type", agente), ("agent_type", outro)]

    runs = client.get("/observability/runs", params=agentes).json()["items"]
    assert {r["run_id"] for r in runs} == {ok, erro, interrompida}
    falharam = client.get("/observability/runs", params=[*agentes, ("status", "error"), ("status", "interrupted")])
    assert {r["run_id"] for r in falharam.json()["items"]} == {erro, interrompida}
    assert client.get("/observability/runs", params=[*agentes, ("status", "talvez")]).status_code == 422

    sessoes = client.get("/observability/sessions", params=agentes).json()["items"]
    assert len(sessoes) == 3
    stats = client.get("/observability/stats", params=[*agentes, ("status", "error"), ("status", "interrupted")]).json()
    assert stats["status_counts"] == {"error": 1, "interrupted": 1}

    # Chamada direta (outros módulos, testes) ainda passa um valor só.
    assert listar(agente, status="error") == {erro}


def test_ordenar_pelas_mais_caras_e_mais_lentas_pagina_pela_ordem(agente):
    t = BASE + timedelta(days=3)
    barata = gravar(agente, t, cost=0.001, latency_ms=9000)
    media = gravar(agente, t + timedelta(minutes=1), cost=0.05, latency_ms=100)
    cara = gravar(agente, t + timedelta(minutes=2), cost=0.5, latency_ms=3000)
    empate = gravar(agente, t + timedelta(minutes=3), cost=0.05, latency_ms=500)
    sem_custo = gravar(agente, t + timedelta(minutes=4), cost=None)
    client = _cliente()

    def pagina(sort, cursor=None):
        params = {"agent_type": agente, "sort": sort, "limit": 2, **({"cursor": cursor} if cursor else {})}
        resposta = client.get("/observability/runs", params=params)
        assert resposta.status_code == 200
        return resposta.json()

    primeira = pagina("cost")
    segunda = pagina("cost", primeira["next_cursor"])
    ids = [r["run_id"] for r in primeira["items"] + segunda["items"]]
    # O empate de custo sai numa ordem estável (run_id), sem repetir nem pular na virada da página.
    assert ids[0] == cara and ids[-1] == barata and set(ids[1:3]) == {media, empate}
    assert sem_custo not in ids, "run sem custo não entra na fila das mais caras"
    assert segunda["next_cursor"] is None

    lentas = [r["run_id"] for r in pagina("latency")["items"]]
    assert lentas == [barata, cara]
    # O cursor de uma ordenação não vale em outra: recomeça do topo em vez de errar.
    assert [r["run_id"] for r in pagina("latency", primeira["next_cursor"])["items"]] == lentas
    assert client.get("/observability/runs", params={"sort": "preco"}).status_code == 422
