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
    for c in turn.get("checks") or []:
        if c.get("status") == "passed":
            continue
        color = "red" if c.get("status") == "failed" and c.get("severity") == "fail" else "yellow"
        console.print(
            f"  [{color}]checagem {c.get('name')}: {c.get('status')}[/] "
            f"[dim]({c.get('field')} {c.get('op')} {c.get('expected')!r}; veio {c.get('actual')!r})[/]",
            highlight=False,
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
    result = call(st, st.client.list_sessions, user_id=user_id, component_id=agent, limit=limit, page=page)
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
    scope = None if all_users else user_id
    try:
        result = st.client.session_timeline(session_id, scope, limit=limit, cursor=cursor)
    except ApiError as exc:
        if exc.status != 404:
            fail_from(st, exc)
        whose = f" para o user_id {scope!r}" if scope else ""
        fail(st, f"conversa {session_id!r} não encontrada{whose}.{_hint(st, session_id, scope)}", EXIT_FAILED)
    except ServiceUnavailable as exc:
        fail_from(st, exc)
    emit(st, result, _render_timeline)


def _hint(st: Any, session_id: str, user_id: str | None) -> str:
    """Por que não achou: a conversa é de outro dono (uma integração grava com o próprio
    user_id, e o padrão aqui é `cli`) ou o id está um pouco diferente."""
    try:
        owners = {r.get("user_id") for r in st.client.list_runs(session_id=session_id, limit=5).get("items") or []}
    except (ApiError, ServiceUnavailable):
        return ""
    owners.discard(None)
    if user_id and owners and user_id not in owners:
        return f" Ela é de {', '.join(sorted(owners))}: use -u {sorted(owners)[0]} (ou --all-users)."
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
