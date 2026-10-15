"""Fila de jobs no banco: um job roda uma vez, lease vencido volta, retry com backoff, agendamento."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agent_service.jobs import store
from agent_service.jobs import worker as jobs_worker
from agent_service.jobs.worker import Reschedule, Worker


@pytest.fixture(autouse=True)
def fila_limpa(monkeypatch):
    """Só os handlers e os jobs deste teste."""
    with store._engine().begin() as conn:
        conn.execute(store.update(store.jobs).where(store.jobs.c.status.in_(store.ACTIVE)).values(status="cancelled"))
    monkeypatch.setattr(jobs_worker, "HANDLERS", {})
    monkeypatch.setattr(store.get_settings(), "jobs_backoff_seconds", 10)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def test_dois_workers_um_job_roda_uma_vez_so():
    execucoes: list[str] = []

    @jobs_worker.register("teste.lento")
    async def lento(job):
        execucoes.append(job["id"])
        await asyncio.sleep(0.05)

    ids = [store.enqueue("teste.lento", {"n": n})["id"] for n in range(6)]

    async def disputa():
        workers = [Worker(), Worker()]

        async def drena(w):
            while await w.run_once():
                pass

        await asyncio.gather(*(drena(w) for w in workers for _ in range(3)))

    asyncio.run(disputa())
    assert sorted(execucoes) == sorted(ids)  # todos rodaram, nenhum duas vezes
    assert {store.get_job(i)["status"] for i in ids} == {"succeeded"}


def test_claim_nao_pega_o_que_outro_ja_pegou():
    store.enqueue("teste.x")
    lease = timedelta(seconds=30)
    primeiro = store.claim("w1", ["teste.x"], lease)
    assert primeiro is not None and primeiro["attempts"] == 1
    assert store.claim("w2", ["teste.x"], lease) is None
    assert not store.renew(primeiro["id"], "w2", lease)  # renovar só o dono renova
    assert store.renew(primeiro["id"], "w1", lease)


def test_lease_vencido_volta_para_a_fila_e_depois_desiste():
    job = store.enqueue("teste.x", max_attempts=2)
    lease = timedelta(seconds=30)
    assert store.claim("morto", ["teste.x"], lease)
    _vence_lease(job["id"])
    assert store.reap_expired() == []
    voltou = store.get_job(job["id"])
    assert voltou["status"] == "queued" and "Lease vencido" in voltou["last_error"]
    assert _aware(voltou["run_after"]) > datetime.now(timezone.utc)  # com backoff

    _vence_espera(job["id"])
    assert store.claim("morto-de-novo", ["teste.x"], lease)["attempts"] == 2
    assert not store.complete(job["id"], "morto")  # o primeiro dono não fecha mais nada
    _vence_lease(job["id"])
    desistiu = store.reap_expired()
    assert [j["id"] for j in desistiu] == [job["id"]] and store.get_job(job["id"])["status"] == "failed"


def test_retry_com_backoff_exponencial_e_on_give_up():
    desistencias = []

    @jobs_worker.register("teste.quebra", on_give_up=desistencias.append)
    async def quebra(job):
        raise RuntimeError("deu ruim")

    job = store.enqueue("teste.quebra", max_attempts=3)
    w = Worker()
    esperas = []
    for tentativa in (1, 2, 3):
        antes = datetime.now(timezone.utc)
        assert asyncio.run(w.run_once()) == ("queued" if tentativa < 3 else "failed")
        atual = store.get_job(job["id"])
        assert atual["attempts"] == tentativa and atual["last_error"] == "RuntimeError: deu ruim"
        if tentativa < 3:
            esperas.append((_aware(atual["run_after"]) - antes).total_seconds())
            assert asyncio.run(w.run_once()) is None  # ainda esperando o backoff
            _vence_espera(job["id"])
    assert 9 <= esperas[0] <= 11 and 19 <= esperas[1] <= 21  # 10s, 20s
    assert [d["id"] for d in desistencias] == [job["id"]]
    assert store.get_job(job["id"])["finished_at"] is not None


def test_agendamento_e_reschedule():
    rodou = []

    @jobs_worker.register("teste.recorrente")
    async def recorrente(job):
        rodou.append(job["id"])
        return Reschedule(run_after=datetime.now(timezone.utc) + timedelta(hours=1))

    futuro = store.enqueue("teste.recorrente", {"a": 1}, dedupe_key="rec:1",
                           run_after=datetime.now(timezone.utc) + timedelta(minutes=5))
    w = Worker()
    assert asyncio.run(w.run_once()) is None  # ainda não é hora
    assert store.advance("rec:1")  # "sincronizar agora" puxa o agendado
    assert asyncio.run(w.run_once()) == "succeeded" and rodou == [futuro["id"]]
    proximo = store.active_job("rec:1")
    assert proximo["id"] != futuro["id"] and proximo["payload"] == {"a": 1}
    assert _aware(proximo["run_after"]) > datetime.now(timezone.utc) + timedelta(minutes=59)


def test_dedupe_key_trava_so_entre_ativos():
    primeiro = store.enqueue("teste.x", dedupe_key="k")
    with pytest.raises(store.JobConflictError):
        store.enqueue("teste.x", dedupe_key="k")
    store.cancel(primeiro["id"])
    store.enqueue("teste.x", dedupe_key="k")  # cancelado não trava mais
    with pytest.raises(store.JobConflictError):
        store.retry(primeiro["id"])  # nem volta enquanto o outro estiver ativo


def test_cancelar_um_job_rodando_para_a_execucao():
    parou, comecou = [], []

    @jobs_worker.register("teste.longo")
    async def longo(job):
        comecou.append(job["id"])
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            parou.append(job["id"])
            raise

    job = store.enqueue("teste.longo")

    async def roda():
        w = Worker()
        tarefa = asyncio.create_task(w.run_once())
        while not comecou:
            await asyncio.sleep(0.01)
        store.cancel(job["id"])
        await w.cancel_local(job["id"])
        return await tarefa

    assert asyncio.run(roda()) is None
    assert parou == [job["id"]] and store.get_job(job["id"])["status"] == "cancelled"


def test_renew_falho_cancela_a_execucao_local(monkeypatch):
    """Outra réplica cancelou (ou o lease venceu): quem roda descobre no renew e para."""
    monkeypatch.setattr(store.get_settings(), "jobs_lease_seconds", 3)

    @jobs_worker.register("teste.longo")
    async def longo(job):
        store.cancel(job["id"])  # como se viesse de fora
        await asyncio.sleep(30)

    job = store.enqueue("teste.longo")
    assert asyncio.run(Worker().run_once()) is None
    assert store.get_job(job["id"])["status"] == "cancelled"


def test_desligar_devolve_o_job_sem_gastar_tentativa():
    @jobs_worker.register("teste.longo")
    async def longo(job):
        await asyncio.sleep(30)

    job = store.enqueue("teste.longo")

    async def roda():
        w = Worker(poll_seconds=0.01)
        w.start()
        while job["id"] not in w.running:
            await asyncio.sleep(0.01)
        await w.stop()

    asyncio.run(roda())
    voltou = store.get_job(job["id"])
    assert voltou["status"] == "queued" and voltou["attempts"] == 0


def _vence_lease(job_id: str) -> None:
    with store._engine().begin() as conn:
        conn.execute(store.update(store.jobs).where(store.jobs.c.id == job_id)
                     .values(locked_until=datetime.now(timezone.utc) - timedelta(seconds=1)))


def _vence_espera(job_id: str) -> None:
    with store._engine().begin() as conn:
        conn.execute(store.update(store.jobs).where(store.jobs.c.id == job_id)
                     .values(run_after=datetime.now(timezone.utc) - timedelta(seconds=1)))
