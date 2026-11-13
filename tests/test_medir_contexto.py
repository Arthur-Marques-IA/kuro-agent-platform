"""A medição de `scripts/medir_contexto.py`, como aceite: com o contexto como estado da
sessão, o prompt de cada turno traz o contexto vigente uma vez, e só ele, mande a
integração o `context` só quando muda ou em todo turno."""

import importlib.util
from pathlib import Path

import pytest

from agent_service.agents import session_context
from agent_service.observability import trace_store, tracing

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "medir_contexto.py"


def _medicao():
    spec = importlib.util.spec_from_file_location("medir_contexto", _SCRIPT)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    session_context.forget()
    yield
    trace_store.set_trace_store(None)


def test_o_contexto_aparece_uma_vez_por_turno_e_o_prompt_nao_cresce_com_ele(tmp_path):
    medicao = _medicao()
    mudancas = medicao.medir("mudancas", tmp_path)
    todo_turno = medicao.medir("todo_turno", tmp_path)

    assert len(mudancas) == medicao.TURNOS
    for turno in mudancas + todo_turno:
        assert turno["copias"] == 1 and turno["copias_antigas"] == 0, turno
        # O histórico é só a conversa: algumas centenas de caracteres por turno, nunca o contexto.
        assert turno["historico"] < 300 * turno["turno"], turno
    # Mandar o contexto em todo turno custa o mesmo que mandá-lo só quando muda.
    assert [t["total"] for t in mudancas] == [t["total"] for t in todo_turno]
    # Um contexto de ~7 mil caracteres, e o prompt do 12º turno fica perto disso.
    assert mudancas[-1]["total"] < mudancas[0]["total"] + 3000
