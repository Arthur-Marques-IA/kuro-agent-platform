"""Entrada estruturada do `/chat`: `events` e `context` em campos, o texto do modelo
montado pelo servidor com um gabarito fixo, e as três colunas do run.

O agente é sempre falso (guarda a mensagem que recebeu): nada sai para a rede."""

import asyncio
import json
from uuid import uuid4

import pytest
import sqlalchemy as sa
from agno.run.agent import RunCompletedEvent
from fastapi import HTTPException
from pydantic import ValidationError

from agent_service.agents import structured_input
from agent_service.agents.system_context import current_system_context
from agent_service.api import idempotency, routes
from agent_service.api.observability_routes import get_run_trace
from agent_service.api.routes import ChatRequest, chat, chat_stream
from agent_service.config import get_settings
from agent_service.observability import run_store, trace_store, tracing

REGENTE = {
    "message": "boa tarde, me envia novamente o comprovante",
    "events": [
        {"role": "agent_sent", "content": "Maria, recebi seus dados...", "at": "2026-11-11T12:36:10Z"},
        {"role": "contact", "content": "Para finalizar o diploma. Obrigada.", "at": "2026-11-11T12:37:01Z"},
        {"role": "contact", "content": "boa tarde, me envia novamente o comprovante", "at": "2026-11-11T12:37:15Z"},
    ],
    "context": [
        {"key": "configuracao", "title": "Configuração desta conversa", "content": "EFETIVAÇÃO: LIGADA"},
        {"key": "dados_aluno", "title": "Dados do aluno", "content": "Nome: Maria\nCurso: Pedagogia"},
        {"key": "divida", "title": "Contexto da dívida", "content": "SEM parcela vencida"},
        {"key": "anotacoes", "title": "Anotações internas", "content": "Pediu 2ª via em outubro."},
        {"key": "temporal", "title": "Contexto temporal", "content": "Janela 24h ABERTA", "volatile": True},
    ],
}

# Os gabaritos, letra por letra. Mudou de propósito? Atualize aqui e nas notas de versão:
# o texto que o modelo lê muda para todo agente que recebe entrada estruturada.
# O contexto vai no prompt de sistema; a mensagem do turno, só com eventos e fala.
SNAPSHOT_CONTEXTO = """\
# Contexto

Este é o contexto atual desta conversa. Ele substitui qualquer versão anterior.

## Configuração desta conversa
EFETIVAÇÃO: LIGADA

## Dados do aluno
Nome: Maria
Curso: Pedagogia

## Contexto da dívida
SEM parcela vencida

## Anotações internas
Pediu 2ª via em outubro.

## Contexto temporal
Janela 24h ABERTA"""

SNAPSHOT_REGENTE = """\
# Conversa desde a sua última resposta

[Enviado em seu nome · 2026-11-11T12:36:10Z]
Maria, recebi seus dados...

[Contato · 2026-11-11T12:37:01Z]
Para finalizar o diploma. Obrigada.

# Mensagem nova do contato

boa tarde, me envia novamente o comprovante"""


def _request(**overrides) -> ChatRequest:
    body = {"agent_type": "estruturado", "user_id": "u", "session_id": f"s-{uuid4().hex[:8]}", **overrides}
    return ChatRequest.model_validate(body)


# -- gabarito ------------------------------------------------------------------------


def test_snapshot_do_texto_montado_numa_chamada_do_regente():
    r = _request(**REGENTE)
    assert structured_input.build_turn_text(r.message, r.events) == SNAPSHOT_REGENTE
    contexto = structured_input.context_for_storage(r.context)
    assert structured_input.render_context(contexto) == SNAPSHOT_CONTEXTO
    assert structured_input.render_context([]) is None and structured_input.render_context(None) is None


def test_so_events_a_fala_e_o_ultimo_contact_e_o_que_vem_depois_fica_depois():
    r = _request(
        events=[
            {"role": "contact", "content": "oi"},
            {"role": "human_agent", "content": "Olá, Maria.", "at": "2026-11-11T09:00:00-03:00"},
            {
                "role": "contact",
                "content": "segue o documento",
                "kind": "document",
                "attachments": [{"filename": "rg.pdf", "mime_type": "application/pdf"}],
            },
            {"role": "system", "content": "Atendimento devolvido ao agente."},
        ]
    )
    texto = structured_input.build_turn_text(r.message, r.events)
    assert texto == (
        "# Conversa desde a sua última resposta\n\n"
        "[Contato]\noi\n\n"
        "[Atendente humano · 2026-11-11T12:00:00Z]\nOlá, Maria.\n\n"
        "# Mensagem nova do contato\n\n"
        "segue o documento\n(anexo: rg.pdf, application/pdf)\n\n"
        "# Depois dessa mensagem\n\n"
        "[Sistema]\nAtendimento devolvido ao agente."
    )
    assert structured_input.pure_message(r.message, r.events) == "segue o documento"
    guardados = structured_input.events_for_storage(r.message, r.events)
    assert [e["speech"] for e in guardados] == [False, False, True, False]
    assert guardados[1]["at"] == "2026-11-11T12:00:00Z", "com fuso, guardado em UTC"


def test_message_diferente_do_ultimo_evento_vem_depois_de_todos():
    r = _request(message="e o boleto?", events=[{"role": "contact", "content": "oi"}])
    texto = structured_input.build_turn_text(r.message, r.events)
    assert texto.endswith("[Contato]\noi\n\n# Mensagem nova do contato\n\ne o boleto?")
    assert [e["speech"] for e in structured_input.events_for_storage(r.message, r.events)] == [False]


def test_mesma_entrada_mesmo_texto():
    a, b = _request(**REGENTE), _request(**json.loads(json.dumps(REGENTE)))
    assert structured_input.build_turn_text(a.message, a.events) == structured_input.build_turn_text(
        b.message, b.events
    )


# -- validação -----------------------------------------------------------------------


def test_sem_message_e_sem_evento_contact_e_recusado():
    with pytest.raises(ValidationError, match="role='contact'"):
        _request(events=[{"role": "agent_sent", "content": "oi"}])
    with pytest.raises(ValidationError):
        _request(context=[{"key": "a", "title": "A", "content": "x"}])
    with pytest.raises(ValidationError):
        _request()


def test_limites_de_blocos_e_eventos():
    bloco = lambda i: {"key": f"b{i}", "title": "B", "content": "x"}  # noqa: E731
    evento = {"role": "contact", "content": "x"}
    _request(message="oi", context=[bloco(i) for i in range(30)], events=[evento] * 200)
    with pytest.raises(ValidationError, match="30"):
        _request(message="oi", context=[bloco(i) for i in range(31)])
    with pytest.raises(ValidationError, match="200"):
        _request(message="oi", events=[evento] * 201)


def test_key_repetida_e_role_invalido():
    with pytest.raises(ValidationError, match="repetida"):
        _request(
            message="oi",
            context=[{"key": "a", "title": "A", "content": "x"}, {"key": "a", "title": "B", "content": "y"}],
        )
    with pytest.raises(ValidationError, match="role"):
        _request(message="oi", events=[{"role": "aluno", "content": "x"}])


def test_anexo_de_evento_nao_aceita_conteudo():
    with pytest.raises(ValidationError, match="attachments do corpo"):
        _request(
            message="oi",
            events=[
                {"role": "contact", "content": "x", "attachments": [{"content_base64": "AAAA", "filename": "a.pdf"}]}
            ],
        )


def test_soma_acima_de_max_input_chars_e_422_no_chat_e_no_stream(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_input_chars", 100)
    r = _request(
        message="oi",
        context=[{"key": "a", "title": "A", "content": "x" * 60}],
        events=[{"role": "contact", "content": "y" * 40}],
    )
    for rota in (chat, chat_stream):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(rota(r))
        assert exc.value.status_code == 422 and "MAX_INPUT_CHARS" in exc.value.detail


def test_422_pela_api_http(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(routes.router)
    cliente = TestClient(app)
    corpo = {"agent_type": "x", "user_id": "u", "session_id": "s", "events": [{"role": "system", "content": "oi"}]}
    resposta = cliente.post("/chat", json=corpo)
    assert resposta.status_code == 422
    assert "role='contact'" in json.dumps(resposta.json(), ensure_ascii=False)


# -- rota e persistência ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    yield
    trace_store.set_trace_store(None)


class AgenteQueGuarda:
    """Guarda a mensagem e o contexto que iria para o prompt de sistema
    (`agents/system_context.py`), lido no momento em que o run começa."""

    def __init__(self) -> None:
        self.mensagens: list[str] = []
        self.contextos: list[str | None] = []

    def arun(self, message, **kwargs):
        self.mensagens.append(message)
        self.contextos.append(current_system_context())

        async def stream():
            yield RunCompletedEvent(run_id=kwargs.get("run_id"), content="Claro, segue.")

        return stream()


@pytest.fixture
def agente(monkeypatch):
    agent = AgenteQueGuarda()
    definicao = {
        "kind": "conversational",
        "name": "Estruturado",
        "prompt_version": 1,
        "agent_version": 1,
        "config_hash": "h",
        "dependency_fields": [],
    }
    monkeypatch.setattr(routes, "get_agent_with_definition", lambda agent_type: (agent, definicao))
    agent.definicao = definicao
    return agent


def _colunas(run_id: str) -> tuple:
    from agent_service.db import get_db

    with get_db().db_engine.connect() as conn:
        return conn.execute(
            sa.select(
                run_store.runs.c.message,
                run_store.runs.c.input_message,
                run_store.runs.c.input_events,
                run_store.runs.c.input_context,
            ).where(run_store.runs.c.run_id == run_id)
        ).one()


def test_regressao_so_message_se_comporta_como_antes(agente):
    mensagem = "  meu wifi caiu\n[ALUNO] texto qualquer  "
    resposta = asyncio.run(chat(_request(message=mensagem)))

    assert agente.mensagens == [mensagem], "o modelo recebe a message byte a byte"
    assert agente.contextos == [None], "e nada a mais no prompt de sistema"
    assert tuple(_colunas(resposta.run_id)) == (mensagem, None, None, None)
    from agent_service.db import get_db

    with get_db().db_engine.connect() as conn:
        nulas = conn.execute(
            sa.text(
                "SELECT input_message IS NULL, input_events IS NULL, input_context IS NULL FROM runs WHERE run_id = :r"
            ),
            {"r": resposta.run_id},
        ).one()
    assert tuple(nulas) == (1, 1, 1), "SQL NULL, não o JSON null: é o filtro de run não estruturado"
    trace = get_run_trace(resposta.run_id)
    assert trace.run.message == mensagem
    assert trace.run.input_message is None and trace.input_events is None and trace.input_context is None
    raiz = next(s for s in trace.spans if s.parent_id is None)
    assert raiz.input == {"message": mensagem, "dependencies": {}}, "a span raiz não ganha campo novo"


def test_chat_estruturado_grava_as_tres_colunas_e_o_texto_montado(agente):
    resposta = asyncio.run(chat(_request(**REGENTE)))

    assert agente.mensagens == [SNAPSHOT_REGENTE] and agente.contextos == [SNAPSHOT_CONTEXTO]
    message, input_message, input_events, input_context = _colunas(resposta.run_id)
    assert message == SNAPSHOT_REGENTE, "runs.message guarda a mensagem do turno, sem o contexto"
    assert input_message == "boa tarde, me envia novamente o comprovante"
    assert input_events == [
        {
            "role": "agent_sent",
            "content": "Maria, recebi seus dados...",
            "at": "2026-11-11T12:36:10Z",
            "kind": "text",
            "attachments": [],
            "speech": False,
        },
        {
            "role": "contact",
            "content": "Para finalizar o diploma. Obrigada.",
            "at": "2026-11-11T12:37:01Z",
            "kind": "text",
            "attachments": [],
            "speech": False,
        },
        {
            "role": "contact",
            "content": "boa tarde, me envia novamente o comprovante",
            "at": "2026-11-11T12:37:15Z",
            "kind": "text",
            "attachments": [],
            "speech": True,
        },
    ]
    assert input_context[1] == {
        "key": "dados_aluno",
        "title": "Dados do aluno",
        "content": "Nome: Maria\nCurso: Pedagogia",
        "volatile": False,
        "hash": structured_input.content_hash("Nome: Maria\nCurso: Pedagogia"),
    }
    assert input_context[4]["volatile"] is True and len(input_context[4]["hash"]) == 64

    trace = get_run_trace(resposta.run_id)
    assert trace.run.input_message == input_message
    assert trace.input_events == input_events and trace.input_context == input_context
    # A listagem traz a fala, mas não carrega eventos e blocos.
    pagina = run_store.DbTraceStore().list_runs(trace_store.RunQuery(session_id=trace.run.session_id))
    assert [r.input_message for r in pagina.items] == [input_message]


def test_so_context_grava_eventos_vazios_e_nao_nulos(agente):
    resposta = asyncio.run(chat(_request(message="oi", context=[{"key": "a", "title": "A", "content": "x"}])))
    _, input_message, input_events, input_context = _colunas(resposta.run_id)
    assert (input_message, input_events) == ("oi", [])
    assert [b["key"] for b in input_context] == ["a"]


def test_stream_estruturado_manda_o_texto_montado(agente):
    async def consumir():
        response = await chat_stream(_request(**REGENTE))
        return [chunk async for chunk in response.body_iterator]

    eventos = [c for c in asyncio.run(consumir()) if isinstance(c, dict)]
    assert eventos[0]["event"] == "run" and eventos[-1]["event"] == "done"
    assert agente.mensagens == [SNAPSHOT_REGENTE] and agente.contextos == [SNAPSHOT_CONTEXTO]
    run_id = json.loads(eventos[0]["data"])["run_id"]
    assert _colunas(run_id)[1] == "boa tarde, me envia novamente o comprovante"


def test_procedural_recusa_entrada_estruturada_com_422(agente):
    agente.definicao["kind"] = "procedural"
    for rota in (chat, chat_stream):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(rota(_request(**REGENTE)))
        assert exc.value.status_code == 422 and "procedural" in exc.value.detail


def test_hash_de_idempotencia_do_legado_nao_muda():
    r = _request(message="oi")
    corpo_antigo = {k: v for k, v in r.model_dump(mode="json").items() if k not in ("events", "context")}
    capturado = {}

    def reserve(endpoint, agent_type, key, body_hash):
        capturado["hash"] = body_hash
        raise idempotency.IdempotencyError(422, "parar aqui")

    import agent_service.api.idempotency as modulo

    original = modulo.reserve
    modulo.reserve = reserve
    try:
        with pytest.raises(HTTPException):
            asyncio.run(routes._idempotent("chat", r, "chave-1", lambda: None))
    finally:
        modulo.reserve = original
    assert capturado["hash"] == idempotency.request_hash(corpo_antigo)


# -- contrato de integração -------------------------------------------------------------


def test_contrato_e_openapi_descrevem_os_campos_novos(monkeypatch):
    from agent_service.api.agents_routes import AgentDefinitionIn, create_agent, delete_agent
    from agent_service.api.integration_routes import CHAT_FIELDS, get_integration_contract

    campos = {f.name: f for f in CHAT_FIELDS}
    assert campos["message"].required is False
    assert {"events", "context"} <= set(campos)

    nome = f"estr-{uuid4().hex[:6]}"
    create_agent(AgentDefinitionIn(agent_type=nome, name="E", instructions=["Ajude."]))
    try:
        contrato = get_integration_contract(nome, base_url="https://api.exemplo.com")
        exemplo = contrato.structured_request_example
        assert exemplo["agent_type"] == nome and exemplo["events"] and exemplo["context"]
        ChatRequest.model_validate(exemplo)  # o exemplo é uma chamada válida

        from fastapi import FastAPI

        from agent_service.api.integration_routes import agent_openapi

        app = FastAPI()
        app.include_router(routes.router)
        from agent_service.agents.store import get_definition

        doc = agent_openapi(app.openapi(), get_definition(nome), "https://api.exemplo.com", False)
        schemas = doc["components"]["schemas"]
        pedido = next(v for k, v in schemas.items() if k.endswith("ChatRequest"))
        assert "message" not in (pedido.get("required") or [])
        assert {"events", "context"} <= set(pedido["properties"])
        assert "role" in schemas["InputEvent"]["properties"] and "volatile" in schemas["ContextBlock"]["properties"]
        assert "dependencies" in pedido["properties"]["context"]["description"]
    finally:
        delete_agent(nome)


# -- migração ---------------------------------------------------------------------------


def test_migracao_0016_sobe_e_desce_sem_perder_runs(tmp_path):
    from alembic import command

    from agent_service.migrations import _config

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'estruturada.db'}")
    config = _config()

    def colunas() -> set[str]:
        return {c["name"] for c in sa.inspect(engine).get_columns("runs")}

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0015")
        conn.execute(
            sa.text(
                "INSERT INTO runs (run_id, trace_id, tenant_id, agent_type, status, message, input_tokens, output_tokens, "
                "total_tokens, started_at) VALUES ('r1', 't1', 'default', 'r6', 'success', 'texto antigo', 0, 0, 0, '2026-11-01')"
            )
        )
        command.upgrade(config, "0016")
    assert {"input_message", "input_events", "input_context"} <= colunas()
    with engine.connect() as conn:
        antigo = conn.execute(sa.text("SELECT message, input_message, input_events FROM runs WHERE run_id='r1'")).one()
    assert tuple(antigo) == ("texto antigo", None, None), "sem retrocarga: o run antigo fica não estruturado"

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0015")
    assert not {"input_message", "input_events", "input_context"} & colunas()
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT message FROM runs WHERE run_id='r1'")).scalar_one() == "texto antigo"
        indices = {i["name"] for i in sa.inspect(engine).get_indexes("runs")}
    assert "ix_runs_complexity" in indices, "o downgrade recria a tabela com os índices"

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    assert "input_context" in colunas()
