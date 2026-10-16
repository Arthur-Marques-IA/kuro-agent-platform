"""Feedback do operador por trecho recuperado (`/observability/chunk-feedback`).

Rotas chamadas direto, sem TestClient, como em test_analysis_and_feedback_routes.py."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from agent_service.api.observability_routes import (
    ChunkFeedbackIn,
    create_chunk_feedback,
    delete_chunk_feedback,
    export_chunk_feedback,
    flagged_documents,
    list_chunk_feedback,
)
from agent_service.observability import run_store


def _trecho(chunk_id: str, content_id: str, document: str, rank: int = 1) -> dict:
    return {"rank": rank, "score": 0.9, "document": document, "content_id": content_id, "chunk_id": chunk_id, "content": "..."}


def _run_com_busca(collection: str, query: str, trechos: list[dict], agent_type: str = "suporte") -> str:
    run_id = uuid4().hex
    agora = datetime.now(timezone.utc)
    run_store._write_run(
        run_store.RunRecord(
            run_id=run_id,
            trace_id=uuid4().hex,
            agent_type=agent_type,
            agent_name="t",
            prompt_version=1,
            endpoint="chat",
            user_id="u",
            session_id="s",
            message=query,
            started_at=agora,
            spans=[
                run_store.SpanRecord(
                    type="TOOL",
                    name="search_knowledge_base",
                    started_at=agora,
                    metadata={"references": {"query": query, "collection": collection, "results": trechos}},
                )
            ],
        )
    )
    return run_id


def test_marca_copia_pergunta_e_documento_da_span_e_substitui_a_anterior():
    col = f"col-{uuid4().hex[:6]}"
    run_id = _run_com_busca(col, "prazo de garantia", [_trecho("c1", "doc-a", "Manual")])

    primeira = create_chunk_feedback(ChunkFeedbackIn(run_id=run_id, chunk_id="c1", label="irrelevant", created_by="ana"))
    assert (primeira.query, primeira.collection, primeira.content_id, primeira.document) == (
        "prazo de garantia", col, "doc-a", "Manual"
    )
    assert primeira.span_id == f"{run_id}:0" and primeira.agent_type == "suporte"

    segunda = create_chunk_feedback(ChunkFeedbackIn(run_id=run_id, chunk_id="c1", label="relevant", comment="era esse"))
    marcas = list_chunk_feedback(run_id=run_id)
    assert [m.id for m in marcas] == [segunda.id]
    assert marcas[0].label == "relevant" and marcas[0].comment == "era esse"

    delete_chunk_feedback(segunda.id)
    assert list_chunk_feedback(run_id=run_id) == []
    with pytest.raises(HTTPException) as exc:
        delete_chunk_feedback(segunda.id)
    assert exc.value.status_code == 404


def test_trecho_ou_run_inexistente_da_404():
    run_id = _run_com_busca("x", "q", [_trecho("c1", "doc", "D")])
    for body in (
        ChunkFeedbackIn(run_id=run_id, chunk_id="nao-veio", label="wrong"),
        ChunkFeedbackIn(run_id="run-que-nao-existe", chunk_id="c1", label="wrong"),
    ):
        with pytest.raises(HTTPException) as exc:
            create_chunk_feedback(body)
        assert exc.value.status_code == 404


def test_rotulo_invalido_e_recusado():
    with pytest.raises(ValueError):
        ChunkFeedbackIn(run_id="r", chunk_id="c", label="talvez")


def test_documentos_mais_marcados_como_errados_apontam_os_runs():
    col = f"col-{uuid4().hex[:6]}"
    r1 = _run_com_busca(col, "q1", [_trecho("a1", "doc-velho", "Tabela 2023"), _trecho("b1", "doc-ok", "Manual", 2)])
    r2 = _run_com_busca(col, "q2", [_trecho("a2", "doc-velho", "Tabela 2023")])
    create_chunk_feedback(ChunkFeedbackIn(run_id=r1, chunk_id="a1", label="wrong", comment="preço antigo"))
    create_chunk_feedback(ChunkFeedbackIn(run_id=r2, chunk_id="a2", label="wrong"))
    create_chunk_feedback(ChunkFeedbackIn(run_id=r1, chunk_id="b1", label="wrong"))

    docs = flagged_documents(collection=col, label="wrong", limit=20)
    assert [(d.content_id, d.count) for d in docs] == [("doc-velho", 2), ("doc-ok", 1)]
    assert set(docs[0].run_ids) == {r1, r2}
    assert docs[0].comments == ["preço antigo"]


def test_export_agrupa_os_relevantes_por_pergunta():
    col = f"col-{uuid4().hex[:6]}"
    r1 = _run_com_busca(col, "garantia", [_trecho("g1", "doc-g", "Garantia"), _trecho("x1", "doc-x", "Outro", 2)])
    r2 = _run_com_busca(col, "garantia", [_trecho("g2", "doc-g", "Garantia")])
    create_chunk_feedback(ChunkFeedbackIn(run_id=r1, chunk_id="g1", label="relevant"))
    create_chunk_feedback(ChunkFeedbackIn(run_id=r1, chunk_id="x1", label="irrelevant"))
    create_chunk_feedback(ChunkFeedbackIn(run_id=r2, chunk_id="g2", label="relevant"))

    casos = export_chunk_feedback(collection=col, agent_type=None, since=None, until=None)
    assert len(casos) == 1
    caso = casos[0]
    assert caso.query == "garantia" and caso.collection == col
    assert caso.relevant_chunk_ids == ["g1", "g2"]
    assert caso.relevant_content_ids == ["doc-g"]
    assert caso.run_ids == [r1, r2]
