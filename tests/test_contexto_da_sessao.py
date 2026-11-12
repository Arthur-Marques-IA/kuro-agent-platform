"""O contexto da entrada estruturada como estado da sessão, com o Agno de verdade.

Um agente real (`agents/base.py::build_agent`, o `KuroAgent`), com histórico numa
sessão do Agno em SQLite e um modelo falso que guarda as mensagens que recebeu em
cada chamada. É o que prova o desenho:

- o contexto vigente vai **uma vez**, no prompt de sistema, depois das instruções;
- nenhuma mensagem do histórico traz o texto dos blocos (ele não passa pela
  mensagem do usuário, que é o que o Agno grava e reenvia);
- `context` ausente herda o último, uma lista substitui, `[]` limpa;
- uma chamada só com `message` numa sessão sem contexto manda ao modelo exatamente
  o que mandaria o `Agent` do Agno sem nada do Kuro no meio;
- o agente em cache, rodando duas conversas ao mesmo tempo, não mistura os contextos;
- `agent_sent` igual à última resposta sai do texto e fica gravado com `echo: true`;
- `history_tool_calls` limita as chamadas de tool antigas que voltam no histórico.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.metrics import MessageMetrics
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse

from agent_service.agents import base, session_context
from agent_service.agents.base import KuroAgent, build_agent
from agent_service.api import routes
from agent_service.api.routes import ChatRequest, chat, chat_stream
from agent_service.db import get_db
from agent_service.observability import run_store, trace_store, tracing

INSTRUCOES = ["Você atende alunos.", "Responda curto."]


class ModeloQueGuarda(Model):
    """Responde `Resposta: <última fala>` e guarda, por chamada, as mensagens recebidas.

    Com `tool`, chama essa tool uma vez por turno (quando a última mensagem é do
    usuário) e responde depois do resultado — para o histórico ter tools antigas."""

    name: str = "ModeloQueGuarda"
    provider: str = "teste"

    def __init__(self, *args: Any, tool: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.chamadas: list[list[Message]] = []
        self.tool = tool
        self.em_andamento = 0
        self.pico = 0

    def _resposta(self, messages: list[Message]) -> ModelResponse:
        self.chamadas.append([m.model_copy(deep=True) for m in messages])
        ultima = messages[-1]
        if self.tool and ultima.role == "user":
            response = ModelResponse(
                role="assistant",
                tool_calls=[
                    {
                        "id": f"call-{uuid4().hex[:8]}",
                        "type": "function",
                        "function": {"name": self.tool, "arguments": "{}"},
                    }
                ],
            )
        else:
            fala = next((m.content for m in reversed(messages) if m.role == "user"), "")
            texto = fala if isinstance(fala, str) else str(fala)
            response = ModelResponse(role="assistant", content="Resposta: " + texto.splitlines()[-1].strip())
        response.response_usage = MessageMetrics(input_tokens=10, output_tokens=5, total_tokens=15)
        return response

    def invoke(self, messages: list[Message], assistant_message: Message, **kwargs: Any) -> ModelResponse:
        return self._resposta(messages)

    async def ainvoke(self, messages: list[Message], assistant_message: Message, **kwargs: Any) -> ModelResponse:
        await asyncio.sleep(0)
        return self._resposta(messages)

    def invoke_stream(
        self, messages: list[Message], assistant_message: Message, **kwargs: Any
    ) -> Iterator[ModelResponse]:
        yield self._resposta(messages)

    async def ainvoke_stream(
        self, messages: list[Message], assistant_message: Message, **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        # Cede a vez antes de responder: duas conversas ao mesmo tempo se intercalam aqui.
        self.em_andamento += 1
        self.pico = max(self.pico, self.em_andamento)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.em_andamento -= 1
        yield self._resposta(messages)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    session_context.forget()
    yield
    trace_store.set_trace_store(None)
    session_context.forget()


@pytest.fixture
def banco_do_agno(tmp_path: Path) -> SqliteDb:
    return SqliteDb(db_url=f"sqlite:///{(tmp_path / 'agno.db').as_posix()}")


def _montar(monkeypatch, banco: SqliteDb, modelo: ModeloQueGuarda, **kwargs: Any) -> KuroAgent:
    monkeypatch.setattr(base, "get_db", lambda: banco)
    monkeypatch.setattr(base, "get_model", lambda **_: modelo)
    agent = build_agent(agent_id="contexto", name="Contexto", instructions=INSTRUCOES, **kwargs)
    assert isinstance(agent, KuroAgent)
    return agent


@pytest.fixture
def modelo() -> ModeloQueGuarda:
    return ModeloQueGuarda(id="modelo-que-guarda")


@pytest.fixture
def agente(monkeypatch, banco_do_agno, modelo) -> KuroAgent:
    agent = _montar(monkeypatch, banco_do_agno, modelo)
    definicao = {
        "kind": "conversational",
        "name": "Contexto",
        "prompt_version": 1,
        "agent_version": 1,
        "config_hash": "h",
        "dependency_fields": [],
    }
    monkeypatch.setattr(routes, "get_agent_with_definition", lambda agent_type: (agent, definicao))
    return agent


def _pedido(session_id: str, **campos: Any) -> ChatRequest:
    return ChatRequest.model_validate({"agent_type": "contexto", "user_id": "u", "session_id": session_id, **campos})


def _turno(session_id: str, **campos: Any):
    return asyncio.run(chat(_pedido(session_id, **campos)))


def _sistema(chamada: list[Message]) -> str:
    sistemas = [m for m in chamada if m.role == "system"]
    assert len(sistemas) == 1
    return str(sistemas[0].content)


def _fora_do_sistema(chamada: list[Message]) -> list[str]:
    return [str(m.content or "") for m in chamada if m.role != "system"]


def _ocorrencias(chamada: list[Message], trecho: str) -> tuple[int, int]:
    """Quantas vezes `trecho` aparece no prompt de sistema e no resto das mensagens."""
    return _sistema(chamada).count(trecho), sum(texto.count(trecho) for texto in _fora_do_sistema(chamada))


def _contexto_gravado(run_id: str) -> Any:
    with get_db().db_engine.connect() as conn:
        return conn.execute(
            sa.select(run_store.runs.c.input_context, run_store.runs.c.message, run_store.runs.c.input_events).where(
                run_store.runs.c.run_id == run_id
            )
        ).one()


DIVIDA_V1 = "SEM parcela vencida. Chaves {cpf} ${user_id} $user_id {} e $$ ficam como estão."
DIVIDA_V2 = "1 parcela vencida em 10/11."
DADOS = "Nome: Maria\nCurso: Pedagogia"


def _contexto(divida: str) -> list[dict[str, Any]]:
    return [
        {"key": "dados_aluno", "title": "Dados do aluno", "content": DADOS},
        {"key": "divida", "title": "Contexto da dívida", "content": divida},
    ]


# -- aceite 1, 2 e 3 -----------------------------------------------------------------


def test_contexto_vai_uma_vez_no_sistema_e_nunca_no_historico(agente, modelo):
    sid = f"s-{uuid4().hex[:8]}"
    respostas = [
        _turno(sid, message="oi", context=_contexto(DIVIDA_V1), dependencies={"cpf": "12345678900"}),
        _turno(sid, message="quero a segunda via", dependencies={"cpf": "12345678900"}),
        _turno(sid, message="obrigada", events=[{"role": "contact", "content": "pode ser por e-mail?"}]),
    ]

    assert len(modelo.chamadas) == 3
    for chamada in modelo.chamadas:
        for trecho in (DIVIDA_V1, "Nome: Maria\nCurso: Pedagogia", "# Contexto\n\nEste é o contexto atual"):
            assert _ocorrencias(chamada, trecho) == (1, 0), trecho
        sistema = _sistema(chamada)
        # Depois das instruções, e o conteúdo chega como veio: nada de template.
        assert sistema.index("Responda curto.") < sistema.index("# Contexto")
        assert "{cpf}" in sistema and "12345678900" not in sistema
    # O histórico do terceiro turno traz os dois anteriores, só com a conversa.
    terceira = modelo.chamadas[2]
    usuario = [str(m.content) for m in terceira if m.role == "user"]
    assert len(usuario) == 3 and usuario[0].startswith("# Mensagem nova do contato\n\noi")
    # O run grava o contexto vigente em todo turno, herdado ou não; a mensagem, sem ele.
    for resposta in respostas:
        contexto, mensagem, _ = _contexto_gravado(resposta.run_id)
        assert [b["content"] for b in contexto] == [DADOS, DIVIDA_V1]
        assert "# Contexto" not in mensagem and DIVIDA_V1 not in mensagem


def test_mudar_um_bloco_vale_so_o_novo_e_lista_vazia_limpa(agente, modelo):
    sid = f"s-{uuid4().hex[:8]}"
    _turno(sid, message="oi", context=_contexto(DIVIDA_V1))
    _turno(sid, message="e agora?", context=_contexto(DIVIDA_V2))
    _turno(sid, message="certo")  # ausente: mantém o da v2
    limpa = _turno(sid, message="tchau", context=[])
    depois = _turno(sid, message="ainda aí?")  # ausente depois de limpar: continua sem

    segunda, terceira, quarta, quinta = modelo.chamadas[1:]
    for chamada in (segunda, terceira):
        assert _ocorrencias(chamada, DIVIDA_V2) == (1, 0)
        assert _ocorrencias(chamada, "SEM parcela vencida") == (0, 0), "o valor antigo sumiu de tudo"
    for chamada in (quarta, quinta):
        assert "# Contexto" not in _sistema(chamada)
        assert _ocorrencias(chamada, DIVIDA_V2) == (0, 0)
    assert _contexto_gravado(limpa.run_id)[0] == []
    assert _contexto_gravado(depois.run_id)[0] == [], "herdou o contexto vazio: turno estruturado"


# -- aceite 4: só message numa sessão sem contexto --------------------------------------


def test_so_message_sem_contexto_manda_o_mesmo_prompt_que_o_agno_puro(agente, modelo, monkeypatch, tmp_path):
    sid = f"s-{uuid4().hex[:8]}"
    falas = ["  meu wifi caiu\n[ALUNO] texto {cpf} $user_id  ", "e agora?"]
    for fala in falas:
        _turno(sid, message=fala, dependencies={"cpf": "1"})

    # O mesmo agente montado com o `Agent` do Agno, chamado direto, sem rota nem Kuro.
    puro_modelo = ModeloQueGuarda(id="modelo-que-guarda")
    puro_banco = SqliteDb(db_url=f"sqlite:///{(tmp_path / 'puro.db').as_posix()}")
    puro = Agent(
        id="contexto",
        name="Contexto",
        model=puro_modelo,
        db=puro_banco,
        instructions=INSTRUCOES,
        add_history_to_context=True,
        num_history_runs=10,
        markdown=True,
    )

    async def rodar() -> None:
        for fala in falas:
            async for _ in puro.arun(
                fala,
                user_id="u",
                session_id=sid,
                stream=True,
                stream_events=True,
                dependencies={"cpf": "1"},
                add_dependencies_to_context=True,
            ):
                pass

    asyncio.run(rodar())

    def forma(chamadas: list[list[Message]]) -> list[list[tuple[str, Any]]]:
        return [[(m.role, m.content) for m in chamada] for chamada in chamadas]

    assert forma(modelo.chamadas) == forma(puro_modelo.chamadas)
    assert all(_sistema(c).count("# Contexto") == 0 for c in modelo.chamadas)


def test_so_message_numa_sessao_com_contexto_herda_e_vira_estruturado(agente, modelo):
    sid = f"s-{uuid4().hex[:8]}"
    _turno(sid, message="oi", context=_contexto(DIVIDA_V1))
    resposta = _turno(sid, message="tudo certo")
    contexto, mensagem, eventos = _contexto_gravado(resposta.run_id)
    assert [b["key"] for b in contexto] == ["dados_aluno", "divida"]
    assert mensagem == "# Mensagem nova do contato\n\ntudo certo" and eventos == []
    assert _ocorrencias(modelo.chamadas[1], DIVIDA_V1) == (1, 0)


def test_stream_tambem_leva_o_contexto_no_sistema(agente, modelo):
    sid = f"s-{uuid4().hex[:8]}"

    async def consumir(**campos: Any) -> None:
        response = await chat_stream(_pedido(sid, **campos))
        async for _ in response.body_iterator:
            pass

    asyncio.run(consumir(message="oi", context=_contexto(DIVIDA_V1)))
    asyncio.run(consumir(message="de novo"))
    for chamada in modelo.chamadas:
        assert _ocorrencias(chamada, DIVIDA_V1) == (1, 0)


def test_herda_mesmo_antes_da_gravacao_do_run_anterior_chegar_ao_banco(agente, modelo, monkeypatch):
    """A fila de gravação do trace (`run_store.TraceWriter`) pode estar atrás do turno
    seguinte: o cache da sessão, alimentado no caminho do run, cobre esse intervalo."""
    pendentes: list[Any] = []
    monkeypatch.setattr(run_store, "_submit", pendentes.append)  # nada chega ao banco
    sid = f"s-{uuid4().hex[:8]}"
    _turno(sid, message="oi", context=_contexto(DIVIDA_V1))
    _turno(sid, message="Resposta: oi", events=[{"role": "agent_sent", "content": "Resposta: oi"}])
    assert _ocorrencias(modelo.chamadas[1], DIVIDA_V1) == (1, 0)
    assert "Enviado em seu nome" not in _fora_do_sistema(modelo.chamadas[1])[-1]
    # Sem o cache (outro processo, ou reiniciado), o banco é a fonte — aqui ainda vazio.
    session_context.forget()
    assert session_context.current_context(sid) is None
    for gravar in pendentes:
        gravar()
    assert [b["key"] for b in session_context.current_context(sid) or []] == ["dados_aluno", "divida"]
    assert session_context.last_reply(sid) == "Resposta: Resposta: oi"


# -- linha do tempo -------------------------------------------------------------------------


def test_linha_do_tempo_mostra_o_contexto_herdado_sem_mudanca(agente):
    from agent_service.observability.transcript import build_timeline

    sid = f"s-{uuid4().hex[:8]}"
    _turno(sid, message="oi", context=_contexto(DIVIDA_V1))
    _turno(sid, message="segue")  # herda
    _turno(sid, message="e agora?", context=_contexto(DIVIDA_V2))
    _turno(sid, message="ok", events=[{"role": "contact", "content": "certo"}])  # herda a v2

    linha = build_timeline(sid, run_store.session_timeline(sid))
    primeiro, herdado, mudou, herdado_de_novo = linha.items
    assert all(t.structured for t in linha.items)
    for turno in (primeiro, herdado, herdado_de_novo):
        assert [b.key for b in turno.context] == ["dados_aluno", "divida"]
        assert not any(b.changed for b in turno.context)
    assert [b.content for b in herdado.context] == [DADOS, DIVIDA_V1]
    divida = mudou.context[1]
    assert divida.changed and divida.previous_content == DIVIDA_V1 and not mudou.context[0].changed
    assert herdado_de_novo.context[1].content == DIVIDA_V2


# -- concorrência: o agente em cache com duas conversas ao mesmo tempo --------------------


def test_duas_conversas_ao_mesmo_tempo_nao_misturam_o_contexto(agente, modelo):
    sessoes = {f"s-{uuid4().hex[:8]}-{i}": f"contexto exclusivo da conversa {i}" for i in range(4)}

    async def conversa(sid: str, texto: str) -> None:
        bloco = [{"key": "k", "title": "Bloco", "content": texto}]
        await chat(_pedido(sid, message=f"fala de {sid}", context=bloco))
        await chat(_pedido(sid, message=f"de novo {sid}"))

    async def todas() -> None:
        await asyncio.gather(*(conversa(sid, texto) for sid, texto in sessoes.items()))

    asyncio.run(todas())
    assert modelo.pico >= 2, "as conversas precisam ter rodado intercaladas para o teste dizer algo"
    assert len(modelo.chamadas) == 2 * len(sessoes)
    for chamada in modelo.chamadas:
        fala = _fora_do_sistema(chamada)[-1]
        sid = next(s for s in sessoes if s in fala)
        sistema = _sistema(chamada)
        for outro, texto in sessoes.items():
            assert sistema.count(texto) == (1 if outro == sid else 0)
    assert agente.additional_context is None, "o agente em cache não guardou nada de nenhuma conversa"


def test_copia_do_agente_nao_leva_o_contexto_do_run(agente):
    from agent_service.agents.system_context import set_system_context

    set_system_context("# Contexto\n\nnão copie")
    try:
        assert agente.additional_context == "# Contexto\n\nnão copie"
        copia = agente.deep_copy()
    finally:
        set_system_context(None)
    assert copia.additional_context is None, "a cópia guardou só o valor configurado"


# -- aceite 5: agent_sent repetido -------------------------------------------------------


def test_agent_sent_igual_a_ultima_resposta_sai_do_texto_e_fica_gravado(agente, modelo):
    sid = f"s-{uuid4().hex[:8]}"
    primeira = _turno(sid, message="qual o prazo?", context=_contexto(DIVIDA_V1))
    assert primeira.content == "Resposta: qual o prazo?"

    segunda = _turno(
        sid,
        message="ok",
        events=[
            {"role": "agent_sent", "content": "  Resposta:\n  qual o prazo?  "},  # mesmo texto, outro espaçamento
            {"role": "agent_sent", "content": "Lembrete: o boleto vence amanhã."},
        ],
    )
    texto = _fora_do_sistema(modelo.chamadas[1])[-1]
    assert texto.count("Enviado em seu nome") == 1
    assert "Lembrete: o boleto vence amanhã." in texto and "qual o prazo?" not in texto.split("# Mensagem")[0]
    # O histórico já traz a resposta, uma vez.
    assert sum(t.count("Resposta: qual o prazo?") for t in _fora_do_sistema(modelo.chamadas[1])) == 1
    _, mensagem, eventos = _contexto_gravado(segunda.run_id)
    assert [e.get("echo") for e in eventos] == [True, None]
    assert eventos[0]["content"] == "  Resposta:\n  qual o prazo?  ", "gravado como veio"
    assert "qual o prazo" not in mensagem


def test_agent_sent_diferente_da_ultima_resposta_fica(agente, modelo):
    sid = f"s-{uuid4().hex[:8]}"
    _turno(sid, message="qual o prazo?", events=[])
    _turno(sid, message="ok", events=[{"role": "agent_sent", "content": "Resposta: qual o prazo? Até sexta."}])
    texto = _fora_do_sistema(modelo.chamadas[1])[-1]
    assert texto.startswith("# Conversa desde a sua última resposta\n\n[Enviado em seu nome]\nResposta: qual o prazo?")


@pytest.mark.parametrize("com_cache", [True, False])
def test_analise_na_mesma_sessao_nao_vira_a_ultima_resposta(agente, modelo, com_cache):
    """O `/analyze` aceita a `session_id` da conversa (agrupa as decisões de um analista com
    ela). A saída JSON dele, mais nova que a resposta do chat, não pode ser a "última
    resposta" do eco, nem com o cache quente nem lendo do banco."""
    from datetime import datetime, timedelta, timezone

    sid = f"s-{uuid4().hex[:8]}"
    primeira = _turno(sid, message="qual o prazo?", context=_contexto(DIVIDA_V1))
    run_store.record_run(
        run_store.RunRecord(
            run_id=str(uuid4()),
            trace_id=uuid4().hex,
            agent_type="triagem",
            agent_name="Triagem",
            prompt_version=1,
            endpoint="analyze",
            user_id="u",
            session_id=sid,
            message="{}",
            started_at=datetime.now(timezone.utc) + timedelta(seconds=5),
            output='{"fase": "cobranca"}',
        )
    )
    if not com_cache:
        session_context.forget()
    assert session_context.last_reply(sid) == primeira.content
    assert [b["key"] for b in session_context.current_context(sid) or []] == ["dados_aluno", "divida"]
    segunda = _turno(sid, message="ok", events=[{"role": "agent_sent", "content": primeira.content}])
    assert [e.get("echo") for e in _contexto_gravado(segunda.run_id)[2]] == [True]


# -- aceite 6: history_tool_calls ---------------------------------------------------------


def _agente_com_tool(monkeypatch, banco: SqliteDb, history_tool_calls: int | None) -> tuple[KuroAgent, ModeloQueGuarda]:
    def consultar_boleto() -> str:
        """Consulta o boleto do aluno."""
        return json.dumps({"boleto": "123"})

    modelo = ModeloQueGuarda(id="modelo-com-tool", tool="consultar_boleto")
    agent = _montar(monkeypatch, banco, modelo, tools=[consultar_boleto], history_tool_calls=history_tool_calls)
    return agent, modelo


async def _conversar(agent: KuroAgent, sid: str, falas: list[str]) -> None:
    for fala in falas:
        async for _ in agent.arun(fala, user_id="u", session_id=sid, stream=True, stream_events=True):
            pass


@pytest.mark.parametrize("limite, esperadas", [(None, 3), (1, 1), (0, 0)])
def test_history_tool_calls_limita_as_tools_antigas_do_historico(monkeypatch, banco_do_agno, limite, esperadas):
    agent, modelo = _agente_com_tool(monkeypatch, banco_do_agno, limite)
    assert agent.max_tool_calls_from_history == limite
    sid = f"s-{uuid4().hex[:8]}"
    asyncio.run(_conversar(agent, sid, ["um", "dois", "três", "quatro"]))
    # Primeira chamada do quarto turno: só o histórico (três turnos, uma tool em cada) e a fala nova.
    quarta = next(c for c in modelo.chamadas if c[-1].role == "user" and c[-1].content == "quatro")
    assert sum(1 for m in quarta if m.role == "tool") == esperadas
    chamadas_de_tool = [tc for m in quarta if m.role == "assistant" for tc in (m.tool_calls or [])]
    assert len(chamadas_de_tool) == esperadas
