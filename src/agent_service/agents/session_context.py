"""O que o servidor lembra de uma sessão entre um turno e outro do `/chat`.

Duas coisas, as duas lidas do trace store local (`runs`), sem tabela nova:

- **o contexto vigente** (`current_context`): o `input_context` do run mais recente
  da sessão que tinha contexto (`input_context` não nulo). Um turno que não manda
  `context` herda esse; `[]` é "a sessão ficou sem contexto", diferente de `None`,
  "a sessão nunca teve";
- **a última resposta do agente** (`last_reply`): o `output` do run mais recente da
  sessão que terminou com sucesso. Serve para tirar do texto do modelo o
  `agent_sent` que só repete essa resposta (`structured_input.echo_indices`).

O banco sozinho não basta: a gravação do run vai por uma fila (`run_store.TraceWriter`)
e pode ainda não ter chegado quando o turno seguinte entra (ou ter sido descartada,
com a fila cheia). Por isso há um cache curto em memória, por sessão, alimentado no
caminho do próprio run (`observability/tracing.py`): o contexto quando o run começa
(já com a vaga de execução, logo antes de ir ao modelo), a resposta quando ele
termina — os dois antes de o registro entrar na fila. Na leitura, o banco é sempre
consultado (uma linha, pelo índice de `runs.session_id`) e vence o cache quando o run
dele é mais recente: com mais de um worker, o turno anterior pode ter rodado noutro
processo, e o cache deste estaria velho. O que nenhum dos dois cobre é o run de outro
processo que ainda está na fila dele.
"""

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

MAX_SESSIONS = 5000
"""Sessões lembradas em memória; acima disso sai a usada há mais tempo (o banco cobre)."""


@dataclass
class _Entry:
    context_at: datetime | None = None
    context: list[dict[str, Any]] | None = None
    reply_at: datetime | None = None
    reply: str | None = None
    touched: float = field(default=0.0)


_lock = threading.Lock()
_cache: "OrderedDict[str, _Entry]" = OrderedDict()


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    # O SQLite devolve sem fuso o que foi gravado em UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _entry(session_id: str) -> _Entry:
    entry = _cache.get(session_id)
    if entry is None:
        entry = _cache[session_id] = _Entry()
        while len(_cache) > MAX_SESSIONS:
            _cache.popitem(last=False)
    else:
        _cache.move_to_end(session_id)
    return entry


def remember_context(session_id: str, context: list[dict[str, Any]], at: datetime) -> None:
    """O contexto vigente de um run que começou em `at` (o `started_at` do registro)."""
    with _lock:
        entry = _entry(session_id)
        at = _utc(at) or at
        if entry.context_at is None or at >= entry.context_at:
            entry.context_at, entry.context = at, [dict(block) for block in context]


def remember_reply(session_id: str, reply: str, at: datetime) -> None:
    """A resposta de um run bem-sucedido que começou em `at`."""
    with _lock:
        entry = _entry(session_id)
        at = _utc(at) or at
        if entry.reply_at is None or at >= entry.reply_at:
            entry.reply_at, entry.reply = at, reply


def forget(session_id: str | None = None) -> None:
    """Esquece uma sessão (ou todas). Para os testes e para quem apaga a conversa."""
    with _lock:
        if session_id is None:
            _cache.clear()
        else:
            _cache.pop(session_id, None)


def _cached(session_id: str) -> _Entry | None:
    with _lock:
        entry = _cache.get(session_id)
        if entry is None:
            return None
        return _Entry(entry.context_at, entry.context, entry.reply_at, entry.reply)


def _latest(session_id: str, column: str) -> tuple[datetime | None, Any]:
    from agent_service.db import get_db
    from agent_service.observability.run_store import runs

    conditions = [runs.c.session_id == session_id, runs.c[column].is_not(None)]
    if column == "output":
        conditions.append(runs.c.status == "success")
    with get_db().db_engine.connect() as conn:
        row = conn.execute(
            select(runs.c.started_at, runs.c[column])
            .where(*conditions)
            .order_by(runs.c.started_at.desc(), runs.c.run_id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None, None
    return _utc(row[0]), row[1]


def current_context(session_id: str) -> list[dict[str, Any]] | None:
    """Os blocos vigentes da sessão (formato de `runs.input_context`), `[]` se ela foi
    limpa, `None` se nunca teve contexto."""
    db_at, db_context = _latest(session_id, "input_context")
    cached = _cached(session_id)
    if cached is not None and cached.context_at is not None and (db_at is None or cached.context_at >= db_at):
        return [dict(block) for block in cached.context or []]
    if db_at is None or not isinstance(db_context, list):
        return None
    return [block for block in db_context if isinstance(block, dict)]


def last_reply(session_id: str) -> str | None:
    """A resposta do run bem-sucedido mais recente da sessão."""
    db_at, db_reply = _latest(session_id, "output")
    cached = _cached(session_id)
    if cached is not None and cached.reply_at is not None and (db_at is None or cached.reply_at >= db_at):
        return cached.reply
    return db_reply if isinstance(db_reply, str) else None
