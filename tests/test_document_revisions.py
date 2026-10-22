"""O ciclo de correção do RAG: marcar um trecho errado → corrigir o documento
(mesmo `content_id`, revisão nova) → a marcação fica resolvida nessa revisão.

A `Knowledge` do Agno é trocada por uma em memória: o que importa aqui é o id, a
metadata e as revisões, não os vetores. Rotas chamadas direto, sem TestClient."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from agent_service.api.collections_routes import (
    ReplaceTextRequest,
    get_document_revision,
    list_document_revisions,
    replace_collection_document,
    retest_document,
)
from agent_service.api.observability_routes import (
    ChunkFeedbackIn,
    ChunkFeedbackStatusIn,
    create_chunk_feedback,
    flagged_documents,
    list_chunk_feedback,
    set_chunk_feedback_status,
)
from agent_service.db import get_db
from agent_service.documents import collections, revisions
from tests.test_chunk_feedback import _run_com_busca, _trecho


class FakeKnowledge:
    def __init__(self) -> None:
        self.contents: dict[str, SimpleNamespace] = {}
        self.vector_db = None
        self.max_results = 5

    def _build_content_hash(self, content) -> str:
        raw = json.dumps([content.name, content.metadata, content.file_data.content.decode("utf-8", "replace")], default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    async def aget_content_by_id(self, content_id):
        return self.contents.get(content_id)

    async def aget_content(self, limit=50, page=1):
        items = list(self.contents.values())
        return items, len(items)

    async def aremove_content_by_id(self, content_id):
        return self.contents.pop(content_id, None) is not None

    async def _aload_content(self, content, upsert=False, skip_if_exists=False):
        if skip_if_exists and content.id in self.contents:
            return
        self.contents[content.id] = SimpleNamespace(
            id=content.id,
            name=content.name,
            description=content.description,
            metadata=dict(content.metadata or {}),
            text=content.file_data.content.decode("utf-8", "replace"),
            content_hash=content.content_hash,
            chunks=[(d.content, dict(d.meta_data)) for d in getattr(content.reader, "documents", [])],
        )


@pytest.fixture
def base():
    """Uma collection (só o nome: o cadastro é dispensado) com a Knowledge falsa."""
    knowledge = FakeKnowledge()
    name = f"col-{uuid4().hex[:6]}"
    with (
        patch.object(collections, "get_collection", return_value=knowledge),
        patch("agent_service.api.collections_routes.get_collection", return_value=knowledge),
        patch("agent_service.api.collections_routes._row_or_404", return_value={"name": name}),
        patch("agent_service.documents.sources_store.source_of_content", return_value=None),
    ):
        yield SimpleNamespace(name=name, knowledge=knowledge)


def _add(base, text: str, name: str = "Tutorial") -> str:
    return asyncio.run(collections.add_text(base.name, text=text, name=name))


def _mark(base, content_id: str, label: str = "wrong", revision: int | None = 1, query: str = "como pedir presença"):
    hit = _trecho(uuid4().hex, content_id, "Tutorial")
    if revision is not None:
        hit[revisions.FIELD] = revision
    run_id = _run_com_busca(base.name, query, [hit])
    return create_chunk_feedback(ChunkFeedbackIn(run_id=run_id, chunk_id=hit["chunk_id"], label=label))


def _edit(base, content_id: str, text: str, **extra):
    return asyncio.run(replace_collection_document(base.name, content_id, ReplaceTextRequest(text=text, **extra)))


def test_documento_novo_nasce_na_revisao_1_e_o_mesmo_texto_nao_vira_revisao(base):
    content_id = _add(base, "Use o link de presença.")
    assert base.knowledge.contents[content_id].metadata[revisions.FIELD] == 1
    assert _add(base, "Use o link de presença.") == content_id

    [rev] = list_document_revisions(base.name, content_id)
    assert rev.revision == 1 and rev.created_at is not None
    revisao = asyncio.run(get_document_revision(base.name, content_id, 1))
    assert (revisao.text, revisao.text_source) == ("Use o link de presença.", "stored")


def test_editar_mantem_o_id_cria_revisao_e_resolve_as_marcacoes_abertas(base):
    content_id = _add(base, "Use o link de presença.")
    errada = _mark(base, content_id)
    assert (errada.status, errada.document_revision, errada.current_revision) == ("open", 1, 1)

    out = _edit(base, content_id, "Peça a presença pela Central do Aluno.", note="link saiu", created_by="ana")
    assert (out.content_id, out.revision, out.changed, out.resolved) == (content_id, 2, True, 1)
    doc = base.knowledge.contents[content_id]
    assert doc.text == "Peça a presença pela Central do Aluno." and doc.metadata[revisions.FIELD] == 2
    assert doc.name == "Tutorial"  # o nome continua o de antes

    [marca] = list_chunk_feedback(content_id=content_id)
    assert (marca.status, marca.resolved_in_revision, marca.resolved_by, marca.resolution_note) == (
        "resolved", 2, "ana", "link saiu"
    )
    assert [r.revision for r in list_document_revisions(base.name, content_id)] == [2, 1]
    assert flagged_documents(collection=base.name) == []  # a lista mostra só as abertas
    assert len(flagged_documents(collection=base.name, status="resolved")) == 1


def test_mesmo_texto_nao_reindexa_e_resolves_vazio_nao_resolve_nada(base):
    content_id = _add(base, "Texto A")
    _mark(base, content_id)
    out = _edit(base, content_id, "Texto A")
    assert (out.revision, out.changed) == (1, False)

    out = _edit(base, content_id, "Texto B", resolves=[])
    assert (out.revision, out.changed, out.resolved) == (2, True, 0)
    [marca] = list_chunk_feedback(content_id=content_id)
    assert marca.status == "open" and marca.stale  # vista na revisão 1, o documento está na 2


def test_marcacao_nova_depois_da_correcao_aparece_aberta(base):
    content_id = _add(base, "Texto A")
    _mark(base, content_id)
    _edit(base, content_id, "Texto B")
    nova = _mark(base, content_id, revision=2)
    assert (nova.status, nova.stale) == ("open", False)
    [grupo] = flagged_documents(collection=base.name)
    assert grupo.count == 1 and grupo.current_revision == 2 and not grupo.stale


def test_dispensar_e_reabrir(base):
    content_id = _add(base, "Texto A")
    marca = _mark(base, content_id)
    dispensada = set_chunk_feedback_status(marca.id, ChunkFeedbackStatusIn(status="dismissed", note="era teste"))
    assert (dispensada.status, dispensada.resolution_note) == ("dismissed", "era teste")
    assert flagged_documents(collection=base.name) == []

    reaberta = set_chunk_feedback_status(marca.id, ChunkFeedbackStatusIn(status="open"))
    assert (reaberta.status, reaberta.resolution_note, reaberta.resolved_at) == ("open", None, None)
    with pytest.raises(HTTPException) as exc:
        set_chunk_feedback_status("nao-existe", ChunkFeedbackStatusIn(status="dismissed"))
    assert exc.value.status_code == 404


def test_pagina_de_site_e_documento_de_outra_collection_sao_recusados(base):
    content_id = _add(base, "Página")
    with patch("agent_service.documents.sources_store.source_of_content", return_value={"source_id": "s"}):
        with pytest.raises(HTTPException) as exc:
            _edit(base, content_id, "editado")
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        _edit(base, "nao-existe", "editado")
    assert exc.value.status_code == 404


def test_texto_igual_ao_de_outro_documento_da_409(base):
    primeiro = _add(base, "Texto A")
    segundo = _add(base, "Texto B")
    with pytest.raises(HTTPException) as exc:
        _edit(base, segundo, "Texto A", name="Tutorial")
    assert exc.value.status_code == 409 and primeiro in exc.value.detail


def test_remover_o_documento_fecha_as_marcacoes(base):
    content_id = _add(base, "Texto A")
    _mark(base, content_id)
    assert asyncio.run(collections.remove_document(base.name, content_id))
    [marca] = list_chunk_feedback(content_id=content_id)
    assert (marca.status, marca.resolution_note) == ("resolved", "documento removido")


def test_trecho_sem_revisao_na_metadata_usa_a_que_estava_no_ar(base):
    content_id = _add(base, "Texto A")
    marca = _mark(base, content_id, revision=None)
    assert marca.document_revision == 1


def test_retest_diz_se_o_documento_volta_na_busca(base):
    content_id = _add(base, "Texto A")
    marca = _mark(base, content_id, query="presença gravada")
    _edit(base, content_id, "Texto B")
    hit = {"rank": 2, "score": 0.8, "content_id": content_id, "chunk_id": "c9", revisions.FIELD: 2}
    with (
        patch("agent_service.api.collections_routes.search", return_value=[object()]),
        patch("agent_service.api.collections_routes.describe_hits", return_value=[hit]),
    ):
        [caso] = asyncio.run(retest_document(base.name, content_id))
    assert (caso.query, caso.feedback_ids, caso.status, caso.found) == ("presença gravada", [marca.id], "resolved", True)
    assert (caso.hits[0].rank, caso.hits[0].document_revision) == (2, 2)


def test_filtro_rag_dos_logs():
    from agent_service.api.observability_routes import list_runs

    col, agente = f"col-{uuid4().hex[:6]}", f"rag-{uuid4().hex[:6]}"
    com_trecho = _run_com_busca(col, "presença", [_trecho("c1", "doc-x", "Tutorial")], agent_type=agente)
    vazia = _run_com_busca(col, "nota mínima", [], agent_type=agente)
    create_chunk_feedback(ChunkFeedbackIn(run_id=com_trecho, chunk_id="c1", label="wrong"))

    def ids(rag):
        return {r.run_id for r in list_runs(agent_type=[agente], rag=rag).items}

    assert ids("searched") == {com_trecho, vazia}
    assert ids("empty") == {vazia}
    assert ids("flagged") == {com_trecho}


def test_texto_e_arquivo_sao_cortados_pela_regra_dos_sites(base):
    content_id = _add(base, "# FAQ\n\nComo peço presença?\n\nPela Central do Aluno.")
    [(texto, meta)] = base.knowledge.contents[content_id].chunks
    assert texto.startswith("# FAQ") and texto.endswith("Pela Central do Aluno.")
    assert (meta["chunk"], meta["section"]) == (1, "FAQ")

    corpo = "Regras gerais. " * 150
    arquivo = asyncio.run(collections.add_file(base.name, content_bytes=corpo.encode(), filename="regulamento.md"))
    trechos = base.knowledge.contents[arquivo].chunks
    assert len(trechos) > 1 and all(t.endswith(".") for t, _ in trechos)
    # O texto extraído vai para a revisão: o "Corrigir" abre com ele.
    assert asyncio.run(get_document_revision(base.name, arquivo, 1)).text == corpo


def test_csv_continua_com_o_leitor_do_agno(base):
    content_id = asyncio.run(collections.add_file(base.name, content_bytes=b"a,b\n1,2", filename="tabela.csv"))
    assert base.knowledge.contents[content_id].chunks == []
    assert revisions.get_revision(content_id, 1)["text"] is None


def test_documento_sem_texto_guardado_abre_com_o_texto_dos_trechos(base):
    content_id = _add(base, "qualquer")
    with get_db().db_engine.begin() as conn:  # simula um documento de antes do histórico
        conn.execute(revisions.document_revisions.update().where(
            revisions.document_revisions.c.content_id == content_id).values(text=None))
    trechos = [("Página um.", {"page": 1, "chunk": 1}), ("Página dois.", {"page": 2, "chunk": 2})]
    with patch.object(collections, "_indexed_chunks", return_value=trechos):
        revisao = asyncio.run(get_document_revision(base.name, content_id, 1))
    assert (revisao.text, revisao.text_source) == ("Página um.\n\nPágina dois.", "chunks")


def test_reindex_recorta_mantendo_id_paginas_e_cria_revisao(base):
    from agent_service.api.collections_routes import reindex

    content_id = _add(base, "texto antigo")
    hash_original = base.knowledge.contents[content_id].content_hash
    antigos = [("Como peço presença?", {"page": 3, "chunk": 1}), ("Pela Central.", {"page": 3, "chunk": 2})]
    with patch.object(collections, "_indexed_chunks", return_value=antigos):
        [item] = asyncio.run(reindex(base.name))
    assert (item.content_id, item.status, item.revision, item.chunks_before, item.chunks_after) == (
        content_id, "rechunked", 2, 2, 1
    )
    doc = base.knowledge.contents[content_id]
    [(texto, meta)] = doc.chunks
    assert texto == "Como peço presença?\n\nPela Central." and meta["page"] == 3
    assert doc.metadata[revisions.FIELD] == 2 and doc.content_hash == hash_original
    assert revisions.get_revision(content_id, 2)["note"] == "recortado com a regra de trechos atual"

    with patch("agent_service.documents.sources_store.source_of_content", return_value={"source_id": "s"}):
        [pulado] = asyncio.run(reindex(base.name, content_id=content_id))
    assert pulado.status == "skipped"
