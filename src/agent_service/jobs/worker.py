"""O worker que roda os jobs, dentro do processo do serviço (lifespan).

`JOBS_CONCURRENCY` tarefas puxam da fila (`store.claim`). Cada job roda com um
lease renovado a cada terço de `JOBS_LEASE_SECONDS`; se a renovação falhar (o job
foi cancelado, ou o lease venceu e outra réplica o pegou), a execução local é
cancelada. Um processo que morre deixa o lease vencer, e `store.reap_expired`
devolve o job à fila.

Acordar: no Postgres, `LISTEN kuro_jobs` numa conexão própria (o `enqueue` faz
`pg_notify` no commit); o polling a cada `JOBS_POLL_SECONDS` é o fallback — e no
SQLite dos testes, o único caminho.
"""

import asyncio
import logging
import os
import socket
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

from agent_service.config import get_settings
from agent_service.jobs import store

logger = logging.getLogger(__name__)


@dataclass
class Reschedule:
    """O que um handler devolve para o mesmo job rodar de novo em `run_after`
    (mesmo `kind`, `payload` e `dedupe_key`). É assim que se agenda algo recorrente."""

    run_after: datetime
    payload: dict[str, Any] | None = None


Handler = Callable[[dict[str, Any]], Awaitable[Reschedule | None]]
GiveUp = Callable[[dict[str, Any]], Any]


@dataclass
class _Registered:
    run: Handler
    max_attempts: int | None = None
    on_give_up: GiveUp | None = None


HANDLERS: dict[str, _Registered] = {}


def register(kind: str, *, max_attempts: int | None = None, on_give_up: GiveUp | None = None):
    """Registra o handler de um `kind`: `async def handler(job) -> Reschedule | None`.

    Exceção = tentativa falhou (volta com backoff até `max_attempts`). `on_give_up(job)`
    é chamado quando as tentativas acabam — inclusive por lease vencido —, para o
    dono do job deixar o próprio status coerente."""

    def decorator(fn: Handler) -> Handler:
        HANDLERS[kind] = _Registered(fn, max_attempts, on_give_up)
        return fn

    return decorator


def _lease() -> timedelta:
    return timedelta(seconds=max(get_settings().jobs_lease_seconds, 3))


def _give_up(job: dict[str, Any]) -> None:
    handler = HANDLERS.get(job["kind"])
    if handler is None or handler.on_give_up is None:
        return
    try:
        handler.on_give_up(job)
    except Exception:
        logger.warning("on_give_up do job %s (%s) falhou", job["id"], job["kind"], exc_info=True)


class Worker:
    def __init__(self, *, concurrency: int | None = None, poll_seconds: float | None = None) -> None:
        settings = get_settings()
        self.id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}"
        self.concurrency = max(concurrency or settings.jobs_concurrency, 1)
        self.poll_seconds = poll_seconds if poll_seconds is not None else settings.jobs_poll_seconds
        self.wake = asyncio.Event()
        self.stopping = False
        self.running: dict[str, asyncio.Task[Any]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._listener: threading.Thread | None = None
        self._listener_stop = threading.Event()

    # -- execução -----------------------------------------------------------------

    async def run_job(self, job: dict[str, Any]) -> str | None:
        """Roda um job já reivindicado por este worker e grava o resultado."""
        handler = HANDLERS.get(job["kind"])
        if handler is None:
            return await asyncio.to_thread(store.fail, job["id"], self.id, f"sem handler para {job['kind']!r}")
        lease = _lease()
        task = asyncio.create_task(handler.run(job), name=f"job-{job['kind']}-{job['id'][:8]}")
        self.running[job["id"]] = task
        lost = False
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=lease.total_seconds() / 3)
                if done:
                    break
                if not await asyncio.to_thread(store.renew, job["id"], self.id, lease):
                    lost = True  # cancelado, ou o lease venceu e voltou à fila
                    task.cancel()
                    await asyncio.wait({task})
                    break
        except asyncio.CancelledError:
            # O serviço está desligando: para o handler e devolve o job à fila.
            task.cancel()
            await asyncio.wait({task})
            await asyncio.to_thread(store.release, job["id"], self.id)
            raise
        finally:
            self.running.pop(job["id"], None)

        if lost or task.cancelled():
            if not lost:
                await asyncio.to_thread(store.release, job["id"], self.id)
            return None
        exc = task.exception()
        if exc is None:
            result = task.result()
            follow_up = None
            if isinstance(result, Reschedule):
                follow_up = {
                    "kind": job["kind"],
                    "payload": result.payload if result.payload is not None else job["payload"],
                    "run_after": result.run_after,
                    "dedupe_key": job["dedupe_key"],
                    "max_attempts": job["max_attempts"],
                }
            await asyncio.to_thread(store.complete, job["id"], self.id, follow_up=follow_up)
            return "succeeded"
        logger.warning("Job %s (%s) falhou na tentativa %s", job["id"], job["kind"], job["attempts"], exc_info=exc)
        status = await asyncio.to_thread(store.fail, job["id"], self.id, f"{type(exc).__name__}: {exc}")
        if status == "failed":
            _give_up({**job, "status": "failed", "last_error": f"{type(exc).__name__}: {exc}"})
        return status

    async def run_once(self) -> str | None:
        """Reivindica e roda um job, se houver. Útil em testes e na CLI de manutenção."""
        for job in await asyncio.to_thread(store.reap_expired):
            _give_up(job)
        job = await asyncio.to_thread(store.claim, self.id, list(HANDLERS), _lease())
        if job is None:
            return None
        if self.stopping:  # pegou no instante em que o serviço começou a desligar
            await asyncio.to_thread(store.release, job["id"], self.id)
            return None
        return await self.run_job(job)

    async def _loop(self, n: int) -> None:
        while not self.stopping:
            try:
                ran = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Worker de jobs %s/%s: rodada falhou", self.id, n, exc_info=True)
                ran = None
            if ran is None:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
                self.wake.clear()

    def notify(self) -> None:
        self.wake.set()

    # -- LISTEN/NOTIFY --------------------------------------------------------------

    def _listen(self, loop: asyncio.AbstractEventLoop) -> None:
        """Numa thread, com conexão própria (fora do pool): acorda os loops quando
        alguém enfileira. Caiu? Tenta de novo; enquanto isso o polling segura."""
        import psycopg

        url = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        while not self._listener_stop.is_set():
            try:
                with psycopg.connect(url, autocommit=True) as conn:
                    conn.execute(f"LISTEN {store.NOTIFY_CHANNEL}")
                    while not self._listener_stop.is_set():
                        for _ in conn.notifies(timeout=1.0, stop_after=1):
                            loop.call_soon_threadsafe(self.wake.set)
            except Exception:
                logger.debug("LISTEN %s caiu; tentando de novo", store.NOTIFY_CHANNEL, exc_info=True)
                self._listener_stop.wait(self.poll_seconds)

    # -- ciclo de vida --------------------------------------------------------------

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._loop(n), name=f"jobs-worker-{n}") for n in range(self.concurrency)]
        engine_name = store._engine().dialect.name
        if get_settings().jobs_listen and engine_name == "postgresql":
            self._listener = threading.Thread(target=self._listen, args=(loop,), name="jobs-listen", daemon=True)
            self._listener.start()
        logger.info("Worker de jobs %s: %s tarefa(s), kinds=%s", self.id, self.concurrency, sorted(HANDLERS))

    async def stop(self, grace_seconds: float = 10.0) -> None:
        """Para os handlers em andamento e deixa cada loop fechar o seu job (devolver
        à fila, ou gravar o resultado de quem acabou de terminar). Cancelar o loop no
        meio dessa escrita deixaria o job `running` até o lease vencer — e quem já
        terminou rodaria de novo."""
        self.stopping = True
        self._listener_stop.set()
        self.wake.set()
        for task in list(self.running.values()):
            task.cancel()
        _, pending = await asyncio.wait(self._tasks, timeout=grace_seconds) if self._tasks else (set(), set())
        for task in pending:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def cancel_local(self, job_id: str) -> None:
        """Interrompe aqui um job já marcado como `cancelled` no banco, sem esperar o
        próximo `renew` (que é como as outras réplicas ficam sabendo)."""
        task = self.running.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})


_current: Worker | None = None


def current() -> Worker | None:
    return _current


def is_stopping() -> bool:
    """O serviço está desligando? Um handler cancelado agora vai voltar para a fila."""
    return _current is not None and _current.stopping


def wake() -> None:
    if _current is not None:
        _current.notify()


@asynccontextmanager
async def lifespan(_app: Any = None) -> AsyncIterator[None]:
    """Sobe o worker no startup e o desliga no shutdown (jobs em andamento voltam à fila)."""
    global _current
    if not get_settings().jobs_enabled:
        yield
        return
    worker = Worker()
    _current = worker
    worker.start()
    try:
        yield
    finally:
        await worker.stop()
        _current = None
