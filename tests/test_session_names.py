"""Título da conversa gravado na sessão do Agno, sem as dependencies (`agents/session_names.py`)."""

from agno.session.agent import AgentSession

from agent_service.agents import session_names
from agent_service.agents.session_names import ensure_session_name, title_from


class Banco:
    def __init__(self):
        self.gravadas = []

    def upsert_session(self, session):
        self.gravadas.append(session)


class Agente:
    def __init__(self, session):
        self.db = Banco()
        self.session = session
        self.lidas = 0

    def get_session(self, session_id, user_id):
        self.lidas += 1
        return self.session


def test_titulo_e_a_primeira_linha_curta():
    assert title_from("  meu wifi caiu\nmais detalhes") == "meu wifi caiu"
    assert len(title_from("x" * 200)) == session_names.MAX_TITLE
    assert title_from("   ") is None


def test_grava_o_titulo_uma_vez_e_nao_relê(monkeypatch):
    monkeypatch.setattr(session_names, "_named", type(session_names._named)())
    sessao = AgentSession(session_id="s-titulo", agent_id="a", user_id="u", session_data={"session_state": {"x": 1}})
    agente = Agente(sessao)
    ensure_session_name(agente, "s-titulo", "u", "quero cancelar meu plano")
    assert sessao.session_data == {"session_state": {"x": 1}, "session_name": "quero cancelar meu plano"}
    assert len(agente.db.gravadas) == 1
    ensure_session_name(agente, "s-titulo", "u", "segunda mensagem")
    assert agente.lidas == 1 and len(agente.db.gravadas) == 1  # já nomeada: nem consulta o banco


def test_nome_existente_nao_e_trocado(monkeypatch):
    monkeypatch.setattr(session_names, "_named", type(session_names._named)())
    sessao = AgentSession(session_id="s-renomeada", agent_id="a", user_id="u", session_data={"session_name": "Cliente X"})
    agente = Agente(sessao)
    ensure_session_name(agente, "s-renomeada", "u", "oi")
    assert sessao.session_data["session_name"] == "Cliente X" and agente.db.gravadas == []


def test_sem_banco_ou_sem_sessao_nao_quebra(monkeypatch):
    monkeypatch.setattr(session_names, "_named", type(session_names._named)())
    ensure_session_name(object(), "s1", "u", "oi")
    sem_sessao = Agente(None)
    ensure_session_name(sem_sessao, "s2", "u", "oi")
    assert sem_sessao.db.gravadas == []
