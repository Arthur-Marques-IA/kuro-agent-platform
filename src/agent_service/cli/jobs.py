"""`kuro jobs`: a fila de jobs em background (sincronização de sites, ...) — ver e destravar."""

from typing import Any

import typer
from rich.table import Table

from agent_service.cli.common import call, console, emit, state

app = typer.Typer(help="Fila de jobs em background: listar, ver, reenfileirar, cancelar.")

_COLORS = {"queued": "yellow", "running": "cyan", "succeeded": "green", "failed": "red", "cancelled": "dim"}


def _when(value: Any) -> str:
    return str(value or "")[5:16].replace("T", " ") or "—"


def _render_list(page: dict[str, Any]) -> None:
    counts = page.get("counts") or {}
    console.print("  ".join(f"[{_COLORS.get(k, 'white')}]{k}[/] {v}" for k, v in counts.items()))
    items = page.get("data") or []
    if not items:
        console.print("[dim]nenhum job com esse filtro[/]")
        return
    table = Table(show_edge=False, header_style="bold")
    for column in ("criado", "id", "kind", "status", "tent.", "roda em", "chave / erro"):
        table.add_column(column, no_wrap=True)
    for job in items:
        color = _COLORS.get(job["status"], "white")
        table.add_row(
            _when(job["created_at"]), f"[cyan]{job['id'][:12]}[/]", job["kind"], f"[{color}]{job['status']}[/]",
            f"{job['attempts']}/{job['max_attempts']}", _when(job["run_after"]),
            (job.get("last_error") or job.get("dedupe_key") or "—")[:60],
        )
    console.print(table)


def _render_job(job: dict[str, Any]) -> None:
    for key, value in job.items():
        console.print(f"[bold]{key}:[/] {value}", highlight=False)


@app.callback(invoke_without_command=True)
def jobs(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        list_jobs(ctx, status=None, kind=None, limit=20)


@app.command("list")
def list_jobs(
    ctx: typer.Context,
    status: str | None = typer.Option(None, "--status", "-s", help="queued | running | succeeded | failed | cancelled"),
    kind: str | None = typer.Option(None, "--kind", "-k", help="Ex.: web_source.sync"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=500),
) -> None:
    """Jobs do mais recente para o mais antigo, com a contagem por status."""
    st = state(ctx)
    emit(st, call(st, st.client.list_jobs, status=status, kind=kind, limit=limit), _render_list)


@app.command("show")
def show_job(ctx: typer.Context, job_id: str) -> None:
    """Um job: payload, tentativas, lease e o último erro."""
    st = state(ctx)
    emit(st, call(st, st.client.get_job, job_id), _render_job)


@app.command("retry")
def retry_job(ctx: typer.Context, job_id: str) -> None:
    """Põe de volta na fila um job failed/cancelled, com as tentativas zeradas."""
    st = state(ctx)
    emit(st, call(st, st.client.retry_job, job_id), lambda j: console.print(f"[green]✓[/] {j['id']} de volta na fila"))


@app.command("cancel")
def cancel_job(ctx: typer.Context, job_id: str) -> None:
    """Cancela um job na fila ou rodando."""
    st = state(ctx)
    emit(st, call(st, st.client.cancel_job, job_id), lambda j: console.print(f"[green]✓[/] {j['id']} cancelado"))
