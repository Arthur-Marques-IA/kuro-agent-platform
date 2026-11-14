"""As `dependencies` do run no prompt de sistema, uma vez por chamada, com o Agno de verdade.

Mesmo desenho de `test_contexto_da_sessao.py`: agente real (`build_agent`), histórico numa
sessão do Agno em SQLite e um modelo falso que guarda as mensagens de cada chamada.

- cada turno vê só as dependencies que mandou, uma vez, no prompt de sistema, e nenhuma
  mensagem do histórico as contém (o Agno punha `<additional context>` na mensagem do
  usuário, gravava e reenviava por `num_history_runs` turnos);
- sem dependencies, o prompt é o do Agno sem nada do Kuro;
- contexto da sessão e dependencies convivem (contexto primeiro);
- conversas concorrentes não misturam; as tools e as variáveis `{campo}` das instruções
  continuam recebendo os valores; o Mem0 continua vendo as memórias.
"""

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb

from agent_service.agents import base, session_context
from agent_service.agents.base import KuroAgent, build_agent
from agent_service.agents.system_context import render_dependencies, set_system_dependencies
from agent_service.api import routes
from agent_service.api.routes import ChatRequest, chat
from agent_service.observability import trace_store, tracing
from agent_service.tools.context import get_dependencies
from tests.test_contexto_da_sessao import (
    DIVIDA_V1,
    INSTRUCOES,
    ModeloQueGuarda,
    _contexto,
    _fora_do_sistema,
    _montar,
    _ocorrencias,
    _sistema,
)


@pytest.fixture
def banco_do_agno(tmp_path: Path) -> SqliteDb:
    return SqliteDb(db_url=f"sqlite:///{(tmp_path / 'agno.db').as_posix()}")


@pytest.fixture
def modelo() -> ModeloQueGuarda:
    return ModeloQueGuarda(id="modelo-que-guarda")


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    session_context.forget()
    yield
    trace_store.set_trace_store(None)
    session_context.forget()


def _registrar(monkeypatch, agent: KuroAgent, kind: str = "conversational") -> None:
    definicao = {
        "kind": kind,
        "name": "Contexto",
        "prompt_version": 1,
        "agent_version": 1,
        "config_hash": "h",
        "dependency_fields": [],
    }
    monkeypatch.setattr(routes, "get_agent_with_definition", lambda agent_type: (agent, definicao))


@pytest.fixture
def agente(monkeypatch, banco_do_agno, modelo) -> KuroAgent:
    agent = _montar(monkeypatch, banco_do_agno, modelo)
    _registrar(monkeypatch, agent)
    return agent


def _pedido(session_id: str, **campos: Any) -> ChatRequest:
    return ChatRequest.model_validate({"agent_type": "contexto", "user_id": "u", "session_id": session_id, **campos})


def _turno(session_id: str, **campos: Any):
    return asyncio.run(chat(_pedido(session_id, **campos)))


def _bloco(deps: dict[str, Any]) -> str:
    return "<additional context>\n" + json.dumps(deps, indent=2, ensure_ascii=False) + "\n</additional context>"


def test_cada_turno_ve_so_as_suas_dependencies_uma_vez_no_sistema(agente, modelo):
    sid = f"s-{uuid4().hex[:8]}"
    cpfs = ["11111111111", "22222222222", "33333333333"]
    for i, cpf in enumerate(cpfs):
        _turno(sid, message=f"fala {i}", dependencies={"cpf": cpf, "nome": "Maria"})

    assert len(modelo.chamadas) == 3
    for chamada, cpf in zip(modelo.chamadas, cpfs, strict=True):
        assert _sistema(chamada).count(cpf) == 1
        assert _ocorrencias(chamada, _bloco({"cpf": cpf, "nome": "Maria"})) == (1, 0)
        for outro in cpfs:
            if outro != cpf:
                assert _ocorrencias(chamada, outro) == (0, 0), "o valor de outro turno não aparece"
        assert all("<additional context>" not in t for t in _fora_do_sistema(chamada))
    # O histórico do terceiro turno traz as falas antigas, limpas.
    usuario = [str(m.content) for m in modelo.chamadas[2] if m.role == "user"]
    assert usuario == ["fala 0", "fala 1", "fala 2"]


def test_sem_dependencies_o_prompt_e_o_do_agno_puro(agente, modelo, tmp_path):
    sid = f"s-{uuid4().hex[:8]}"
    falas = ["oi", "e agora?"]
    for fala in falas:
        _turno(sid, message=fala)
    puro_modelo = ModeloQueGuarda(id="modelo-que-guarda")
    puro = Agent(
        id="contexto",
        name="Contexto",
        model=puro_modelo,
        db=SqliteDb(db_url=f"sqlite:///{(tmp_path / 'puro.db').as_posix()}"),
        instructions=INSTRUCOES,
        add_history_to_context=True,
        num_history_runs=10,
        markdown=True,
    )

    async def rodar() -> None:
        for fala in falas:
            async for _ in puro.arun(fala, user_id="u", session_id=sid, stream=True, stream_events=True):
                pass

    asyncio.run(rodar())
    assert [[(m.role, m.content) for m in c] for c in modelo.chamadas] == [
        [(m.role, m.content) for m in c] for c in puro_modelo.chamadas
    ]


def test_dependencies_so_valem_no_turno_que_as_mandou(agente, modelo):
    sid = f"s-{uuid4().hex[:8]}"
    _turno(sid, message="oi", dependencies={"cpf": "11111111111"})
    _turno(sid, message="sem nada")
    assert "<additional context>" not in _sistema(modelo.chamadas[1])
    assert _ocorrencias(modelo.chamadas[1], "11111111111") == (0, 0)


def test_contexto_da_sessao_e_dependencies_juntos(agente, modelo):
    sid = f"s-{uuid4().hex[:8]}"
    _turno(sid, message="oi", context=_contexto(DIVIDA_V1), dependencies={"cpf": "11111111111"})
    _turno(sid, message="de novo", dependencies={"cpf": "22222222222"})
    for chamada, cpf in zip(modelo.chamadas, ["11111111111", "22222222222"], strict=True):
        sistema = _sistema(chamada)
        assert sistema.index("Responda curto.") < sistema.index("# Contexto") < sistema.index("<additional context>")
        assert _ocorrencias(chamada, DIVIDA_V1) == (1, 0)
        assert _ocorrencias(chamada, cpf) == (1, 0)


def test_conteudo_arbitrario_nao_vira_template(agente, modelo):
    estranho = "preço: ${valor} {cpf} {} $$ **negrito** `código` \n- item"
    _turno(f"s-{uuid4().hex[:8]}", message="oi", dependencies={"texto": estranho, "cpf": "1"})
    assert _bloco({"texto": estranho, "cpf": "1"}) in _sistema(modelo.chamadas[0])


def test_valor_que_nao_vira_json_vira_texto():
    class Estranho:
        def __str__(self) -> str:
            return "estranho"

    assert '"x": "estranho"' in render_dependencies({"x": Estranho(), "y": 1})
    assert render_dependencies({"x": {1, 2}}).startswith("<additional context>")


def test_duas_conversas_ao_mesmo_tempo_nao_misturam_dependencies(agente, modelo):
    sessoes = {f"s-{uuid4().hex[:8]}-{i}": f"cpf-exclusivo-{i}" for i in range(4)}

    async def conversa(sid: str, cpf: str) -> None:
        for fala in (f"fala de {sid}", f"de novo {sid}"):
            await chat(_pedido(sid, message=fala, dependencies={"cpf": cpf}))

    async def todas() -> None:
        await asyncio.gather(*(conversa(sid, cpf) for sid, cpf in sessoes.items()))

    asyncio.run(todas())
    assert modelo.pico >= 2
    for chamada in modelo.chamadas:
        sid = next(s for s in sessoes if s in _fora_do_sistema(chamada)[-1])
        for outro, cpf in sessoes.items():
            assert _sistema(chamada).count(cpf) == (1 if outro == sid else 0)
    assert agente.additional_context is None


def test_copia_do_agente_nao_leva_as_dependencies_do_run(agente):
    set_system_dependencies({"cpf": "1"})
    try:
        assert "<additional context>" in (agente.additional_context or "")
        copia = agente.deep_copy()
    finally:
        set_system_dependencies(None)
    assert copia.additional_context is None


def test_a_tool_recebe_o_valor_e_as_instrucoes_resolvem_o_campo(monkeypatch, banco_do_agno):
    vistos: list[dict[str, Any]] = []

    def consultar() -> str:
        """Consulta o aluno."""
        vistos.append(get_dependencies())
        return "{}"

    modelo = ModeloQueGuarda(id="modelo-com-tool", tool="consultar")
    monkeypatch.setattr(base, "get_db", lambda: banco_do_agno)
    monkeypatch.setattr(base, "get_model", lambda **_: modelo)
    agent = build_agent(
        agent_id="contexto", name="Contexto", instructions=["Atenda o aluno {nome}."], tools=[consultar]
    )
    _registrar(monkeypatch, agent)
    _turno(f"s-{uuid4().hex[:8]}", message="oi", dependencies={"cpf": "12345678900", "nome": "Maria"})
    assert vistos and vistos[0]["cpf"] == "12345678900" and vistos[0]["nome"] == "Maria"
    primeira = modelo.chamadas[0]
    assert "Atenda o aluno Maria." in _sistema(primeira), "o template das instruções continua resolvido"
    assert _ocorrencias(primeira, "12345678900") == (1, 0)


def test_mem0_continua_vendo_as_memorias(monkeypatch, banco_do_agno):
    from agent_service.memory import mem0_hooks

    class FalsoMem0:
        def search(self, user_id: str, query: str, limit: int) -> list[str]:
            return [f"{user_id} gosta de café"]

        def add(self, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr(mem0_hooks, "_backend", FalsoMem0())
    modelo = ModeloQueGuarda(id="modelo-mem0")
    monkeypatch.setattr(base, "get_db", lambda: banco_do_agno)
    monkeypatch.setattr(base, "get_model", lambda **_: modelo)
    agent = build_agent(agent_id="contexto", name="Contexto", instructions=INSTRUCOES, memory_backend="mem0")
    _registrar(monkeypatch, agent)
    sid = f"s-{uuid4().hex[:8]}"
    _turno(sid, message="oi", dependencies={"cpf": "1"})
    _turno(sid, message="sem dependencies")  # só as memórias do Mem0
    for chamada, esperado_cpf in zip(modelo.chamadas, (1, 0), strict=True):
        sistema = _sistema(chamada)
        assert sistema.count("u gosta de café") == 1
        assert sistema.count('"cpf"') == esperado_cpf
        assert _ocorrencias(chamada, '"mem0_memories"') == (1, 0)
        assert "<additional context>" not in "".join(_fora_do_sistema(chamada))
