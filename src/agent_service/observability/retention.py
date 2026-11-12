"""Retenção do trace store local (`RUN_RETENTION_DAYS`, `RUN_RETENTION_MODE`).

Desligada por padrão. Ligada, um job recorrente da fila de jobs (`jobs/`) trata os
runs com `started_at` mais antigo que o prazo:

- `delete`: apaga o run e o que depende dele — `run_spans`, `run_metadata`,
  `run_scores`, `run_references` e `chunk_feedback`;
- `anonymize`: mantém as linhas (tokens, custo, latência, status, datas, agente,
  versões, `user_id`, `session_id`, metadata, votos) e apaga o conteúdo — `message`,
  `input_message`, `input_events`, `input_context`, `output`, a entrada e a saída de
  cada span (`dependencies` e anexos estão na entrada da raiz), o `status_message`
  das tools (repete o resultado), a pergunta e o texto dos trechos de RAG
  (`metadata.references`), o comentário dos scores, a referência do shadow e a
  pergunta (`query`) das marcações de trecho. O run anonimizado ganha a metadata
  `retention=anonymized`, que é o que o tira das rodadas seguintes.

Em lotes de `BATCH_SIZE` runs, uma transação curta por lote (sem travar a tabela),
cada lote numa thread e com `await` entre eles — o loop do worker continua
renovando o lease. Uma única instância roda por vez, em qualquer número de réplicas:
o job tem `dedupe_key` fixa, e a fila só aceita um job ativo por chave e só entrega
um job a um worker. Rodar de novo não refaz nada (os apagados sumiram; os
anonimizados têm o marcador).

O que fica de fora (sessões do Agno, memórias, estado de procedurais, Langfuse...)
está em `docs/operacao.md#retenção-e-dados-pessoais`.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from sqlalchemy import delete, exists, insert, null, select, update

from agent_service.config import get_settings
from agent_service.db import get_db
from agent_service.jobs import store as jobs
from agent_service.jobs import worker as jobs_worker
from agent_service.observability.chunk_feedback import chunk_feedback
from agent_service.observability.run_store import (
    run_metadata,
    run_references,
    run_scores,
    run_spans,
    runs,
)

logger = logging.getLogger(__name__)

JOB_KIND = "trace_retention"
DEDUPE_KEY = "trace_retention"
INTERVAL = timedelta(hours=6)
"""De quanto em quanto tempo o job roda de novo. O prazo é em dias: mais que isso não ganha nada."""
BATCH_SIZE = 500
MARKER_KEY = "retention"
MARKER_VALUE = "anonymized"
REMOVED = "[removido]"
"""O que fica no lugar de um texto obrigatório (NOT NULL) que a anonimização apaga."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return get_settings().run_retention_days > 0


def cutoff(now: datetime | None = None) -> datetime:
    """Runs com `started_at` antes disto estão vencidos."""
    return (now or _now()) - timedelta(days=get_settings().run_retention_days)


# -- um lote ----------------------------------------------------------------------


def delete_batch(before: datetime, limit: int = BATCH_SIZE) -> int:
    """Apaga até `limit` runs mais antigos que `before`, com os dependentes. Devolve quantos."""
    with get_db().db_engine.begin() as conn:
        ids = list(
            conn.execute(
                select(runs.c.run_id).where(runs.c.started_at < before).order_by(runs.c.started_at).limit(limit)
            ).scalars()
        )
        if not ids:
            return 0
        for table in (run_spans, run_metadata, run_scores, run_references, chunk_feedback):
            conn.execute(delete(table).where(table.c.run_id.in_(ids)))
        conn.execute(delete(runs).where(runs.c.run_id.in_(ids)))
    return len(ids)


def _anonymous_span_metadata(meta: Any) -> Any:
    """Tira da metadata de um span o que é conteúdo: a pergunta e o texto de cada
    trecho de uma busca na base. A lista de resultados mantém o tamanho, senão o run
    passaria no filtro "busca sem trecho" (`rag=empty`)."""
    if not isinstance(meta, dict) or not isinstance(meta.get("references"), dict):
        return meta
    references = {k: v for k, v in meta["references"].items() if k != "query"}
    results = references.get("results")
    if isinstance(results, list):
        references["results"] = [
            {k: v for k, v in hit.items() if k != "content"} if isinstance(hit, dict) else hit for hit in results
        ]
    return {**meta, "references": references}


def anonymize_batch(before: datetime, limit: int = BATCH_SIZE) -> int:
    """Anonimiza até `limit` runs mais antigos que `before` ainda sem o marcador. Devolve quantos."""
    done = exists().where(
        run_metadata.c.run_id == runs.c.run_id, run_metadata.c.key == MARKER_KEY, run_metadata.c.value == MARKER_VALUE
    )
    with get_db().db_engine.begin() as conn:
        ids = list(
            conn.execute(
                select(runs.c.run_id).where(runs.c.started_at < before, ~done).order_by(runs.c.started_at).limit(limit)
            ).scalars()
        )
        if not ids:
            return 0
        conn.execute(
            update(runs)
            .where(runs.c.run_id.in_(ids))
            .values(message=None, output=None, input_message=None, input_events=None, input_context=None)
        )
        # `null()`: as colunas JSON de `run_spans` gravariam o JSON `null` com um None do Python.
        conn.execute(update(run_spans).where(run_spans.c.run_id.in_(ids)).values(input=null(), output=null()))
        conn.execute(
            update(run_spans).where(run_spans.c.run_id.in_(ids), run_spans.c.type == "TOOL").values(status_message=None)
        )
        for span_id, meta in conn.execute(
            select(run_spans.c.id, run_spans.c.metadata).where(run_spans.c.run_id.in_(ids))
        ).all():
            cleaned = _anonymous_span_metadata(meta)
            if cleaned is not meta:
                conn.execute(update(run_spans).where(run_spans.c.id == span_id).values(metadata=cleaned))
        conn.execute(update(run_scores).where(run_scores.c.run_id.in_(ids)).values(comment=None))
        conn.execute(delete(run_references).where(run_references.c.run_id.in_(ids)))
        conn.execute(update(chunk_feedback).where(chunk_feedback.c.run_id.in_(ids)).values(query=REMOVED))
        # Um run anonimizado de novo (o marcador foi apagado à mão) não pode colidir com a chave.
        conn.execute(delete(run_metadata).where(run_metadata.c.run_id.in_(ids), run_metadata.c.key == MARKER_KEY))
        conn.execute(insert(run_metadata), [{"run_id": i, "key": MARKER_KEY, "value": MARKER_VALUE} for i in ids])
    return len(ids)


# -- a rodada inteira ---------------------------------------------------------------


async def purge(before: datetime, *, mode: str | None = None, batch_size: int = BATCH_SIZE) -> dict[str, Any]:
    """Trata todos os runs vencidos, lote a lote. Para no meio se o serviço estiver
    desligando (o que faltou fica para a próxima rodada)."""
    mode = mode or get_settings().run_retention_mode
    batch = delete_batch if mode == "delete" else anonymize_batch
    total = 0
    while True:
        n = await asyncio.to_thread(batch, before, batch_size)
        total += n
        if n < batch_size or jobs_worker.is_stopping():
            break
        await asyncio.sleep(0)
    if total:
        logger.info("Retenção de traces (%s): %d run(s) antes de %s", mode, total, before.isoformat())
    return {"mode": mode, "before": before.isoformat(), "runs": total}


@jobs_worker.register(JOB_KIND, on_give_up=lambda job: schedule(run_after=_now() + INTERVAL))
async def run_retention_job(job: dict[str, Any]) -> jobs_worker.Reschedule | None:
    """O handler do job: uma rodada e o agendamento da próxima. Desligada a retenção
    depois de agendada, o job termina sem fazer nada e não volta."""
    if not enabled():
        return None
    await purge(cutoff())
    return jobs_worker.Reschedule(run_after=_now() + INTERVAL)


def schedule(*, run_after: datetime | None = None) -> None:
    """Deixa a fila coerente com a configuração: ligada, um job na fila (agora, por
    padrão); desligada, nenhum esperando. Idempotente e seguro entre réplicas."""
    if not enabled():
        jobs.cancel_dedupe(DEDUPE_KEY, only_queued=True)
        return
    try:
        jobs.enqueue(JOB_KIND, {}, dedupe_key=DEDUPE_KEY, run_after=run_after)
    except jobs.JobConflictError:
        pass  # já está na fila ou rodando (outra réplica subiu antes)


@asynccontextmanager
async def retention_lifespan(_app: Any = None) -> AsyncIterator[None]:
    """No startup, agenda (ou desagenda) o job da retenção."""
    try:
        await asyncio.to_thread(schedule)
    except Exception:
        logger.warning("Não consegui agendar a retenção de traces", exc_info=True)
    yield
