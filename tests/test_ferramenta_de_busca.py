"""A tool `search_knowledge_base` em português, com o Agno de verdade.

Um agente real (`build_agent`) com uma collection falsa e um modelo falso que guarda o que
recebeu — as mensagens e o schema das tools — e chama a busca uma vez. Prova que:

- o schema enviado ao modelo traz o mesmo nome (`search_knowledge_base`) e o mesmo parâmetro
  (`query`), com descrição em português, e nada em inglês do Agno vai ao prompt;
- o resultado chega ao modelo como JSON dos trechos, ou "Nenhum documento encontrado.";
- o rastro (`metadata.references`) continua gravado, pelo retriever do Kuro.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from uuid import uuid4

import pytest
from agno.db.sqlite import SqliteDb
from agno.knowledge.document import Document
from agno.metrics import MessageMetrics
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse

from agent_service.agents import base, session_context
from agent_service.agents.base import build_agent
from agent_service.api import routes
from agent_service.api.routes import ChatRequest, chat
from agent_service.documents import retrieval
from agent_service.documents.search import SearchConfig
from agent_service.observability import trace_store, tracing


class ModeloQueBusca(Model):
    """Pede a busca no primeiro passo e responde com o que ela devolveu."""

    name: str = "ModeloQueBusca"
    provider: str = "teste"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.chamadas: list[list[Message]] = []
        self.tools_enviadas: list[Any] = []

    def _resposta(self, messages: list[Message], tools: Any) -> ModelResponse:
        self.chamadas.append([m.model_copy(deep=True) for m in messages])
        self.tools_enviadas.append(tools)
        if messages[-1].role == "user":
            response = ModelResponse(
                role="assistant",
                tool_calls=[
                    {
                        "id": f"call-{uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": "search_knowledge_base",
                            "arguments": json.dumps({"query": "prazo de garantia"}),
                        },
                    }
                ],
            )
        else:
            response = ModelResponse(role="assistant", content="ok")
        response.response_usage = MessageMetrics(input_tokens=1, output_tokens=1, total_tokens=2)
        return response

    def invoke(self, messages: list[Message], assistant_message: Message, **kwargs: Any) -> ModelResponse:
        return self._resposta(messages, kwargs.get("tools"))

    async def ainvoke(self, messages: list[Message], assistant_message: Message, **kwargs: Any) -> ModelResponse:
        return self._resposta(messages, kwargs.get("tools"))

    def invoke_stream(
        self, messages: list[Message], assistant_message: Message, **kwargs: Any
    ) -> Iterator[ModelResponse]:
        yield self._resposta(messages, kwargs.get("tools"))

    async def ainvoke_stream(
        self, messages: list[Message], assistant_message: Message, **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        yield self._resposta(messages, kwargs.get("tools"))

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


class BaseFalsa:
    vector_db = None

    def __init__(self, documentos: list[Document]) -> None:
        self.documentos = documentos

    async def aretrieve(self, **kwargs: Any) -> list[Document]:
        return self.documentos


TRECHO = Document(
    id="chunk-1",
    name="Manual de garantia",
    content="A garantia é de 12 meses.",
    content_id="doc-1",
    meta_data={"chunk": 1, "similarity_score": 0.9},
)


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    session_context.forget()
    yield
    trace_store.set_trace_store(None)
    session_context.forget()


def _agente(monkeypatch, tmp_path: Path, documentos: list[Document]):
    banco = SqliteDb(db_url=f"sqlite:///{(tmp_path / 'agno.db').as_posix()}")
    modelo = ModeloQueBusca(id="modelo-que-busca")
    colecao = BaseFalsa(documentos)
    monkeypatch.setattr(base, "get_db", lambda: banco)
    monkeypatch.setattr(base, "get_model", lambda **_: modelo)
    monkeypatch.setattr(base, "get_collection", lambda nome: colecao)
    monkeypatch.setattr(base, "resolve_search", lambda nome: (colecao, SearchConfig(), (1, "hash")))
    agent = build_agent(
        agent_id="rag", name="RAG", instructions=["Você atende alunos."], knowledge_collection="manuais"
    )
    definicao = {
        "kind": "conversational",
        "name": "RAG",
        "prompt_version": 1,
        "agent_version": 1,
        "config_hash": "h",
        "dependency_fields": [],
    }
    monkeypatch.setattr(routes, "get_agent_with_definition", lambda agent_type: (agent, definicao))
    return agent, modelo


def _turno() -> Any:
    pedido = ChatRequest.model_validate(
        {"agent_type": "rag", "user_id": "u", "session_id": f"s-{uuid4().hex[:8]}", "message": "qual a garantia?"}
    )
    return asyncio.run(chat(pedido))


def test_o_schema_enviado_ao_modelo_e_em_portugues_com_o_mesmo_nome(monkeypatch, tmp_path):
    _, modelo = _agente(monkeypatch, tmp_path, [TRECHO])
    _turno()

    tools = modelo.tools_enviadas[0]
    busca = next(t for t in tools if t["function"]["name"] == "search_knowledge_base")["function"]
    assert busca["description"] == retrieval.SEARCH_TOOL_DESCRIPTION
    assert set(busca["parameters"]["properties"]) == {"query"}
    assert busca["parameters"]["required"] == ["query"]
    assert busca["parameters"]["properties"]["query"]["type"] == "string"
    assert "Use this function" not in json.dumps(tools)
    assert "run_context" not in json.dumps(tools)

    sistema = next(m for m in modelo.chamadas[0] if m.role == "system")
    texto = str(sistema.content)
    assert "Você tem uma base de conhecimento" in texto
    assert "You have a knowledge base" not in texto and "Search before answering" not in texto


def test_o_modelo_recebe_os_trechos_e_o_rastro_fica_gravado(monkeypatch, tmp_path):
    from agent_service.api.observability_routes import get_run_trace

    _, modelo = _agente(monkeypatch, tmp_path, [TRECHO])
    resposta = _turno()

    resultado = next(m for m in modelo.chamadas[1] if m.role == "tool")
    assert json.loads(str(resultado.content)) == [TRECHO.to_dict()]
    trace = get_run_trace(resposta.run_id)
    buscas = [s for s in trace.spans if s.name == "search_knowledge_base"]
    assert len(buscas) == 1
    referencias = buscas[0].metadata["references"]
    assert referencias["query"] == "prazo de garantia" and referencias["collection"] == "manuais"
    assert referencias["results"][0]["chunk_id"] == "chunk-1"
    assert resposta.sources and resposta.sources[0].content_id == "doc-1"


def test_sem_resultado_a_mensagem_e_em_portugues(monkeypatch, tmp_path):
    _, modelo = _agente(monkeypatch, tmp_path, [])
    _turno()
    resultado = next(m for m in modelo.chamadas[1] if m.role == "tool")
    assert str(resultado.content) == retrieval.NO_DOCUMENTS


def test_agente_sem_collection_nao_tem_a_tool(monkeypatch, tmp_path):
    banco = SqliteDb(db_url=f"sqlite:///{(tmp_path / 'agno.db').as_posix()}")
    monkeypatch.setattr(base, "get_db", lambda: banco)
    monkeypatch.setattr(base, "get_model", lambda **_: ModeloQueBusca(id="m"))
    agent = build_agent(agent_id="x", name="x", instructions=["x"])
    assert agent.tools == [] and agent.knowledge is None
