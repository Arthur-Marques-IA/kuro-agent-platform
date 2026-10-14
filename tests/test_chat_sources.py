"""Citações na resposta: `sources` no /chat, no /chat/stream e no /analyze — os
documentos da base que as buscas do run devolveram, um por documento."""

import asyncio
import json
from contextlib import nullcontext
from uuid import uuid4

import pytest
from agno.knowledge.document import Document
from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, ToolCallCompletedEvent
from pydantic import BaseModel

from agent_service.api import routes
from agent_service.api.routes import AnalyzeRequest, ChatRequest, analyze, chat, chat_stream
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
    vector_db = None

    def __init__(self, documentos: list[Document]) -> None:
        self.documentos = documentos

    async def aretrieve(self, **kwargs):
        return self.documentos


def trecho(chunk: int, score: float, *, content_id: str = "doc-1", name: str = "Manual de garantia", **meta) -> Document:
    return Document(
        id=f"{content_id}-chunk-{chunk}",
        name=name,
        content=f"trecho {chunk}",
        content_id=content_id,
        meta_data={"chunk": chunk, "similarity_score": score, **meta},
    )


class AgenteQueBusca:
    """Chama o retriever de dentro do run, como a tool do Agno faria."""

    def __init__(self, retriever, queries: list[str], content="A garantia é de 12 meses.") -> None:
        self.retriever, self.queries, self.content = retriever, queries, content

    def arun(self, message, **kwargs):
        async def stream():
            for query in self.queries:
                docs = await self.retriever(query=query)
                yield ToolCallCompletedEvent(
                    tool=ToolExecution(
                        tool_name="search_knowledge_base", tool_args={"query": query}, result=json.dumps(docs)
                    )
                )
            yield RunCompletedEvent(run_id=kwargs.get("run_id"), content=self.content)

        return stream()


BASE = [
    trecho(3, 0.72),
    trecho(4, 0.91),  # mesmo documento, score melhor
    trecho(1, 0.85, content_id="pg-1", name="docs.ex.com", source_url="https://docs.ex.com/troca", page_title="Trocas"),
]


def _run(endpoint: str = "chat") -> RunContext:
    return RunContext(endpoint=endpoint, agent_type=f"agente-{uuid4().hex[:8]}", agent_name="t", prompt_version=1,
                      user_id="u", session_id="s", message="qual a garantia?")


def _agente(queries: list[str]) -> AgenteQueBusca:
    return AgenteQueBusca(retrieval.make_retriever("manuais", BaseFalsa(BASE)), queries)


def _request() -> ChatRequest:
    return ChatRequest(agent_type="suporte", user_id="u", session_id="s", message="qual a garantia?")


def test_sources_from_deduplica_por_documento_e_ordena_por_score():
    buscas = [
        {"collection": "manuais", "results": [
            {"document": "Manual", "content_id": "doc-1", "chunk_id": "c3", "score": 0.72},
            {"document": "Manual", "content_id": "doc-1", "chunk_id": "c4", "score": 0.91},
            {"document": "site", "source_url": "https://ex.com/a", "page_title": "A", "chunk_id": "c9", "score": None},
        ]},
        # A mesma busca de novo: os trechos não se repetem.
        {"collection": "manuais", "results": [
            {"document": "Manual", "content_id": "doc-1", "chunk_id": "c3", "score": 0.72},
            {"document": "Guia", "content_id": "doc-2", "chunk_id": "g1", "score": 0.8},
        ]},
    ]
    fontes = retrieval.sources_from(buscas)
    assert [f["title"] for f in fontes] == ["Manual", "Guia", "A"]  # sem score vai para o fim
    manual = fontes[0]
    assert manual == {
        "title": "Manual",
        "document": "Manual",
        "source_url": None,
        "content_id": "doc-1",
        "collection": "manuais",
        "score": 0.91,
        "chunk_ids": ["c3", "c4"],
    }
    assert fontes[2]["source_url"] == "https://ex.com/a" and fontes[2]["content_id"] is None
    assert retrieval.sources_from([]) == []


def test_chat_devolve_as_fontes_consultadas(monkeypatch):
    agent = _agente(["garantia", "garantia"])
    monkeypatch.setattr(routes, "_resolve", lambda request, endpoint: (agent, _run(endpoint)))

    resposta = asyncio.run(chat(_request()))

    assert resposta.content == "A garantia é de 12 meses."
    fontes = [f.model_dump() for f in resposta.sources]
    assert [f["content_id"] for f in fontes] == ["doc-1", "pg-1"]
    assert fontes[0]["score"] == 0.91 and fontes[0]["chunk_ids"] == ["doc-1-chunk-3", "doc-1-chunk-4"]
    assert fontes[1]["title"] == "Trocas" and fontes[1]["source_url"] == "https://docs.ex.com/troca"


def test_chat_sem_busca_devolve_sources_vazio(monkeypatch):
    agent = _agente([])
    monkeypatch.setattr(routes, "_resolve", lambda request, endpoint: (agent, _run(endpoint)))
    assert asyncio.run(chat(_request())).sources == []


def test_chat_traz_as_fontes_com_o_langfuse_ligado(monkeypatch):
    """Com o Langfuse o run vai para outra task: as fontes não podem depender do
    ContextVar da busca, que a rota não enxerga."""

    class Observacao:
        def update(self, **kwargs):
            pass

    class ClienteFalso:
        def start_as_current_observation(self, **kwargs):
            return nullcontext(Observacao())

        def update_current_trace(self, **kwargs):
            pass

    monkeypatch.setattr(tracing, "_client", ClienteFalso())
    agent = _agente(["garantia"])
    monkeypatch.setattr(routes, "_resolve", lambda request, endpoint: (agent, _run(endpoint)))
    assert [f.content_id for f in asyncio.run(chat(_request())).sources] == ["doc-1", "pg-1"]


def _eventos_do_stream(agent, monkeypatch) -> list[tuple[str, dict]]:
    monkeypatch.setattr(routes, "_resolve", lambda request, endpoint: (agent, _run(endpoint)))

    async def consumir():
        response = await chat_stream(_request())
        return [chunk async for chunk in response.body_iterator]

    eventos = []
    for chunk in asyncio.run(consumir()):
        if isinstance(chunk, dict):
            eventos.append((chunk["event"], json.loads(chunk["data"])))
    return eventos


def test_chat_stream_manda_as_fontes_antes_do_done(monkeypatch):
    eventos = _eventos_do_stream(_agente(["garantia"]), monkeypatch)
    nomes = [nome for nome, _ in eventos]
    assert nomes.index("sources") == nomes.index("done") - 1
    fontes = dict(eventos)["sources"]["sources"]
    assert [f["content_id"] for f in fontes] == ["doc-1", "pg-1"]


def test_chat_stream_sem_busca_manda_sources_vazio(monkeypatch):
    eventos = dict(_eventos_do_stream(_agente([]), monkeypatch))
    assert eventos["sources"] == {"sources": []}


class Saida(BaseModel):
    valor: int


def test_analyze_devolve_as_fontes(monkeypatch):
    agent = AgenteQueBusca(retrieval.make_retriever("manuais", BaseFalsa(BASE)), ["garantia"], content=Saida(valor=12))
    definicao = {"kind": "analysis", "name": "Extrator", "prompt_version": 1, "agent_version": 2, "config_hash": "h"}
    monkeypatch.setattr(routes, "get_agent_with_definition", lambda agent_type: (agent, definicao))

    resposta = asyncio.run(analyze(AnalyzeRequest(agent_type="extrator", document="contrato")))

    assert resposta.result == {"valor": 12}
    assert [f.content_id for f in resposta.sources] == ["doc-1", "pg-1"]
