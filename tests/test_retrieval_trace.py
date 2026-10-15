"""RAG no trace: a span da busca diz quais trechos voltaram, de qual documento e com que score."""

import asyncio
import json
from contextlib import aclosing
from uuid import uuid4

import pytest
from agno.knowledge.document import Document
from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, ToolCallCompletedEvent

from agent_service.api.observability_routes import get_run_trace
from agent_service.documents import retrieval
from agent_service.observability import trace_store, tracing
from agent_service.observability.tracing import RunContext


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    yield
    trace_store.set_trace_store(None)


class BaseFalsa:
    """Uma `Knowledge` que devolve trechos fixos e registra como foi chamada."""

    vector_db = None

    def __init__(self, documentos: list[Document]) -> None:
        self.documentos = documentos
        self.chamadas: list[dict] = []

    async def aretrieve(self, **kwargs):
        self.chamadas.append(kwargs)
        return self.documentos


def trecho(texto: str, *, chunk: int, score: float, **meta) -> Document:
    return Document(
        id=f"chunk-{chunk}",
        name="Manual de garantia",
        content=texto,
        content_id="doc-1",
        meta_data={"chunk": chunk, "chunk_size": len(texto), "similarity_score": score, **meta},
    )


def test_retriever_devolve_ao_modelo_o_formato_de_sempre_e_guarda_o_rastro():
    base = BaseFalsa([
        trecho("A garantia é de 12 meses.", chunk=3, score=0.91, source_url="https://ex.com/garantia"),
        trecho("Troca em até 7 dias.", chunk=4, score=0.72),
    ])
    retriever = retrieval.make_retriever("manuais", base)

    class Ctx:
        user_id = "u-1"

    async def run():
        retrieval.start_run()
        para_o_modelo = await retriever(query="prazo de garantia", num_documents=2, run_context=Ctx())
        return para_o_modelo, retrieval.take_search("prazo de garantia")

    para_o_modelo, busca = asyncio.run(run())

    assert base.chamadas == [{"query": "prazo de garantia", "max_results": 2, "filters": None, "user_id": "u-1"}]
    assert para_o_modelo[0] == base.documentos[0].to_dict()
    assert busca["collection"] == "manuais"
    primeiro, segundo = busca["results"]
    assert primeiro == {
        "rank": 1,
        "score": 0.91,
        "document": "Manual de garantia",
        "content_id": "doc-1",
        "chunk_id": "chunk-3",
        "chunk": 3,
        "chunk_size": len("A garantia é de 12 meses."),
        "source_url": "https://ex.com/garantia",
        "content": "A garantia é de 12 meses.",
    }
    assert (segundo["rank"], segundo["score"], "source_url" in segundo) == (2, 0.72, False)


def test_busca_sem_resultado_fica_registrada_vazia():
    retriever = retrieval.make_retriever("manuais", BaseFalsa([]))

    async def run():
        retrieval.start_run()
        return await retriever(query="nada"), retrieval.take_search("nada")

    para_o_modelo, busca = asyncio.run(run())
    assert para_o_modelo is None
    assert busca["results"] == []


class AgenteQueBusca:
    """Chama o retriever de dentro do run, como a tool do Agno faria, e emite o evento."""

    def __init__(self, retriever, queries: list[str]) -> None:
        self.retriever, self.queries = retriever, queries

    def arun(self, message, **kwargs):
        async def stream():
            for query in self.queries:
                docs = await self.retriever(query=query)
                yield ToolCallCompletedEvent(
                    tool=ToolExecution(
                        tool_name="search_knowledge_base", tool_args={"query": query}, result=json.dumps(docs)
                    )
                )
            yield RunCompletedEvent(run_id=kwargs.get("run_id"), content="A garantia é de 12 meses.")

        return stream()


def test_span_da_busca_traz_os_trechos_consultados():
    base = BaseFalsa([trecho("A garantia é de 12 meses.", chunk=3, score=0.91)])
    agent = AgenteQueBusca(retrieval.make_retriever("manuais", base), ["garantia", "garantia"])
    run = RunContext(endpoint="chat", agent_type=f"agente-{uuid4().hex[:8]}", agent_name="t", prompt_version=1,
                     user_id="u", session_id="s", message="qual a garantia?")

    async def consumir():
        async with aclosing(tracing.traced_run_events(agent, run)) as eventos:
            async for _ in eventos:
                pass

    asyncio.run(consumir())
    trace = get_run_trace(run.run_id)

    buscas = [s for s in trace.spans if s.name == "search_knowledge_base"]
    assert len(buscas) == 2  # a mesma query duas vezes: cada span com a sua busca
    for span in buscas:
        assert span.level == "DEFAULT"
        refs = span.metadata["references"]
        assert refs["query"] == "garantia" and refs["collection"] == "manuais"
        assert refs["results"][0]["content"] == "A garantia é de 12 meses."
        assert refs["results"][0]["score"] == 0.91
    # Busca não é falha nem conta para a complexidade.
    assert (trace.run.tool_failures, trace.run.complexity) == (0, 1)
