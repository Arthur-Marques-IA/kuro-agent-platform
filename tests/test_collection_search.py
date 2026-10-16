"""Configuração de busca por collection (modo, idioma, k), versionada, e a versão no run."""

import uuid
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from agno.knowledge.document import Document
from agno.vectordb.search import SearchType
from alembic import command
from fastapi import HTTPException

from agent_service.api.collections_routes import (
    CollectionIn,
    CollectionUpdateIn,
    create_collection,
    delete_collection,
    get_collection_version,
    list_collection_versions,
    list_collections,
    update_collection,
)
from agent_service.documents import retrieval
from agent_service.documents.collections import get_collection, resolve_search
from agent_service.documents.search import SearchConfig, SearchConfigError, snapshot, validate_language
from agent_service.documents.versions import config_hash
from agent_service.migrations import _config


@pytest.fixture
def base():
    # Nome novo a cada teste: o histórico de versões fica depois de excluir a collection.
    nome = f"busca-{uuid.uuid4().hex[:8]}"
    created = create_collection(CollectionIn(name=nome, label="Busca"))
    yield created
    if any(c["name"] == nome for c in list_collections()):
        delete_collection(nome)


def test_collection_nova_nasce_vetorial_em_portugues_na_v1(base):
    assert base["search"] == {
        "search_mode": "vector",
        "language": "portuguese",
        "max_results": 10,
        "vector_weight": 0.5,
        "reranker": None,
        "score_kind": "similarity",
    }
    assert base["version"] == 1


def test_a_config_chega_ao_pgvector_e_ao_k_da_knowledge(base):
    """O PgVector de verdade abre conexão com o Postgres; aqui só importa o que ele recebe."""
    from agent_service.documents import collections as mod

    class PgVectorFalso:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    update_collection(base["name"], CollectionUpdateIn(search_mode="hybrid", vector_weight=0.7, max_results=4))
    mod._build_collection.cache_clear()
    with patch.object(mod, "PgVector", PgVectorFalso), patch.object(mod, "build_embedder", lambda *a: None), \
            patch.object(mod, "Knowledge", lambda **kw: kw):
        knowledge = get_collection(base["name"])
    mod._build_collection.cache_clear()
    kwargs = knowledge["vector_db"].kwargs
    assert kwargs["search_type"] == SearchType.hybrid
    assert kwargs["vector_score_weight"] == 0.7
    assert kwargs["content_language"] == "portuguese"
    assert kwargs["reranker"] is None
    assert knowledge["max_results"] == 4


def test_mudar_a_busca_gera_versao_e_voltar_reaproveita(base):
    v2 = update_collection(base["name"], CollectionUpdateIn(search_mode="hybrid"))
    assert (v2["version"], v2["search"]["score_kind"]) == (2, "hybrid")
    # Rótulo não é busca: a versão fica.
    assert update_collection(base["name"], CollectionUpdateIn(label="Outra"))["version"] == 2
    # `null` volta ao padrão, que é a configuração da v1.
    assert update_collection(base["name"], CollectionUpdateIn(search_mode=None))["version"] == 1
    historico = list_collection_versions(base["name"])
    assert [v["version"] for v in historico] == [2, 1]
    assert get_collection_version(base["name"], 2)["config"]["vector_weight"] == 0.5


def test_idioma_nao_muda_depois_de_criada(base):
    with pytest.raises(HTTPException) as exc:
        update_collection(base["name"], CollectionUpdateIn(language="english"))
    assert exc.value.status_code == 422
    # Mandar o mesmo valor (ex.: um apply do que o GET devolveu) passa.
    assert update_collection(base["name"], CollectionUpdateIn(language="portuguese"))["version"] == 1


def test_reranker_e_so_gancho(base):
    with pytest.raises(HTTPException) as exc:
        update_collection(base["name"], CollectionUpdateIn(reranker="cohere"))
    assert exc.value.status_code == 422 and "reranker" in exc.value.detail


def test_idioma_com_formato_invalido_e_recusado():
    with pytest.raises(HTTPException) as exc:
        create_collection(CollectionIn(name="x", label="X", language="portuguese'); drop table x; --"))
    assert exc.value.status_code == 422


def test_idioma_e_conferido_no_pg_ts_config():
    engine = sa.create_engine("sqlite://")
    validate_language("portuguese", engine)  # fora do Postgres, só o formato

    class Pg:
        class dialect:
            name = "postgresql"

        def connect(self):
            class Conn:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

                def execute(self, *_args, **_kw):
                    class R:
                        def first(self):
                            return None

                    return R()

            return Conn()

    with pytest.raises(SearchConfigError, match="pg_ts_config"):
        validate_language("klingon", Pg())


def test_collection_antiga_tem_o_mesmo_hash_da_v1_da_migracao():
    """Linha sem as colunas novas = o snapshot que a migração 0012 gravou."""
    legado = {"name": "x", "embedder_provider": None, "embedder_model": None, "embedder_dimensions": None}
    assert snapshot(legado) == {
        "embedder_provider": "google",
        "embedder_model": None,
        "embedder_dimensions": None,
        "search_mode": "vector",
        "language": None,
        "max_results": 10,
    }
    assert SearchConfig.from_row(legado).content_language == "english"


# -- retriever e run --------------------------------------------------------------


class _Base:
    def __init__(self, docs):
        self.docs, self.max_results, self.vector_db = docs, 10, None
        self.pedidos = []

    async def aretrieve(self, *, query, max_results, filters, user_id):
        self.pedidos.append(max_results)
        return self.docs


def test_retriever_usa_a_config_atual_e_registra_a_versao():
    doc = Document(id="c1", name="Manual", content="prazo", meta_data={"similarity_score": 0.8})
    atual = _Base([doc])
    config = SearchConfig(search_mode="hybrid", max_results=3)
    retriever = retrieval.make_retriever("busca", _Base([]), lambda nome: (atual, config, (4, "abc123")))

    retrieval.start_run()
    retriever(query="prazo", num_documents=10)  # sem event loop: roda até o fim

    assert atual.pedidos == [3]  # o k da collection, não o do Agent em cache
    busca = retrieval.take_search("prazo")
    assert {k: busca[k] for k in ("version", "config_hash", "search_mode", "score_kind")} == {
        "version": 4,
        "config_hash": "abc123",
        "search_mode": "hybrid",
        "score_kind": "hybrid",
    }


def test_resolve_search_devolve_a_versao_atual(base):
    from agent_service.documents import collections as mod

    update_collection(base["name"], CollectionUpdateIn(max_results=3))
    with patch.object(mod, "_from_row", lambda name, row: None):
        _, config, (version, digest) = resolve_search(base["name"])
    assert (config.max_results, version) == (3, 2)
    from agent_service.documents.store import get_collection_row

    assert digest == config_hash(snapshot(get_collection_row(base["name"])))


def _record(run_id, **extra):
    from datetime import datetime, timezone

    from agent_service.observability.run_store import RunRecord

    campos = dict(
        run_id=run_id, trace_id=run_id, agent_type="a", agent_name="A", prompt_version=1, endpoint="chat",
        user_id="u", session_id="s", message="oi", started_at=datetime.now(timezone.utc),
    )
    return RunRecord(**{**campos, **extra})


def test_run_grava_a_versao_da_collection(base):
    from agent_service.observability import tracing

    run = tracing.RunContext(
        endpoint="chat", agent_type="a", agent_name="A", prompt_version=1, user_id="u", session_id="s",
        message="oi", definition={"knowledge_collection": base["name"]},
    )
    record = _record("r")
    # Sem busca no run: a versão atual da collection.
    tracing._set_collection_version(record, run)
    assert (record.knowledge_collection, record.collection_version) == (base["name"], 1)
    # Com busca: a versão que de fato respondeu.
    run.knowledge_searches.append({"query": "x", "version": 7, "config_hash": "h7", "results": []})
    tracing._set_collection_version(record, run)
    assert (record.collection_version, record.collection_config_hash) == (7, "h7")


def test_runs_filtram_pela_versao_da_collection():
    from datetime import datetime, timezone

    from agent_service.observability import run_store
    from agent_service.observability.trace_store import RunQuery

    agora = datetime.now(timezone.utc)
    for run_id, versao in (("rv1", 1), ("rv2", 2)):
        run_store._write_run(
            _record(
                run_id, agent_type="filtro-col", started_at=agora,
                knowledge_collection="busca", collection_version=versao, collection_config_hash=f"h{versao}",
            )
        )
    query = RunQuery(agent_type="filtro-col", knowledge_collection="busca", collection_version=2)
    page = run_store.DbTraceStore().list_runs(query)
    assert [(r.run_id, r.collection_version, r.collection_config_hash) for r in page.items] == [("rv2", 2, "h2")]


# -- migração -----------------------------------------------------------------------


def test_migracao_cria_a_v1_das_collections_existentes(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'antes.db'}")
    config = _config()
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0010")
        conn.execute(
            sa.text(
                "INSERT INTO document_collections (name, label, is_seed, embedder_provider, created_at, updated_at)"
                " VALUES ('manuais', 'Manuais', 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        command.upgrade(config, "head")

    colunas = {c["name"] for c in sa.inspect(engine).get_columns("runs")}
    assert {"knowledge_collection", "collection_version", "collection_config_hash"} <= colunas
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT search_mode, language FROM document_collections")).one()
        assert tuple(row) == (None, None)  # o comportamento de antes
        v1 = conn.execute(sa.text("SELECT collection, version, config_hash FROM collection_versions")).one()
    legado = {"name": "manuais", "embedder_provider": None, "embedder_model": None, "embedder_dimensions": None}
    assert tuple(v1) == ("manuais", 1, config_hash(snapshot(legado)))

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0010")
    assert "collection_versions" not in sa.inspect(engine).get_table_names()
