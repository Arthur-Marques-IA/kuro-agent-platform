"""Run de /chat que volta vazio (sem texto e sem tool).

Caso real: o Gemini 2.5 Flash devolvendo 0 tokens de saída no 1º turno de uma
sessão nova. O Agno grava o run na sessão de qualquer jeito, e quem tenta de
novo ficava com a fala do usuário duplicada e uma resposta vazia no histórico,
reenviada nas próximas `num_history_runs` rodadas.
"""

import asyncio
import uuid

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from agent_service.api.routes import ChatRequest, chat, chat_stream
from agent_service.config import get_settings
from agent_service.observability import run_store, tracing
from agent_service.observability.tracing import RunContext


class AgenteFalso:
    """Devolve, a cada chamada, a próxima resposta da lista ("" = run vazio)."""

    db = None

    def __init__(self, respostas: list[str]) -> None:
        self.respostas = list(respostas)
        self.run_ids: list[str] = []

    def arun(self, message, **kwargs):
        resposta = self.respostas.pop(0)
        self.run_ids.append(kwargs["run_id"])

        async def stream():
            if resposta:
                yield RunContentEvent(content=resposta)
            yield RunCompletedEvent(run_id=kwargs["run_id"], content=resposta)

        return stream()


def _run(**overrides) -> RunContext:
    campos = {
        "endpoint": "chat",
        "agent_type": "suporte",
        "agent_name": "Suporte",
        "prompt_version": 1,
        "user_id": "u1",
        "session_id": "s1",
        "message": "meu wifi caiu",
    }
    return RunContext(**{**campos, **overrides})


@pytest.fixture
def gravados(monkeypatch):
    registros: list[run_store.RunRecord] = []
    monkeypatch.setattr(run_store, "record_run", registros.append)
    return registros


@pytest.fixture
def tentativas(monkeypatch):
    def definir(n: int) -> None:
        monkeypatch.setattr(get_settings(), "empty_response_retries", n)

    return definir


def _usar(monkeypatch, agent) -> None:
    monkeypatch.setattr("agent_service.api.routes._resolve", lambda request, endpoint: (agent, _run(endpoint=endpoint)))


def _pedido() -> ChatRequest:
    return ChatRequest(agent_type="suporte", user_id="u1", session_id="s1", message="meu wifi caiu")


def test_chat_tenta_de_novo_quando_o_run_volta_vazio(monkeypatch, gravados, tentativas):
    tentativas(1)
    agent = AgenteFalso(["", "Reinicie o roteador."])
    _usar(monkeypatch, agent)

    resposta = asyncio.run(chat(_pedido()))

    assert resposta.content == "Reinicie o roteador."
    vazio, bom = gravados
    assert resposta.run_id == bom.run_id == agent.run_ids[1]
    # O vazio continua no trace store, marcado; a tentativa aponta para ele.
    assert vazio.run_id == agent.run_ids[0]
    assert vazio.metadata["empty_response"] == "true"
    assert bom.metadata["retry_of"] == vazio.run_id
    assert "empty_response" not in bom.metadata


def test_chat_sem_nova_tentativa_devolve_vazio(monkeypatch, gravados, tentativas):
    tentativas(0)
    agent = AgenteFalso(["", "nunca chega aqui"])
    _usar(monkeypatch, agent)

    resposta = asyncio.run(chat(_pedido()))

    assert resposta.content == ""
    assert len(gravados) == 1 and gravados[0].metadata["empty_response"] == "true"


def test_chat_desiste_depois_das_tentativas(monkeypatch, gravados, tentativas):
    tentativas(2)
    agent = AgenteFalso(["", "", ""])
    _usar(monkeypatch, agent)

    resposta = asyncio.run(chat(_pedido()))

    assert resposta.content == ""
    assert len(gravados) == 3
    assert all(r.metadata["empty_response"] == "true" for r in gravados)


def test_run_com_texto_nao_e_repetido(monkeypatch, gravados, tentativas):
    tentativas(1)
    agent = AgenteFalso(["oi"])
    _usar(monkeypatch, agent)

    assert asyncio.run(chat(_pedido())).content == "oi"
    assert len(agent.run_ids) == 1
    assert "empty_response" not in gravados[0].metadata


def test_chat_stream_tenta_de_novo_e_anuncia_o_run_novo(monkeypatch, gravados, tentativas):
    tentativas(1)
    agent = AgenteFalso(["", "Reinicie o roteador."])
    _usar(monkeypatch, agent)

    async def consumir() -> list[dict]:
        response = await chat_stream(_pedido())
        return [chunk async for chunk in response.body_iterator]

    eventos = asyncio.run(consumir())
    runs = [e for e in eventos if e["event"] == "run"]
    mensagens = [e for e in eventos if e["event"] == "message"]
    assert len(runs) == 2
    assert agent.run_ids[1] in runs[1]["data"] and agent.run_ids[0] in runs[1]["data"]
    assert len(mensagens) == 1 and "Reinicie o roteador." in mensagens[0]["data"]
    assert eventos[-1]["event"] == "done"


def test_run_vazio_sai_do_historico_da_sessao(tmp_path):
    """Num banco do Agno (SQLite aqui; o serviço usa o Postgres): o run vazio vira `error` na sessão do Agno, que o
    deixa de fora do histórico (`HISTORY_SKIP_STATUSES`) sem apagar nada."""
    db = SqliteDb(db_file=str(tmp_path / "agno.db"))
    session_id = f"s-{uuid.uuid4().hex}"

    def run_output(run_id: str, resposta: str) -> RunOutput:
        return RunOutput(
            run_id=run_id,
            session_id=session_id,
            user_id="u1",
            content=resposta,
            status=RunStatus.completed,
            messages=[Message(role="user", content="meu wifi caiu"), Message(role="assistant", content=resposta)],
        )

    db.upsert_session(AgentSession(session_id=session_id, user_id="u1", agent_id="suporte"))
    for indice, (run_id, resposta) in enumerate([("r-bom", "Reinicie o roteador."), ("r-vazio", "")]):
        db.upsert_run(run_output(run_id, resposta), session_id=session_id, user_id="u1", run_index=indice)

    class Agente:
        pass

    agente = Agente()
    agente.db = db
    tracing._exclude_from_history(agente, _run(session_id=session_id, run_id="r-vazio"))

    # O histórico que o Agno monta: os runs da sessão, menos os de HISTORY_SKIP_STATUSES.
    sessao = AgentSession(session_id=session_id, user_id="u1", runs=db.get_runs(session_id=session_id))
    assert db.get_run("r-bom").status == RunStatus.completed
    assert db.get_run("r-vazio").status == RunStatus.error
    historico = sessao.get_messages()
    assert [m.content for m in historico if m.role == "assistant"] == ["Reinicie o roteador."]
