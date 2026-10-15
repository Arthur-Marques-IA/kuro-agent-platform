"""`Idempotency-Key` em /chat e /analyze: repetir com a mesma chave devolve a resposta
guardada em vez de rodar o agente (e as tools dele) de novo."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from agno.run.agent import RunCompletedEvent, RunErrorEvent
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import insert

from agent_service.api import idempotency, routes
from agent_service.api.routes import AnalyzeRequest, ChatRequest, analyze, chat, chat_stream
from agent_service.db import get_db
from agent_service.observability import trace_store, tracing
from agent_service.observability.tracing import RunContext
from agent_service.tools.context import dependencies_scope, get_idempotency_key


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    yield
    trace_store.set_trace_store(None)


class AgenteContado:
    """Conta as execuções e guarda a chave que as tools enxergariam."""

    def __init__(self, content="feito", *, falhar=False, demora=0.0):
        self.content, self.falhar, self.demora = content, falhar, demora
        self.execucoes = 0
        self.chaves: list[str | None] = []

    def arun(self, message, **kwargs):
        async def stream():
            self.execucoes += 1
            self.chaves.append(get_idempotency_key())
            if self.demora:
                await asyncio.sleep(self.demora)
            if self.falhar:
                yield RunErrorEvent(run_id=kwargs.get("run_id"), content="o provedor caiu")
                return
            yield RunCompletedEvent(run_id=kwargs.get("run_id"), content=self.content)

        return stream()


def _usar(monkeypatch, agente) -> None:
    def resolve(request, endpoint):
        run = RunContext(endpoint=endpoint, agent_type=request.agent_type, agent_name="t", prompt_version=1,
                         user_id=request.user_id, session_id=request.session_id, message=request.message)
        return agente, run

    monkeypatch.setattr(routes, "_resolve", resolve)


def _pedido(**extra) -> ChatRequest:
    return ChatRequest(agent_type=f"agente-{uuid4().hex[:8]}", user_id="u", session_id="s", message="abre o chamado",
                       **extra)


def test_repetir_com_a_mesma_chave_devolve_a_resposta_guardada(monkeypatch):
    agente = AgenteContado()
    _usar(monkeypatch, agente)
    pedido, chave = _pedido(), str(uuid4())

    primeira = asyncio.run(chat(pedido, chave))
    repetida = asyncio.run(chat(pedido, chave))

    assert agente.execucoes == 1, "a repetição não pode rodar o agente (nem as tools) de novo"
    assert repetida.headers[idempotency.REPLAYED_HEADER] == "true"
    assert json.loads(repetida.body) == primeira.model_dump(mode="json")
    # Outra chave é outra operação.
    asyncio.run(chat(pedido, str(uuid4())))
    assert agente.execucoes == 2


def test_a_mesma_chave_com_outro_corpo_e_422(monkeypatch):
    _usar(monkeypatch, AgenteContado())
    pedido, chave = _pedido(), str(uuid4())
    asyncio.run(chat(pedido, chave))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat(pedido.model_copy(update={"message": "outra coisa"}), chave))
    assert exc.value.status_code == 422 and "outro corpo" in exc.value.detail


def test_duas_chamadas_simultaneas_com_a_mesma_chave_rodam_uma_vez(monkeypatch):
    agente = AgenteContado(demora=0.2)
    _usar(monkeypatch, agente)
    pedido, chave = _pedido(), str(uuid4())

    async def duas():
        return await asyncio.gather(chat(pedido, chave), chat(pedido, chave), return_exceptions=True)

    resultados = asyncio.run(duas())
    erros = [r for r in resultados if isinstance(r, HTTPException)]
    assert agente.execucoes == 1
    assert len(erros) == 1 and erros[0].status_code == 409 and "em andamento" in erros[0].detail


def test_falha_libera_a_chave_para_tentar_de_novo(monkeypatch):
    agente = AgenteContado(falhar=True)
    _usar(monkeypatch, agente)
    pedido, chave = _pedido(), str(uuid4())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat(pedido, chave))
    assert exc.value.status_code == 502

    agente.falhar = False
    resposta = asyncio.run(chat(pedido, chave))
    assert agente.execucoes == 2 and resposta.content == "feito"


def test_as_tools_recebem_a_chave_so_durante_o_run(monkeypatch):
    agente = AgenteContado()
    _usar(monkeypatch, agente)
    asyncio.run(chat(_pedido(), "pedido-123"))
    asyncio.run(chat(_pedido()))
    assert agente.chaves == ["pedido-123", None]
    assert get_idempotency_key() is None


def test_fora_de_um_run_vale_a_chave_das_dependencies():
    """A etapa `action` de um procedural roda com a chave dela, não com a do request."""
    idempotency_key_do_request = "do-request"
    from agent_service.tools.context import set_idempotency_key

    set_idempotency_key(idempotency_key_do_request)
    try:
        with dependencies_scope({"idempotency_key": "abc:abrir:123"}):
            assert get_idempotency_key() is None
        assert get_idempotency_key() == idempotency_key_do_request
    finally:
        set_idempotency_key(None)


def test_a_chave_do_servidor_vence_a_das_dependencies(monkeypatch):
    from agent_service.tools.api_tool import build_api_function, validate_api_config
    from agent_service.tools.context import set_dependencies, set_idempotency_key

    from tests.test_tool_param_sources import RedeFalsa

    config = validate_api_config({
        "method": "POST", "url": "https://x.test/chamados",
        "parameters": [{"name": "Idempotency-Key", "type": "string", "location": "header",
                        "source": "dependency", "dependency": "idempotency_key"}],
    })
    fn = build_api_function(tool_name="chamado", description=None, config=config)

    async def chamar():
        set_dependencies({"idempotency_key": "inventada-por-quem-chamou"})
        set_idempotency_key("do-header")
        await fn.entrypoint()

    with RedeFalsa({"ok": True}) as rede:
        asyncio.run(chamar())
    assert rede.requests[-1].headers["Idempotency-Key"] == "do-header"


def test_chave_fica_no_metadata_do_run(monkeypatch):
    agente = AgenteContado()
    runs: list[RunContext] = []

    def resolve(request, endpoint):
        run = RunContext(endpoint=endpoint, agent_type=request.agent_type, agent_name="t", prompt_version=1,
                         user_id="u", session_id="s", message="m")
        runs.append(run)
        return agente, run

    monkeypatch.setattr(routes, "_resolve", resolve)
    asyncio.run(chat(_pedido(), "pedido-9"))
    assert runs[0].metadata["idempotency_key"] == "pedido-9"


def test_stream_recusa_a_chave_em_vez_de_ignorar():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_stream(_pedido(), "pedido-1"))
    assert exc.value.status_code == 422 and "/chat" in exc.value.detail


@pytest.mark.parametrize("chave", ["", "   ", "x" * 256, "quebra\nde linha"])
def test_chave_invalida_e_422(monkeypatch, chave):
    _usar(monkeypatch, AgenteContado())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat(_pedido(), chave))
    assert exc.value.status_code == 422


def test_reserva_abandonada_por_um_processo_que_morreu_e_retomada():
    """Sem isso, um crash no meio deixaria a chave em 409 até expirar."""
    velha = datetime.now(timezone.utc) - idempotency.stale_after() - timedelta(seconds=1)
    agent_type, chave = f"agente-{uuid4().hex[:8]}", "abandonada"
    with get_db().db_engine.begin() as conn:
        conn.execute(insert(idempotency.idempotency_keys).values(
            id=uuid4().hex, endpoint="chat", agent_type=agent_type, key=chave, request_hash="h",
            status="in_progress", created_at=velha, updated_at=velha,
        ))
    reserva = idempotency.reserve("chat", agent_type, chave, "h")
    assert reserva.id is not None and reserva.replay is None


def test_chave_vencida_roda_de_novo(monkeypatch):
    monkeypatch.setattr(idempotency.get_settings(), "idempotency_ttl_hours", 0)
    agente = AgenteContado()
    _usar(monkeypatch, agente)
    pedido, chave = _pedido(), str(uuid4())
    asyncio.run(chat(pedido, chave))
    asyncio.run(chat(pedido, chave))
    assert agente.execucoes == 2


class Saida(BaseModel):
    acao: str


def test_analyze_tambem_e_idempotente(monkeypatch):
    agente = AgenteContado(content=Saida(acao="escalar"))
    definicao = {"kind": "analysis", "name": "Extrator", "prompt_version": 1, "agent_version": 2, "config_hash": "h"}
    monkeypatch.setattr(routes, "get_agent_with_definition", lambda agent_type: (agente, definicao))
    pedido, chave = AnalyzeRequest(agent_type=f"extrator-{uuid4().hex[:6]}", document="contrato"), str(uuid4())

    primeira = asyncio.run(analyze(pedido, chave))
    repetida = asyncio.run(analyze(pedido, chave))

    assert agente.execucoes == 1 and agente.chaves == [chave]
    assert json.loads(repetida.body)["result"] == {"acao": "escalar"} == primeira.result
    assert json.loads(repetida.body)["run_id"] == primeira.run_id
