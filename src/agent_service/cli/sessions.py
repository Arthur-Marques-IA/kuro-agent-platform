"""`kuro sessions`: as conversas guardadas — listar, ler a linha do tempo, apagar.

`list`, `rename` e `delete` usam as rotas de sessão do AgentOS (Postgres). `show` lê a
linha do tempo do trace store local (`GET /observability/sessions/{id}/timeline`): a
conversa já separada em eventos, fala, contexto, tools e resposta. Nenhum dos dois
depende do Langfuse.

O `user_id` é o mesmo de `kuro chat` (`cli` por padrão, ou `KURO_USER_ID`):
sem ele a listagem traria as conversas de todo mundo, inclusive as do console.
O filtro por agente vai em `component_id`, que casa com o `agent_type` porque
`agents/base.py` cria o `Agent` do Agno com `id=agent_type`.
"""

import os
from typing import Any

import typer
from rich.table import Table

from agent_service.cli.common import EXIT_USAGE, call, console, emit, fail, state

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


ROLE_LABELS = {
    "contact": "contato",
    "human_agent": "atendente",
    "agent_sent": "enviado em seu nome",
    "system": "sistema",
}
RAW_INPUT_MAX = 600
"""A entrada não estruturada pode ter dezenas de milhares de caracteres (o contexto
inteiro do sistema chamador): na tela vai só o começo; o `--json` traz inteira."""


def _when(value: Any) -> str:
    return str(value or "")[:19].replace("T", " ")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… [+{len(text) - limit} caracteres, veja --json]"


def _args(args: Any) -> str:
    if isinstance(args, dict):
        return ", ".join(f"{k}={_clip(str(v), 80)}" for k, v in args.items())
    return "" if args is None else _clip(str(args), 160)


def _render_turn(turn: dict[str, Any]) -> None:
    status = turn.get("status")
    head = [f"[bold]{_when(turn.get('at'))}[/]", turn.get("agent_type") or "—"]
    if turn.get("agent_version"):
        head.append(f"v{turn['agent_version']}")
    if status and status != "success":
        head.append(f"[red]{status}[/]")
    if turn.get("dry_run"):
        head.append("[dim]teste[/]")
    head.append(f"[dim]run {turn.get('run_id')}[/]")
    console.print(" · ".join(head), highlight=False)
    if turn.get("structured"):
        changed = [b for b in turn.get("context") or [] if b.get("changed")]
        if changed:
            keys = ", ".join(b.get("title") or b.get("key") for b in changed)
            console.print(f"  [yellow]contexto mudou:[/] {keys}", highlight=False)
        for event in turn.get("events") or []:
            if event.get("speech"):
                continue  # é a fala nova, logo abaixo
            label = ROLE_LABELS.get(event.get("role"), event.get("role") or "?")
            console.print(f"  [dim]{label}>[/] {event.get('content') or ''}", highlight=False)
        if turn.get("message"):
            console.print(f"  [bold cyan]contato>[/] {turn['message']}", highlight=False)
    else:
        raw = turn.get("raw_input") or "—"
        console.print(f"  [bold cyan]entrada>[/] {_clip(raw, RAW_INPUT_MAX)}", highlight=False)
    for step in turn.get("steps") or []:
        mark = "[green]✓[/]" if step.get("ok") else "[red]✗[/]"
        effect = " [magenta](efeito colateral)[/]" if step.get("side_effect") else ""
        detail = step.get("result_preview") if step.get("ok") else (step.get("error") or step.get("failure"))
        console.print(
            f"  {mark} {step.get('tool')}({_args(step.get('args'))}){effect} → {detail or '—'}", highlight=False
        )
    if turn.get("status_message") and status != "success":
        console.print(f"  [red]{turn['status_message']}[/]", highlight=False)
    console.print(f"  [bold magenta]agente>[/] {turn.get('output') or '—'}", highlight=False)
    usage = turn.get("usage") or {}
    parts = [f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out"]
    if usage.get("cost_usd") is not None:
        parts.append(f"US$ {usage['cost_usd']:.4f}")
    if usage.get("latency_ms") is not None:
        parts.append(f"{usage['latency_ms'] / 1000:.1f}s")
    console.print(f"  [dim]{' · '.join(parts)}[/]\n", highlight=False)


def _render_timeline(timeline: dict[str, Any]) -> None:
    parts = [
        f"[bold]{timeline.get('session_id')}[/]",
        ", ".join(timeline.get("agent_types") or []) or "—",
        f"{timeline.get('turns', 0)} turno(s)",
        f"{_when(timeline.get('started_at'))} → {_when(timeline.get('ended_at'))}",
        f"{timeline.get('total_tokens', 0)} tokens",
    ]
    if timeline.get("cost_usd") is not None:
        parts.append(f"US$ {timeline['cost_usd']:.4f}")
    if timeline.get("error_count"):
        parts.append(f"[red]{timeline['error_count']} erro(s)[/]")
    console.print(" · ".join(parts) + "\n", highlight=False)
    for turn in timeline.get("items") or []:
        _render_turn(turn)
    if timeline.get("next_cursor"):
        console.print(f"[dim]há turnos mais novos — use --cursor {timeline['next_cursor']}[/]", highlight=False)


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
    user_id: str = typer.Option(
        "cli",
        "--user-id",
        "-u",
        envvar="KURO_USER_ID",
        help="Só os runs deste user_id — o mesmo de `kuro chat`. `--all-users` tira o filtro.",
    ),
    all_users: bool = typer.Option(False, "--all-users", help="Não filtra por user_id (conversa de uma integração)."),
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=200, help="Turnos por página."),
    cursor: str | None = typer.Option(None, "--cursor", help="O `next_cursor` da página anterior."),
) -> None:
    """A linha do tempo da conversa: por turno a conversa e a fala nova, os blocos de
    contexto que mudaram, cada tool numa linha e a resposta, com tokens e custo."""
    st = state(ctx)
    result = call(
        st, st.client.session_timeline, session_id, None if all_users else user_id, limit=limit, cursor=cursor
    )
    emit(st, result, _render_timeline)


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
