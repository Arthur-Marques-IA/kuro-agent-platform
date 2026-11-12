"""O que só um Postgres de verdade exercita — e o SQLite do resto da suíte deixa passar.

Pulados sem `TEST_DATABASE_URL` (ver `tests/pg.py` e docs/desenvolvimento.md):

- migrações: do zero até a última revisão num banco limpo, o schema batendo com as
  tabelas do código, e o caminho de volta (as revisões com `downgrade`, 0018 → 0007);
- fila de jobs: `FOR UPDATE SKIP LOCKED` (o SQLite não tem; lá quem segura é o
  `status='queued'` do UPDATE) e `LISTEN/NOTIFY` acordando o worker;
- busca híbrida: pgvector + `tsvector` numa collection pequena, com embedder falso;
- trace store: o cursor da linha do tempo da sessão (`timestamptz` com microssegundos
  e empate de `started_at`) e as colunas JSON gravando SQL NULL, não o JSON `null`.
"""

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import psycopg
import pytest
import sqlalchemy as sa
from agno.knowledge.embedder.base import Embedder
from alembic import command
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet

from agent_service.agents import procedure_store, store as agents_store, versions as agent_versions
from agent_service.api import idempotency
from agent_service.config import get_settings
from agent_service.db import get_db
from agent_service.documents import collections
from agent_service.documents import revisions, sources_store, store as documents_store, versions as collection_versions
from agent_service.documents.search import SearchConfig
from agent_service.jobs import store as jobs
from agent_service.jobs import worker as jobs_worker
from agent_service.jobs.worker import Worker
from agent_service.migrations import _config, upgrade_database
from agent_service.models import store as models_store
from agent_service.observability import chunk_feedback, run_store
from agent_service.observability.run_store import RunRecord
from agent_service.tools import store as tools_store

pytestmark = pytest.mark.postgres

HEAD = ScriptDirectory.from_config(_config()).get_current_head()

TABELAS_DO_CODIGO = [
    m.metadata
    for m in (
        agents_store,
        agent_versions,
        procedure_store,
        idempotency,
        documents_store,
        revisions,
        sources_store,
        collection_versions,
        jobs,
        models_store,
        chunk_feedback,
        run_store,
        tools_store,
    )
]
"""As tabelas como o código as declara — o banco migrado precisa ter todas as colunas."""


# -- migrações -------------------------------------------------------------------


def _alembic(engine: sa.Engine, action: Any, revision: str) -> None:
    """`command.upgrade`/`command.downgrade` com a conexão, como `upgrade_database` faz."""
    config = _config()
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        action(config, revision)


def _revisao(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()


def _colunas(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _config_da_tool(engine: sa.Engine, name: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT config FROM tool_definitions WHERE tool_name = :n"), {"n": name}
        ).scalar_one()


def test_migracoes_do_zero_ate_head_e_de_volta(banco_postgres_vazio):
    engine = banco_postgres_vazio

    upgrade_database(engine)
    upgrade_database(engine)  # idempotente
    assert _revisao(engine) == HEAD

    # O schema que as migrações montam é o que o código usa: nenhuma coluna declarada faltando.
    inspector = sa.inspect(engine)
    existentes = set(inspector.get_table_names())
    faltando = {
        f"{table.name}.{column.name}"
        for md in TABELAS_DO_CODIGO
        for table in md.tables.values()
        for column in table.columns
        if table.name not in existentes or column.name not in {c["name"] for c in inspector.get_columns(table.name)}
    }
    assert not faltando, f"colunas do código sem migração: {sorted(faltando)}"

    with engine.connect() as conn:
        # O índice parcial que impede dois jobs ativos com a mesma dedupe_key.
        indexdef = conn.execute(
            sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_jobs_dedupe_active'")
        ).scalar_one()
        # As colunas da entrada estruturada (0016) são `json` de verdade, não texto.
        tipos = dict(
            conn.execute(
                sa.text(
                    "SELECT column_name, data_type FROM information_schema.columns"
                    " WHERE table_name = 'runs' AND column_name IN ('input_events', 'input_context', 'input_message')"
                )
            ).all()
        )
    assert "UNIQUE" in indexdef and "WHERE" in indexdef and "queued" in indexdef
    assert tipos == {"input_events": "json", "input_context": "json", "input_message": "text"}

    # 0017 de ida e de volta, com um segredo em texto plano gravado antes dela.
    _alembic(engine, command.downgrade, "0016")
    assert _revisao(engine) == "0016"
    config = {
        "url": "https://api.exemplo.test/x",
        "auth": {"type": "bearer", "token": "segredo-123"},
        "headers": {"X-Api-Key": "chave-456", "Accept": "application/json"},
    }
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tool_definitions (tool_name, kind, label, config, enabled, is_seed)"
                " VALUES ('legada', 'api', 'Legada', CAST(:config AS json), true, false)"
            ),
            {"config": json.dumps(config)},
        )
    _alembic(engine, command.upgrade, "0017")
    cifrada = _config_da_tool(engine, "legada")
    fernet = Fernet(os.environ["CREDENTIALS_ENCRYPTION_KEY"].encode())
    for valor, original in ((cifrada["auth"]["token"], "segredo-123"), (cifrada["headers"]["X-Api-Key"], "chave-456")):
        assert valor.startswith("enc:v1:")
        assert fernet.decrypt(valor[len("enc:v1:") :].encode()).decode() == original
    assert cifrada["headers"]["Accept"] == "application/json"
    _alembic(engine, command.downgrade, "0016")
    assert _config_da_tool(engine, "legada") == config

    # Até onde há downgrade (0001–0007 não têm): 0018 → 0007, passando por todas.
    _alembic(engine, command.downgrade, "0007")
    assert _revisao(engine) == "0007"
    tabelas = set(sa.inspect(engine).get_table_names())
    assert not {"jobs", "idempotency_keys", "knowledge_sources", "chunk_feedback", "document_revisions"} & tabelas
    assert not {"input_events", "input_message", "collection_version"} & _colunas(engine, "runs")
    assert not {"plain_text", "history_tool_calls"} & _colunas(engine, "agent_definitions")

    # E de volta até a última, sem perder o que já estava gravado.
    upgrade_database(engine)
    assert _revisao(engine) == HEAD
    assert {"jobs", "idempotency_keys", "document_revisions"} <= set(sa.inspect(engine).get_table_names())
    assert _config_da_tool(engine, "legada")["auth"]["token"].startswith("enc:v1:")


# -- fila de jobs -----------------------------------------------------------------


@pytest.fixture
def kind(monkeypatch):
    """Um `kind` só deste teste: o banco da sessão é compartilhado com o resto da suíte."""
    monkeypatch.setattr(jobs_worker, "HANDLERS", {})
    return f"teste.pg.{uuid4().hex[:8]}"


def test_skip_locked_claim_pula_o_job_travado_sem_esperar(kind):
    agora = datetime.now(timezone.utc)
    primeiro = jobs.enqueue(kind, {"n": 1}, run_after=agora - timedelta(minutes=1))
    segundo = jobs.enqueue(kind, {"n": 2}, run_after=agora - timedelta(seconds=1))
    lease = timedelta(seconds=30)

    with jobs._engine().connect() as travador, ThreadPoolExecutor(1) as pool:
        # Outra transação segura a linha do primeiro da fila (como um claim em andamento).
        transacao = travador.begin()
        travador.execute(sa.select(jobs.jobs.c.id).where(jobs.jobs.c.id == primeiro["id"]).with_for_update())
        try:
            # Sem SKIP LOCKED, o claim ficaria esperando o lock — o timeout reprova em vez de travar.
            pego = pool.submit(jobs.claim, "w-pg", [kind], lease).result(timeout=10)
            assert pego is not None and pego["id"] == segundo["id"]
            # Só sobrou o travado: nada para pegar, e na hora.
            assert pool.submit(jobs.claim, "w-pg", [kind], lease).result(timeout=10) is None
        finally:
            transacao.rollback()

    # Solto o lock, o primeiro volta a ser pegável.
    assert jobs.claim("w-pg", [kind], lease)["id"] == primeiro["id"]


def test_skip_locked_varios_consumidores_cada_job_uma_vez(kind):
    ids = [jobs.enqueue(kind, {"n": n})["id"] for n in range(40)]
    lease = timedelta(seconds=30)

    def consome(n: int) -> list[str]:
        pegos = []
        while (job := jobs.claim(f"w{n}", [kind], lease)) is not None:
            pegos.append(job["id"])
        return pegos

    with ThreadPoolExecutor(8) as pool:
        por_consumidor = list(pool.map(consome, range(8), timeout=60))

    pegos = [i for lista in por_consumidor for i in lista]
    assert len(pegos) == len(set(pegos)), "um job foi pego por dois consumidores"
    assert sorted(pegos) == sorted(ids)
    assert {jobs.get_job(i)["status"] for i in ids} == {"running"}


def _url_psycopg() -> str:
    return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def test_notify_sai_so_no_commit_com_o_kind(kind):
    with psycopg.connect(_url_psycopg(), autocommit=True) as ouvinte:
        ouvinte.execute(f"LISTEN {jobs.NOTIFY_CHANNEL}")
        with jobs._engine().begin() as conn:
            jobs.enqueue(kind, conn=conn)
            # Ainda dentro da transação: o worker não pode acordar para um job que não existe.
            assert list(ouvinte.notifies(timeout=0.5, stop_after=1)) == []
        recebidas = list(ouvinte.notifies(timeout=5, stop_after=1))
    assert [(n.channel, n.payload) for n in recebidas] == [(jobs.NOTIFY_CHANNEL, kind)]


def _ouvintes() -> int:
    with jobs._engine().connect() as conn:
        return conn.execute(
            sa.text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND query = :q"),
            {"q": f"LISTEN {jobs.NOTIFY_CHANNEL}"},
        ).scalar_one()


def test_listen_acorda_o_worker_antes_do_polling(kind, monkeypatch):
    monkeypatch.setattr(get_settings(), "jobs_listen", True)
    claim_original = jobs.claim
    rodadas_vazias: list[float] = []

    def claim_contado(*args, **kwargs):
        job = claim_original(*args, **kwargs)
        if job is None:
            rodadas_vazias.append(time.monotonic())
        return job

    monkeypatch.setattr(jobs, "claim", claim_contado)

    async def cenario() -> float:
        rodou = asyncio.Event()

        @jobs_worker.register(kind)
        async def handler(job):
            rodou.set()

        antes = await asyncio.to_thread(_ouvintes)
        # Polling de 60 s: só a notificação explica o job rodar em poucos segundos.
        worker = Worker(concurrency=1, poll_seconds=60)
        worker.start()
        try:
            prazo = time.monotonic() + 15
            # O LISTEN está no ar e o loop já fez a primeira rodada (vazia) e dormiu —
            # senão o job seria pego por essa rodada, e o teste passaria sem notificação.
            while (await asyncio.to_thread(_ouvintes) <= antes or not rodadas_vazias) and time.monotonic() < prazo:
                await asyncio.sleep(0.05)
            assert rodadas_vazias, "o worker não fez a primeira rodada"
            assert await asyncio.to_thread(_ouvintes) > antes, "o LISTEN do worker não subiu"
            await asyncio.sleep(0.2)
            inicio = time.monotonic()
            await asyncio.to_thread(jobs.enqueue, kind)
            await asyncio.wait_for(rodou.wait(), timeout=10)
            return time.monotonic() - inicio
        finally:
            await worker.stop()

    assert asyncio.run(cenario()) < 10


# -- busca híbrida (pgvector + tsvector) --------------------------------------------


@dataclass
class EmbedderFalso(Embedder):
    """Vetores fixos, sem provedor: o documento "alfa" fica no mesmo ponto que
    qualquer pergunta, o "beta" longe dela. Nenhum vetor é nulo (cosseno de zero é NaN)."""

    dimensions: int = 3

    def _vetor(self, texto: str) -> list[float]:
        if "alfa" in texto:
            return [1.0, 0.0, 0.1]
        if "beta" in texto:
            return [0.0, 1.0, 0.1]
        return [1.0, 0.0, 0.1]

    def get_embedding(self, text: str) -> list[float]:
        return self._vetor(text)

    def get_embedding_and_usage(self, text: str):
        return self._vetor(text), None

    async def async_get_embedding(self, text: str) -> list[float]:
        return self._vetor(text)

    async def async_get_embedding_and_usage(self, text: str):
        return self._vetor(text), None


@pytest.fixture
def collection_hibrida(monkeypatch):
    nome = f"pg_{uuid4().hex[:8]}"
    monkeypatch.setattr(collections, "build_embedder", lambda *a, **k: EmbedderFalso())
    collections._build_collection.cache_clear()
    documents_store.create_collection(
        name=nome, label="PG", search={"search_mode": "hybrid", "language": "portuguese", "vector_weight": 0.5}
    )
    try:
        yield nome
    finally:
        collections._build_collection.cache_clear()
        with get_db().db_engine.begin() as conn:
            conn.execute(sa.text(f'DROP TABLE IF EXISTS ai."knowledge_{nome}"'))
            conn.execute(sa.text(f'DROP TABLE IF EXISTS "knowledge_{nome}"'))
        documents_store.delete_collection(nome)


def _busca(nome: str, peso: float) -> list[Any]:
    knowledge = collections._build_collection(
        nome, None, None, None, SearchConfig(search_mode="hybrid", language="portuguese", vector_weight=peso)
    )
    return asyncio.run(knowledge.asearch(query="garantia", max_results=5))


def test_busca_hibrida_soma_vetor_e_texto(collection_hibrida):
    nome = collection_hibrida
    # "alfa" tem o vetor da pergunta e nenhuma palavra dela; "beta" tem a palavra e o vetor longe.
    asyncio.run(collections.add_text(nome, text="alfa: o prazo de entrega é de cinco dias úteis.", name="prazo"))
    asyncio.run(collections.add_text(nome, text="beta: a garantia cobre defeitos de fabricação.", name="garantia"))

    # O híbrido do Agno engole erro de SQL e devolve [] — por isso "veio algo" é a primeira conferência.
    so_vetor = _busca(nome, 1.0)
    so_texto = _busca(nome, 0.0)
    assert len(so_vetor) == 2 and len(so_texto) == 2
    assert so_vetor[0].content.startswith("alfa")  # pgvector decide
    assert so_texto[0].content.startswith("beta")  # tsvector (português: "garantia" → "garant") decide
    assert so_texto[1].meta_data["similarity_score"] == pytest.approx(0.0)

    # O caminho do agente (a configuração gravada, peso 0,5): os dois pesam.
    resultado = asyncio.run(collections.search(nome, query="garantia"))
    assert [d.content[:4] for d in resultado] == ["alfa", "beta"]
    alfa, beta = (d.meta_data["similarity_score"] for d in resultado)
    assert alfa == pytest.approx(0.5, abs=0.01)  # vetor inteiro × 0,5, texto zero
    assert 0.05 < beta < 0.5  # quase só o texto × 0,5


# -- trace store --------------------------------------------------------------------


T0 = datetime(2026, 11, 11, 12, 0, 0, 123456, tzinfo=timezone.utc)
"""Com microssegundos: o cursor (`isoformat`) precisa voltar exato do `timestamptz`."""


def _run(sid: str, started: datetime, *, structured: bool = True, context: list | None = None) -> str:
    run_id = f"r-{uuid4().hex[:12]}"
    fields: dict[str, Any] = dict(
        run_id=run_id,
        trace_id=run_id,
        agent_type="pg-teste",
        agent_name="PG",
        prompt_version=1,
        endpoint="chat",
        user_id="u-pg",
        session_id=sid,
        message="texto montado",
        started_at=started,
        ended_at=started + timedelta(seconds=2),
        output="ok",
    )
    if structured:
        fields.update(
            input_message="oi",
            input_events=[{"role": "contact", "content": "oi", "at": started.isoformat(), "kind": "text"}],
            input_context=context if context is not None else [],
        )
    run_store._write_run(RunRecord(**fields))
    return run_id


def test_cursor_da_linha_do_tempo_com_empate_e_fim_pelo_inicio():
    sid = f"s-pg-{uuid4().hex[:8]}"
    r1 = _run(sid, T0, context=[{"key": "k", "content": "1"}])
    empate = sorted([_run(sid, T0 + timedelta(minutes=1), context=[{"key": "k", "content": n}]) for n in "ab"])
    r4 = _run(sid, T0 + timedelta(minutes=2))
    r5 = _run(sid, T0 + timedelta(minutes=3), structured=False)
    # Um run sem `ended_at` (ficou pela metade): o fim da sessão cai no início dele.
    with get_db().db_engine.begin() as conn:
        conn.execute(sa.update(run_store.runs).where(run_store.runs.c.run_id == r5).values(ended_at=None))

    vistos: list[str] = []
    paginas = []
    cursor = None
    while True:
        pagina = run_store.session_timeline(sid, limit=2, cursor=cursor)
        paginas.append(pagina)
        vistos += [r.run_id for r in pagina["rows"]]
        cursor = pagina["next_cursor"]
        if cursor is None:
            break

    assert vistos == [r1, *empate, r4, r5]  # o corte entre as duas páginas cai no meio do empate
    assert len(paginas) == 3
    total = paginas[0]["total"]
    assert total["turns"] == 5
    assert total["started_at"] == T0
    assert total["ended_at"] == T0 + timedelta(minutes=3)
    # A 2ª página começa no segundo do empate: a base de comparação é o contexto do primeiro.
    contexto_do_primeiro = run_store.session_timeline(sid, limit=5)["rows"][1].input_context
    assert paginas[1]["seed"] == contexto_do_primeiro


def test_chamada_legada_grava_sql_null_e_nao_json_null():
    sid = f"s-pg-{uuid4().hex[:8]}"
    legado = _run(sid, T0, structured=False)
    estruturado = _run(sid, T0 + timedelta(minutes=1))
    with get_db().db_engine.connect() as conn:
        linhas = {
            r.run_id: r
            for r in conn.execute(
                sa.text(
                    "SELECT run_id, input_events IS NULL AS ev, input_context IS NULL AS ctx,"
                    " input_message IS NULL AS msg, input_context::text AS ctx_texto"
                    " FROM runs WHERE run_id IN (:a, :b)"
                ),
                {"a": legado, "b": estruturado},
            )
        }
        # O filtro de "run não estruturado" é `IS NULL`: um JSON 'null' gravado escaparia dele.
        nao_estruturados = (
            conn.execute(
                sa.select(run_store.runs.c.run_id).where(
                    run_store.runs.c.session_id == sid, run_store.runs.c.input_events.is_(None)
                )
            )
            .scalars()
            .all()
        )
    assert (linhas[legado].ev, linhas[legado].ctx, linhas[legado].msg) == (True, True, True)
    assert (linhas[estruturado].ev, linhas[estruturado].ctx) == (False, False)
    assert linhas[estruturado].ctx_texto == "[]"
    assert nao_estruturados == [legado]
