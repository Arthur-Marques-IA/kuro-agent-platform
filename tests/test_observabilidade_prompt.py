"""Composição do prompt, uma span por chamada ao modelo e custo com cache.

Um agente Agno de verdade (o `KuroAgent`) com o modelo falso de `test_contexto_da_sessao`,
passando pela rota `/chat` e pelo trace store: o que se confere é o que fica gravado.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from agno.db.sqlite import SqliteDb
from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse

from agent_service.agents import session_context
from agent_service.api import routes
from agent_service.api.routes import chat
from agent_service.models import pricing
from agent_service.observability import prompt_composition, run_store, trace_store, tracing
from agent_service.observability.run_store import DbTraceStore
from tests.test_contexto_da_sessao import (  # noqa: F401  (fixtures e helpers do modelo falso)
    ModeloQueGuarda,
    _montar,
    _pedido,
)


class ModeloComCache(ModeloQueGuarda):
    """Como o modelo falso, mas diz que 6 dos 10 tokens de entrada vieram do cache."""

    def _resposta(self, messages: list[Message]) -> ModelResponse:
        response = super()._resposta(messages)
        response.response_usage = MessageMetrics(input_tokens=10, output_tokens=5, total_tokens=15, cache_read_tokens=6)
        return response


@pytest.fixture
def banco_do_agno(tmp_path) -> SqliteDb:
    return SqliteDb(db_url=f"sqlite:///{(tmp_path / 'agno.db').as_posix()}")


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    session_context.forget()
    yield
    trace_store.set_trace_store(None)
    session_context.forget()


def _rota(monkeypatch, agent) -> None:
    definicao = {
        "kind": "conversational",
        "name": "Contexto",
        "prompt_version": 1,
        "agent_version": 1,
        "config_hash": "h",
        "dependency_fields": [],
    }
    monkeypatch.setattr(routes, "get_agent_with_definition", lambda agent_type: (agent, definicao))


def _turno(sid: str, **campos: Any):
    return asyncio.run(chat(_pedido(sid, **campos)))


def _trace(run_id: str):
    trace = DbTraceStore().get_run(run_id)
    assert trace is not None
    return trace


def _consultar_boleto() -> str:
    """Consulta o boleto do aluno."""
    return json.dumps({"boleto": "123"})


# -- composição do prompt ----------------------------------------------------------------


def test_sem_contexto_nem_historico_so_instrucoes_e_a_fala(monkeypatch, banco_do_agno):
    modelo = ModeloQueGuarda(id="m")
    _rota(monkeypatch, _montar(monkeypatch, banco_do_agno, modelo))

    resposta = _turno(f"s-{uuid4().hex[:8]}", message="oi tudo bem")

    parts = _trace(resposta.run_id).prompt_composition["parts"]
    assert parts["instructions"] > 0 and parts["turn"] == len("oi tudo bem")
    for nome in ("session_context", "memories", "session_summary", "history", "history_tools", "dependencies", "other"):
        assert parts[nome] == 0, nome
    comp = _trace(resposta.run_id).prompt_composition
    assert comp["chars"] == sum(parts.values()) == _trace(resposta.run_id).run.prompt_chars
    assert comp["tokens_estimate"] == round(comp["chars"] / 4) and comp["tokens_estimate_method"] == "chars/4"
    # As partes batem com o que o modelo recebeu na primeira chamada.
    enviado = sum(len(str(m.content or "")) for m in modelo.chamadas[0])
    assert comp["chars"] == enviado


def test_contexto_da_sessao_e_historico_aparecem_nas_partes(monkeypatch, banco_do_agno):
    modelo = ModeloQueGuarda(id="m")
    _rota(monkeypatch, _montar(monkeypatch, banco_do_agno, modelo))
    sid = f"s-{uuid4().hex[:8]}"
    contexto = [{"key": "dados", "title": "Dados", "content": "Nome: Maria\nCurso: Pedagogia"}]

    primeira = _turno(sid, message="oi", context=contexto)
    segunda = _turno(sid, message="e a segunda via?")

    um, dois = _trace(primeira.run_id).prompt_composition, _trace(segunda.run_id).prompt_composition
    assert um["parts"]["session_context"] > 0 and um["parts"]["history"] == 0
    assert dois["parts"]["session_context"] == um["parts"]["session_context"]
    historico = dois["parts"]["history"]
    assert historico > 0  # a fala do turno anterior e a resposta
    assert dois["parts"]["history_tools"] == 0
    assert _trace(segunda.run_id).run.prompt_chars == dois["chars"]
    assert prompt_composition.history_chars(dois) == historico


def test_tool_do_historico_conta_a_parte(monkeypatch, banco_do_agno):
    modelo = ModeloQueGuarda(id="m", tool="_consultar_boleto")
    agent = _montar(monkeypatch, banco_do_agno, modelo, tools=[_consultar_boleto])
    _rota(monkeypatch, agent)
    sid = f"s-{uuid4().hex[:8]}"

    primeiro = _turno(sid, message="um")
    segundo = _turno(sid, message="dois")

    # A composição é a da PRIMEIRA chamada do run: a tool do próprio turno não conta.
    assert _trace(primeiro.run_id).prompt_composition["parts"]["history_tools"] == 0
    assert _trace(segundo.run_id).prompt_composition["parts"]["history_tools"] > 0


def test_medicao_que_falha_nao_derruba_o_run(monkeypatch, banco_do_agno):
    _rota(monkeypatch, _montar(monkeypatch, banco_do_agno, ModeloQueGuarda(id="m")))

    def quebra(*_a, **_k):
        raise RuntimeError("falhou")

    monkeypatch.setattr(prompt_composition, "_measure", quebra)

    resposta = _turno(f"s-{uuid4().hex[:8]}", message="oi")

    assert resposta.content and _trace(resposta.run_id).prompt_composition is None


def test_measure_separa_memorias_e_resumo_do_sistema():
    mensagens = [
        Message(
            role="system",
            content="Instruções.\n<memories_from_previous_interactions>\n- gosta de café\n</memories_from_previous_interactions>"
            "\n<summary_of_previous_interactions>\nresumo\n</summary_of_previous_interactions>\nCONTEXTO",
        ),
        Message(role="user", content="antiga", from_history=True),
        Message(role="user", content="nova\n\n<additional context>\n{'a': 1}\n</additional context>"),
    ]

    comp = prompt_composition.measure(mensagens, system_context="CONTEXTO", attachments=2)

    assert comp is not None and comp["attachments"] == 2
    parts = comp["parts"]
    assert parts["session_context"] == len("CONTEXTO")
    assert parts["memories"] > 0 and parts["session_summary"] > 0
    assert parts["history"] == len("antiga") and parts["dependencies"] > 0 and parts["turn"] > 0
    assert prompt_composition.measure([]) is None


# -- uma span por chamada ao modelo --------------------------------------------------------


def test_run_com_uma_tool_tem_duas_geracoes_na_ordem_certa_e_os_totais_nao_mudam(monkeypatch, banco_do_agno):
    modelo = ModeloQueGuarda(id="m", tool="_consultar_boleto")
    _rota(monkeypatch, _montar(monkeypatch, banco_do_agno, modelo, tools=[_consultar_boleto]))

    resposta = _turno(f"s-{uuid4().hex[:8]}", message="meu boleto")

    trace = _trace(resposta.run_id)
    filhos = [s for s in trace.spans if s.parent_id is not None]
    assert [s.type for s in filhos] == ["GENERATION", "TOOL", "GENERATION"]
    primeira, _, segunda = filhos
    assert [primeira.metadata["call"], segunda.metadata["call"]] == [1, 2]
    for geracao in (primeira, segunda):
        assert geracao.ended_at is not None and geracao.latency_ms is not None and geracao.latency_ms >= 0
        assert (geracao.input_tokens, geracao.output_tokens) == (10, 5)
    # Os totais do run são os de sempre (das métricas agregadas), e fecham com a soma das chamadas.
    assert (trace.run.input_tokens, trace.run.output_tokens, trace.run.total_tokens) == (20, 10, 30)
    assert sum(s.input_tokens for s in filhos if s.type == "GENERATION") == trace.run.input_tokens


def test_se_as_chamadas_nao_fecham_com_o_agregado_fica_o_agregado():
    metricas = SimpleNamespace(
        details={
            "model": [
                SimpleNamespace(provider="X", id="m", input_tokens=30, output_tokens=5, total_tokens=35, cost=None)
            ]
        }
    )
    chamada = run_store.SpanRecord(
        type="GENERATION",
        name="X.model",
        started_at=tracing.datetime.now(tracing.timezone.utc),
        input_tokens=10,
        output_tokens=5,
    )

    spans = tracing._model_spans(metricas, chamada.started_at, [chamada])

    assert len(spans) == 1 and spans[0] is not chamada and spans[0].input_tokens == 30


# -- cache ---------------------------------------------------------------------------------


def test_tokens_de_cache_viram_campos_do_run_e_da_lista(monkeypatch, banco_do_agno):
    modelo = ModeloComCache(id="gemini-2.5-flash")
    modelo.provider = "Google"
    _rota(monkeypatch, _montar(monkeypatch, banco_do_agno, modelo))

    resposta = _turno(f"s-{uuid4().hex[:8]}", message="oi")

    trace = _trace(resposta.run_id)
    assert trace.run.cache_read_tokens == 6 and trace.run.cache_write_tokens == 0
    geracao = next(s for s in trace.spans if s.type == "GENERATION")
    assert geracao.metadata["cache_read_tokens"] == 6
    esperado = pricing.estimate_cost(
        provider="Google", model_id="gemini-2.5-flash", input_tokens=10, output_tokens=5, cache_read_tokens=6
    )
    assert geracao.cost_usd == pytest.approx(esperado) and trace.run.cost_usd == pytest.approx(esperado)
    assert esperado is not None and esperado < pricing.estimate_cost(
        provider="Google", model_id="gemini-2.5-flash", input_tokens=10, output_tokens=5
    )  # type: ignore[operator]
    listado = next(r for r in DbTraceStore().list_runs(trace_store.RunQuery(session_id=trace.run.session_id)).items)
    assert listado.cache_read_tokens == 6 and listado.prompt_chars == trace.run.prompt_chars


def test_custo_desconta_o_cache_no_gemini_e_soma_a_parte_no_claude():
    gemini = dict(provider="Google", model_id="gemini-2.5-flash", output_tokens=0)
    # 1M de entrada, 800k do cache: 200k a 0,30 + 800k a 0,03 = 0,084
    assert pricing.estimate_cost(**gemini, input_tokens=1_000_000, cache_read_tokens=800_000) == pytest.approx(0.084)
    claude = dict(provider="Anthropic", model_id="claude-sonnet-4-5", output_tokens=0)
    # No Claude a entrada já exclui o cache: 100k a 3 + 900k lidos a 0,30 + 50k escritos a 3,75
    custo = pricing.estimate_cost(**claude, input_tokens=100_000, cache_read_tokens=900_000, cache_write_tokens=50_000)
    assert custo == pytest.approx(0.3 + 0.27 + 0.1875)


def test_sem_preco_de_cache_usa_o_de_lista():
    lista = pricing.estimate_cost(
        provider="Google", model_id="gemini-3.5-flash", input_tokens=1_000_000, output_tokens=0
    )
    com_cache = pricing.estimate_cost(
        provider="Google",
        model_id="gemini-3.5-flash",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=900_000,
    )
    assert com_cache == lista
    assert "gemini-3.5-flash" not in pricing.CACHE_PRICES
    assert (
        pricing.estimate_cost(
            provider="X", model_id="desconhecido", input_tokens=1, output_tokens=1, cache_read_tokens=1
        )
        is None
    )
