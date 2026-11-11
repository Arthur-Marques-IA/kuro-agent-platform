"""Fila própria da gravação de trace (`run_store.TraceWriter`)."""

import asyncio
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_service.observability import run_store
from agent_service.observability.run_store import TraceWriter

_SUBMIT_REAL = run_store._submit  # antes do `gravacao_sincrona` do conftest trocar


def _bloqueio(writer: TraceWriter) -> tuple[threading.Event, threading.Event]:
    """Põe na fila um item que segura a thread até `soltar.set()`."""
    comecou, soltar = threading.Event(), threading.Event()

    def segura() -> None:
        comecou.set()
        soltar.wait(5)

    assert writer.submit(segura)
    assert comecou.wait(5)
    return comecou, soltar


def test_fila_cheia_descarta_e_conta_sem_levantar(caplog):
    writer = TraceWriter(2)
    _, soltar = _bloqueio(writer)
    feitos: list[int] = []
    assert writer.submit(lambda: feitos.append(1))
    assert writer.submit(lambda: feitos.append(2))
    # a thread está ocupada e a fila (2) cheia: descarta na hora, sem bloquear
    assert writer.submit(lambda: feitos.append(3)) is False
    assert writer.submit(lambda: feitos.append(4)) is False
    stats = writer.stats()
    assert stats["dropped"] == 2
    assert stats["pending"] == 3  # o item em gravação + os 2 na fila
    # o aviso sai uma vez só dentro do intervalo (limitação de taxa)
    assert sum("descartada" in r.getMessage() for r in caplog.records) == 1
    soltar.set()
    assert writer.flush(5)
    assert feitos == [1, 2]
    assert writer.stats() | {"max_pending": 0} == {
        "pending": 0, "max_pending": 0, "written": 3, "dropped": 2, "failed": 0
    }
    writer.shutdown(1)


def test_flush_esvazia_e_preserva_a_ordem():
    writer = TraceWriter(500)
    ordem: list[int] = []
    for i in range(200):
        assert writer.submit(lambda i=i: ordem.append(i))
    assert writer.flush(5)
    assert ordem == list(range(200))
    assert writer.stats()["written"] == 200
    assert writer.shutdown(1) == 0


def test_falha_de_gravacao_e_contada_e_nao_propaga(caplog):
    writer = TraceWriter(10)

    def quebra() -> None:
        raise RuntimeError("banco fora")

    quebra.trace_label = "o run r-1"  # type: ignore[attr-defined]
    feitos: list[str] = []
    assert writer.submit(quebra)
    assert writer.submit(lambda: feitos.append("depois"))
    assert writer.flush(5)
    assert feitos == ["depois"]  # a thread sobreviveu à falha
    assert writer.stats()["failed"] == 1
    assert writer.stats()["written"] == 1
    assert any("o run r-1" in r.getMessage() for r in caplog.records)
    writer.run_now(quebra)  # o caminho síncrono também não propaga
    assert writer.stats()["failed"] == 2
    writer.shutdown(1)


def test_encerramento_esvazia_e_para_de_aceitar():
    writer = TraceWriter(10)
    feitos: list[int] = []
    for i in range(5):
        writer.submit(lambda i=i: feitos.append(i))
    assert writer.shutdown(5) == 0
    assert feitos == list(range(5))
    assert writer.submit(lambda: feitos.append(99)) is False
    assert writer.stats()["dropped"] == 1
    assert feitos == list(range(5))


def test_encerramento_com_prazo_conta_o_que_ficou(caplog):
    writer = TraceWriter(10)
    _, soltar = _bloqueio(writer)
    writer.submit(lambda: None)
    writer.submit(lambda: None)
    assert writer.shutdown(0.05) == 3
    assert any("ficaram para trás" in r.getMessage() for r in caplog.records)
    soltar.set()


def test_teto_minimo_e_um():
    """`queue.Queue(maxsize=0)` seria fila sem limite."""
    assert TraceWriter(0).max_pending == 1


def test_submit_no_event_loop_usa_a_fila_e_shutdown_solta_o_writer(monkeypatch):
    # o conftest troca `_submit` por `write_now`; aqui é o caminho real, pela fila
    monkeypatch.setattr(run_store, "_submit", _SUBMIT_REAL)
    monkeypatch.setattr(run_store, "_writer", None)
    feitos: list[str] = []

    async def chama() -> None:
        run_store._submit(lambda: feitos.append(threading.current_thread().name))

    asyncio.run(chama())
    assert run_store.flush(5)
    assert feitos == ["kuro-trace-writer"]
    assert run_store.write_stats()["written"] == 1
    assert run_store.shutdown(1) == 0
    assert run_store._writer is None  # o próximo app no processo cria um writer novo
    assert run_store.write_stats()["written"] == 0
    assert run_store.shutdown(1) == 0  # sem writer, não faz nada


def test_fora_do_event_loop_grava_na_hora(monkeypatch):
    monkeypatch.setattr(run_store, "_submit", _SUBMIT_REAL)
    monkeypatch.setattr(run_store, "_writer", None)
    feitos: list[str] = []
    run_store._submit(lambda: feitos.append(threading.current_thread().name))
    assert feitos == [threading.current_thread().name]


def test_health_expoe_os_contadores_da_fila():
    from agent_service.api.routes import router

    app = FastAPI()
    app.include_router(router)
    resposta = TestClient(app).get("/health")
    assert resposta.status_code == 200
    assert set(resposta.json()["trace_writes"]) == {"pending", "max_pending", "written", "dropped", "failed"}
