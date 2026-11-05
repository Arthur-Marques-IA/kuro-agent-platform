"""`kuro sessions`: as conversas guardadas — listar, ler a transcrição, apagar.

Usa as rotas de sessão do AgentOS (Postgres), não `/observability/sessions`:
a conversa em si não depende do Langfuse, então isto funciona também no modo
só-terminal, em que o tracing está desligado.

O `user_id` é o mesmo de `kuro chat` (`cli` por padrão, ou `KURO_USER_ID`):
sem ele a listagem traria as conversas de todo mundo, inclusive as do console.
O filtro por agente vai em `component_id`, que casa com o `agent_type` porque
`agents/base.py` cria o `Agent` do Agno com `id=agent_type`.
"""

import os
from typing import Any

import typer
from rich.table import Table

from agent_service.cli.client import ApiError, ServiceUnavailable
from agent_service.cli.common import EXIT_FAILED, EXIT_USAGE, call, console, emit, fail, fail_from, parse_pairs, state
from agent_service.cli.replay import ReplayError, not_found_hint, replay

app = typer.Typer(help="Sessões (conversas): listar, ver a transcrição, apagar.")

_USER_ID = typer.Option(
    "cli", "--user-id", "-u", envvar="KURO_USER_ID", help="Dono das conversas — o mesmo de `kuro chat`."
)


def _render_list(page: dict[str, Any]) -> None:
    items = page.get("data") or []
    if not items:
        console.print("Nenhuma conversa para este user_id — converse com `kuro chat <agente>` ou troque `--user-id`.")
        return
    table = Table(show_edge=False, header_style="bold")
    for column in ("atualizada", "session_id", "agente", "título", "tokens"):
        table.add_column(column, no_wrap=True)
    for s in items:
        when = str(s.get("updated_at") or s.get("created_at") or "")[5:16].replace("T", " ")
        title = (s.get("session_name") or "—")[:48]
        table.add_row(
            when,
            f"[cyan]{s['session_id']}[/]",
            s.get("agent_id") or "—",
            title,
            str(s.get("total_tokens") or "—"),
        )
    console.print(table)
    meta = page.get("meta") or {}
    if (meta.get("total_pages") or 0) > 1:
        console.print(f"[dim]página {meta.get('page')} de {meta['total_pages']} — use --page[/]")


def _render_transcript(runs: list[dict[str, Any]]) -> None:
    if not runs:
        console.print("[dim]sessão sem execuções[/]")
        return
    for run in runs:
        console.print(f"[bold cyan]você>[/] {run.get('run_input') or '—'}", highlight=False)
        content = run.get("content")
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        console.print(f"[bold magenta]agente>[/] {content or '—'}", highlight=False)
        metrics = run.get("metrics") or {}
        parts = [f"run {run['run_id']}"]
        if metrics.get("total_tokens"):
            parts.append(f"{metrics['total_tokens']} tokens")
        console.print(f"[dim]{' · '.join(parts)}[/]\n", highlight=False)


@app.callback(invoke_without_command=True)
def sessions(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:  # defaults explícitos: chamar direto não resolve os typer.Option
        list_sessions(ctx, agent=None, user_id=os.environ.get("KURO_USER_ID") or "cli", limit=20, page=1)


@app.command("list")
def list_sessions(
    ctx: typer.Context,
    agent: str | None = typer.Option(None, "--agent", "-a", help="Filtra por agent_type."),
    user_id: str = _USER_ID,
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=100),
    page: int = typer.Option(1, "--page", min=1),
) -> None:
    """Conversas de um user_id, da mais recente para a mais antiga."""
    st = state(ctx)
    result = call(
        st, st.client.list_sessions, user_id=user_id, component_id=agent, limit=limit, page=page
    )
    emit(st, result, _render_list)


@app.command("show")
def show_session(
    ctx: typer.Context,
    session_id: str,
    user_id: str = _USER_ID,
) -> None:
    """Transcrição da conversa: cada mensagem, a resposta e os tokens do run."""
    st = state(ctx)
    try:
        runs = st.client.session_runs(session_id, user_id)
    except ApiError as exc:
        if exc.status != 404:
            fail_from(st, exc)
        fail(st, f"conversa {session_id!r} não encontrada para o user_id {user_id!r}.{_hint(st, session_id, user_id)}", EXIT_FAILED)
    except ServiceUnavailable as exc:
        fail_from(st, exc)
    emit(st, runs, _render_transcript)


def _hint(st: Any, session_id: str, user_id: str) -> str:
    """Por que não achou: a conversa é de outro dono (uma integração grava com o próprio
    user_id, e o padrão aqui é `cli`) ou o id está um pouco diferente."""
    try:
        owners = {r.get("user_id") for r in st.client.list_runs(session_id=session_id, limit=5).get("items") or []}
    except (ApiError, ServiceUnavailable):
        return ""
    owners.discard(None)
    if owners and user_id not in owners:
        return f" Ela é de {', '.join(sorted(owners))}: use -u {sorted(owners)[0]}."
    return not_found_hint(st.client, session_id)


@app.command("replay")
def replay_session(
    ctx: typer.Context,
    session_id: str,
    agent: str | None = typer.Option(None, "--agent", "-a", help="Quem responde (padrão: o agente da conversa). Ex.: o draft."),
    compare: str | None = typer.Option(None, "--compare", help="Roda também neste agente (ex.: o de produção) e compara."),
    repeat: int = typer.Option(1, "--repeat", "-n", min=1, max=10, help="Quantas vezes rodar a conversa em cada agente."),
    turns: int | None = typer.Option(None, "--turns", min=1, help="Só os primeiros N turnos."),
    focus: list[str] = typer.Option(
        [], "--focus", "-f", help="Argumento de tool que resume a decisão, como tool.arg (repetível). Ex.: regente_r6_decidir.acao"
    ),
    dep: list[str] = typer.Option([], "--dep", "-d", help="Sobrepõe uma dependency da conversa: nome=valor (repetível)."),
) -> None:
    """Reenvia as falas de uma conversa guardada, na ordem e numa sessão nova, e mostra a decisão
    do agente em cada turno — o teste de regressão de um agente conversacional (o eval é só para
    analysis). Sempre em dry_run. Ex.: `kuro sessions replay <id> -a r6-draft --compare r6 -n 3
    -f regente_r6_decidir.acao`."""
    st = state(ctx)
    try:
        report = replay(
            st.client, session_id, agent_type=agent, compare_with=compare, repeat=repeat, turns=turns,
            focus=focus or None, dependencies=parse_pairs(st, dep, "--dep") or None,
        )
    except ReplayError as exc:
        fail(st, str(exc), EXIT_USAGE)
    except (ApiError, ServiceUnavailable) as exc:
        fail_from(st, exc)
    emit(st, report, _render_replay)


def _render_replay(r: dict[str, Any]) -> None:
    console.print(
        f"Conversa [cyan]{r['session_id']}[/] ({r['source_agent']}, {r['turns']} turnos), {r['repeat']}× em cada agente",
        highlight=False,
    )
    table = Table(show_edge=False, header_style="bold")
    table.add_column("turno", justify="right")
    table.add_column("original")
    for name in r["agents"]:
        table.add_column(name)
    for i, orig in enumerate(r["original"]):
        cells = []
        for data in r["agents"].values():
            row = data["summary"][i]
            cells.append("\n".join(f"{n}× {d}" for d, n in row["decisions"].items()))
        table.add_row(str(i + 1), orig["decision"], *cells)
    console.print(table)
    console.print("[dim]os runs do replay ficam como teste (dry_run): `kuro runs list --session-id <replay-…>`[/]")


@app.command("rename")
def rename_session(ctx: typer.Context, session_id: str, name: str) -> None:
    """Dá um nome à conversa — é o que aparece na lista, aqui e no console."""
    st = state(ctx)
    emit(
        st,
        call(st, st.client.rename_session, session_id, name),
        lambda _: console.print(f"[green]✓[/] sessão {session_id} renomeada"),
    )


@app.command("delete")
def delete_session(
    ctx: typer.Context,
    session_id: str,
    user_id: str = _USER_ID,
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação (obrigatório sem TTY)."),
) -> None:
    """Apaga a conversa e todas as suas execuções. Não tem volta.

    O registro das execuções não é afetado: `kuro runs` continua mostrando o
    que aconteceu."""
    st = state(ctx)
    if not yes:
        if not st.interactive:
            fail(st, "confirme com --yes para apagar sem TTY", EXIT_USAGE)
        if not typer.confirm(f"Apagar a conversa {session_id!r} e todo o histórico dela?"):
            raise typer.Exit()
    call(st, st.client.delete_session, session_id, user_id)
    emit(st, {"deleted": session_id}, lambda _: console.print(f"[green]✓[/] conversa {session_id} apagada"))
