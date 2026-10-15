"""`Idempotency-Key` em `/chat` e `/analyze` (tabela `idempotency_keys`).

Quem integra repete a chamada depois de um timeout ou de uma queda de rede — e não sabe
se a primeira chegou. Num agente conversacional com tool de efeito colateral, repetir
**repetia a ação** e duplicava o turno no histórico. Com o header:

- a mesma chave (no mesmo endpoint e agente) com o mesmo corpo, depois de concluída,
  devolve **a resposta guardada**, com `Idempotent-Replayed: true` — nada roda de novo;
- ainda em andamento: **409**, para quem chama esperar e repetir;
- com outro corpo: **422** — é bug de quem chama (a chave foi reaproveitada);
- se a execução falhou (4xx, 5xx, 504), a reserva some e a repetição roda de verdade.
  O destino das tools deduplica pela mesma chave, que elas recebem em
  `dependencies.idempotency_key` (ver `tools/context.py`).

No banco, e não em memória, porque com mais de um worker (ou réplica) a repetição cai em
outro processo. A reserva é um `INSERT` com chave única: das duas chamadas simultâneas,
só uma grava, e a outra lê a linha e recebe o 409.

Uma reserva `in_progress` mais velha que o pior caso de uma execução (`STALE_AFTER`) é de
um processo que morreu no meio: é retomada, senão a chave ficaria em 409 até expirar.
"""

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, UniqueConstraint, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from agent_service.config import get_settings
from agent_service.db import get_db

HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
MAX_KEY_LENGTH = 255

metadata = MetaData()

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("id", String, primary_key=True),
    Column("endpoint", String, nullable=False),
    Column("agent_type", String, nullable=False),
    Column("key", String, nullable=False),
    Column("request_hash", String, nullable=False),
    Column("status", String, nullable=False),  # in_progress | done
    Column("response", JSON, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("endpoint", "agent_type", "key", name="uq_idempotency_keys_key"),
)

# O timeout de um agente vai até 600 s (`AgentDefinitionIn.timeout_seconds`), mais a espera
# na fila e uma folga: passou disso sem concluir, o processo que reservou não volta mais.
MAX_RUN_SECONDS = 600


def stale_after() -> timedelta:
    settings = get_settings()
    return timedelta(seconds=max(MAX_RUN_SECONDS, settings.run_timeout_seconds) + settings.queue_max_wait_seconds + 60)


class IdempotencyError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Reservation:
    id: str | None = None
    """A linha reservada por esta chamada — `None` quando é uma repetição."""
    replay: dict[str, Any] | None = None
    """A resposta guardada de uma chamada concluída com a mesma chave e o mesmo corpo."""


def check_key(key: str) -> str:
    key = key.strip()
    if not key or len(key) > MAX_KEY_LENGTH or not key.isprintable():
        raise IdempotencyError(
            422, f"{HEADER} inválida: de 1 a {MAX_KEY_LENGTH} caracteres imprimíveis (um UUID por operação serve)."
        )
    return key


def request_hash(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    # O SQLite (testes) devolve sem fuso o que gravou com fuso.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def reserve(endpoint: str, agent_type: str, key: str, body_hash: str) -> Reservation:
    """Reserva a chave para esta chamada, ou diz o que fazer com ela. I/O síncrono no banco:
    chame via `run_in_threadpool`."""
    _purge_expired()
    for _ in range(3):  # uma linha vencida ou abandonada sai, e a reserva é tentada de novo
        now = _now()
        row_id = uuid.uuid4().hex
        try:
            with get_db().db_engine.begin() as conn:
                conn.execute(
                    insert(idempotency_keys).values(
                        id=row_id, endpoint=endpoint, agent_type=agent_type, key=key, request_hash=body_hash,
                        status="in_progress", created_at=now, updated_at=now,
                    )
                )
            return Reservation(id=row_id)
        except IntegrityError:
            pass
        row = _find(endpoint, agent_type, key)
        if row is None:  # liberada entre o INSERT e a leitura
            continue
        age = now - _aware(row["updated_at"])
        expired = now - _aware(row["created_at"]) > _ttl()
        abandoned = row["status"] == "in_progress" and age > stale_after()
        if expired or abandoned:
            _delete_if_unchanged(row)
            continue
        if row["request_hash"] != body_hash:
            raise IdempotencyError(
                422,
                f"{HEADER} {key!r} já foi usada com outro corpo neste endpoint e agente. Cada operação "
                "leva a sua chave; a mesma chave só serve para repetir exatamente a mesma requisição.",
            )
        if row["status"] == "in_progress":
            raise IdempotencyError(
                409,
                f"Uma requisição com a {HEADER} {key!r} ainda está em andamento. Espere a resposta dela "
                "(ou alguns segundos) e repita com a mesma chave.",
            )
        return Reservation(replay=row["response"])
    raise IdempotencyError(409, f"Não consegui reservar a {HEADER} {key!r}: tente de novo.")


def complete(row_id: str, response: dict[str, Any]) -> None:
    with get_db().db_engine.begin() as conn:
        conn.execute(
            update(idempotency_keys)
            .where(idempotency_keys.c.id == row_id)
            .values(status="done", response=response, updated_at=_now())
        )


def release(row_id: str) -> None:
    """A execução falhou: a repetição com a mesma chave roda de verdade."""
    with get_db().db_engine.begin() as conn:
        conn.execute(delete(idempotency_keys).where(idempotency_keys.c.id == row_id))


def _find(endpoint: str, agent_type: str, key: str) -> dict[str, Any] | None:
    with get_db().db_engine.connect() as conn:
        row = conn.execute(
            select(idempotency_keys).where(
                idempotency_keys.c.endpoint == endpoint,
                idempotency_keys.c.agent_type == agent_type,
                idempotency_keys.c.key == key,
            )
        ).first()
    return dict(row._mapping) if row else None


def _delete_if_unchanged(row: dict[str, Any]) -> None:
    # Só se ninguém mexeu nela desde a leitura: outra chamada pode tê-la retomado no meio.
    with get_db().db_engine.begin() as conn:
        conn.execute(
            delete(idempotency_keys).where(
                idempotency_keys.c.id == row["id"], idempotency_keys.c.updated_at == row["updated_at"]
            )
        )


def _ttl() -> timedelta:
    return timedelta(hours=get_settings().idempotency_ttl_hours)


_last_purge = 0.0
PURGE_EVERY_SECONDS = 600


def _purge_expired() -> None:
    """Some com as chaves vencidas de vez em quando — a tabela não cresce para sempre."""
    global _last_purge
    if time.monotonic() - _last_purge < PURGE_EVERY_SECONDS:
        return
    _last_purge = time.monotonic()
    with get_db().db_engine.begin() as conn:
        conn.execute(delete(idempotency_keys).where(idempotency_keys.c.created_at < _now() - _ttl()))
