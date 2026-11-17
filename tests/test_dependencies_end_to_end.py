"""As `dependencies` da requisição chegam à tool, pelos dois caminhos.

Os testes de `test_tool_param_sources.py` cobrem a tool isolada. Aqui o que se
verifica é o transporte: o valor viaja num `ContextVar` (`tools/context.py`),
e o run do agente não acontece na mesma task em todos os caminhos — com o
Langfuse ligado ele roda numa task própria, e o `/chat/stream` é consumido pela
task do sse-starlette. `ContextVar` não atravessa task criada depois do `set`
de qualquer jeito, então isto é o tipo de coisa que quebra calado.

Nenhum modelo é chamado: o agente é falso e a tool real, com o HTTP num
`MockTransport`.
"""

import asyncio
from contextlib import aclosing

import httpx
import pytest
from agno.run.agent import RunCompletedEvent, RunContentEvent

from agent_service.api.routes import ChatRequest, chat, chat_stream
from agent_service.observability import tracing
from agent_service.observability.tracing import RunContext, traced_run_events
from agent_service.tools import api_tool
from agent_service.tools.api_tool import build_api_function
from agent_service.tools.context import get_dependencies

CONFIG = {
    "method": "GET",
    "url": "https://exemplo.test/clientes/{cpf}",
    "parameters": [
        {
            "name": "cpf",
            "type": "string",
            "location": "path",
            "required": True,
            "source": "dependency",
            "dependency": "cpf",
        },
        {"name": "assunto", "type": "string", "location": "query", "required": True},
    ],
}


class AgentQueChamaATool:
    """Agente falso que faz o que interessa aqui: chamar a tool de dentro do run."""

    def __init__(self, tool) -> None:
        self.tool = tool
        self.resultado: str | None = None

    def arun(self, message, **kwargs):
        async def stream():
            yield RunContentEvent(content="")
            self.resultado = await self.tool.entrypoint(assunto="fatura")
            yield RunCompletedEvent(run_id=kwargs.get("run_id"), content=self.resultado)

        return stream()


@pytest.fixture
def tool_e_chamadas():
    """A tool real, com o HTTP interceptado — devolve a URL que ela montou."""
    chamadas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    original = api_tool.get_client
    api_tool.get_client = lambda: client
    try:
        yield build_api_function(tool_name="ficha", description=None, config=CONFIG), chamadas
    finally:
        api_tool.get_client = original


def _run(**overrides) -> RunContext:
    campos = {
        "endpoint": "chat",
        "agent_type": "suporte",
        "agent_name": "Suporte",
        "prompt_version": 1,
        "user_id": "u1",
        "session_id": "s1",
        "message": "quero ver minha fatura",
        "dependencies": {"cpf": "12345678900"},
    }
    return RunContext(**{**campos, **overrides})


async def _consumir(agent, run) -> None:
    async with aclosing(traced_run_events(agent, run)) as eventos:
        async for _ in eventos:
            pass


def test_dependency_chega_na_tool_durante_o_run(tool_e_chamadas):
    tool, chamadas = tool_e_chamadas
    agent = AgentQueChamaATool(tool)
    asyncio.run(_consumir(agent, _run()))
    assert chamadas == ["https://exemplo.test/clientes/12345678900?assunto=fatura"]


def test_dependency_chega_na_tool_com_o_langfuse_ligado(tool_e_chamadas, monkeypatch):
    """Com o Langfuse ligado o run vai para uma task própria (`asyncio.create_task`
    em `traced_run_events`). A task herda o contexto de quem a criou — mas só o
    que já estava setado nele."""
    tool, chamadas = tool_e_chamadas

    class ClienteFalso:
        def start_as_current_observation(self, **kwargs):
            from contextlib import nullcontext

            return nullcontext(_ObservacaoFalsa())

        def update_current_trace(self, **kwargs):
            pass

    class _ObservacaoFalsa:
        def update(self, **kwargs):
            pass

    monkeypatch.setattr(tracing, "_client", ClienteFalso())
    agent = AgentQueChamaATool(tool)
    asyncio.run(_consumir(agent, _run()))
    assert chamadas == ["https://exemplo.test/clientes/12345678900?assunto=fatura"]


def test_dependency_chega_na_tool_pelo_chat_stream(tool_e_chamadas, monkeypatch):
    """No `/chat/stream` quem itera o generator é a task do sse-starlette, não a
    que atendeu a requisição."""
    tool, chamadas = tool_e_chamadas
    agent = AgentQueChamaATool(tool)
    monkeypatch.setattr("agent_service.api.routes._resolve", lambda request, endpoint: (agent, _run(endpoint=endpoint)))

    async def consumir() -> list[str]:
        response = await chat_stream(ChatRequest(agent_type="suporte", user_id="u1", session_id="s1", message="oi"))
        eventos = []
        async for chunk in response.body_iterator:
            eventos.append(chunk)
        return eventos

    eventos = asyncio.run(consumir())
    assert chamadas == ["https://exemplo.test/clientes/12345678900?assunto=fatura"]
    assert any("done" in str(e) for e in eventos)


def test_dependency_nao_aparece_no_schema_que_o_modelo_ve(tool_e_chamadas):
    """O ponto do `source="dependency"`: o modelo não vê o parâmetro, então não
    tem como inventar nem trocar o valor."""
    tool, _ = tool_e_chamadas
    propriedades = tool.parameters["properties"]
    assert "cpf" not in propriedades
    assert "assunto" in propriedades


def test_chat_valida_dependency_fields_antes_de_chamar_o_agente(monkeypatch):
    """Campo obrigatório ausente é 422 na porta, não erro dentro do run."""
    from fastapi import HTTPException

    monkeypatch.setattr(
        "agent_service.api.routes.get_agent_with_definition",
        lambda agent_type: (
            object(),
            {
                "name": "Suporte",
                "prompt_version": 1,
                "dependency_fields": [{"name": "cpf", "type": "string", "required": True}],
            },
        ),
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat(ChatRequest(agent_type="suporte", user_id="u", session_id="s", message="oi")))
    assert exc.value.status_code == 422
    assert "cpf" in str(exc.value.detail)


def test_dependencies_nao_vazam_entre_requisicoes(tool_e_chamadas):
    """Cada requisição do FastAPI roda no seu contexto — o `set` de uma não
    pode aparecer na seguinte."""
    tool, chamadas = tool_e_chamadas
    agent = AgentQueChamaATool(tool)
    asyncio.run(_consumir(agent, _run()))
    assert chamadas, "a tool precisa ter sido chamada para o teste dizer alguma coisa"
    assert get_dependencies() == {}, "o contexto da requisição não deveria sobrar no processo"
