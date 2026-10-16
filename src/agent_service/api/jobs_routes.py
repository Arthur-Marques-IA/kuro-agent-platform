"""`/jobs`: a fila de jobs para o operador — ver, reenfileirar, cancelar (escopo admin).

Quem cria job é o próprio serviço (`web_sync.start_sync`, ...); por aqui só se
acompanha e se destrava. O status que o usuário final vê continua no recurso dono
do job (a fonte de site, por exemplo)."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from agent_service.jobs import store
from agent_service.jobs import worker as jobs_worker

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobOut(BaseModel):
    id: str
    kind: str
    payload: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_by: str | None = None
    locked_until: datetime | None = None
    last_error: str | None = None
    dedupe_key: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class JobListOut(BaseModel):
    data: list[JobOut]
    counts: dict[str, int]


def _or_404(job_id: str) -> dict[str, Any]:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} não encontrado")
    return job


@router.get("", response_model=JobListOut)
def list_jobs(
    status: str | None = Query(None, description="queued | running | succeeded | failed | cancelled"),
    kind: str | None = None,
    dedupe_key: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    if status is not None and status not in store.STATUSES:
        raise HTTPException(status_code=422, detail=f"status inválido; use um de {', '.join(store.STATUSES)}")
    return {"data": store.list_jobs(status=status, kind=kind, dedupe_key=dedupe_key, limit=limit),
            "counts": store.counts()}


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str) -> dict[str, Any]:
    return _or_404(job_id)


@router.post("/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: str) -> dict[str, Any]:
    """Põe de volta na fila um job `failed` ou `cancelled`, com as tentativas zeradas."""
    _or_404(job_id)
    try:
        job = store.retry(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except store.JobConflictError as exc:
        raise HTTPException(status_code=409, detail="Já há outro job ativo com a mesma dedupe_key.") from exc
    jobs_worker.wake()
    return job


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancela um job na fila ou rodando (numa outra réplica, ele para no próximo renew do lease)."""
    job = _or_404(job_id)
    if job["status"] not in store.ACTIVE:
        raise HTTPException(status_code=409, detail=f"O job já terminou ({job['status']}).")
    job = store.cancel(job_id)
    if (worker := jobs_worker.current()) is not None:
        await worker.cancel_local(job_id)
    return job  # type: ignore[return-value]
