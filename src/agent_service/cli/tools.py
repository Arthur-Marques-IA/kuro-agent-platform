"""`kuro tools`: listar, inspecionar e invocar tools sem montar um agente."""

import json
from typing import Any

import questionary
import typer
from rich.table import Table

from agent_service.cli.agents import BACK
from agent_service.cli.common import (
    EXIT_FAILED,
    EXIT_USAGE,
    State,
    call,
    console,
    emit,
    fail,
    parse_json_object,
    parse_pairs,
    print_json,
    read_json_file,
    state,
)

app = typer.Typer(help="Tools: listar, ver, criar/editar (apply), invocar, remover.")

# `kind` fica de fora: mudar o tipo de uma tool existente é criar outra tool.
EDITABLE_FIELDS = ("label", "description", "config", "enabled", "side_effect")


def _efeito(t: dict[str, Any]) -> str:
    """Efeito colateral para a tabela: o valor, ou "não classificada" com a sugestão pelo método HTTP."""
    if t.get("side_effect") is not None:
        return "sim" if t["side_effect"] else "não"
    suggested = t.get("side_effect_suggested")
    hint = f" [dim](sugestão: {'sim' if suggested else 'não'})[/]" if suggested is not None else ""
    return f"[yellow]não classificada[/]{hint}"


def _print_warnings(t: dict[str, Any]) -> None:
    for warning in t.get("warnings") or []:
        console.print(f"[yellow]![/] {warning}", highlight=False)


def _render_list(tools: list[dict[str, Any]]) -> None:
    table = Table(show_edge=False, header_style="bold")
    for column in ("tool_name", "kind", "label", "ativa", "efeito colateral"):
        table.add_column(column)
    for t in tools:
        seed = " [dim](seed)[/]" if t["is_seed"] else ""
        efeito = _efeito(t)
        table.add_row(
            f"[cyan]{t['tool_name']}[/]{seed}", t["kind"], t["label"], "sim" if t["enabled"] else "[red]não[/]", efeito
        )
    console.print(table)


def _render_tool(t: dict[str, Any]) -> None:
    console.print(f"[bold]{t['label']}[/] [dim]({t['tool_name']} · {t['kind']})[/]")
    if t.get("description"):
        console.print(t["description"])
    if t.get("functions"):
        console.print(f"funções: {', '.join(t['functions'])}")
    if t.get("build_error"):
        console.print(f"[red]não constrói:[/] {t['build_error']}")
    console.print_json(json.dumps(t["config"], ensure_ascii=False, default=str))


def _render_invoke(result: dict[str, Any]) -> None:
    value = result["result"]
    if isinstance(value, str):
        console.print(value, highlight=False)
    else:
        console.print_json(json.dumps(value, ensure_ascii=False, default=str))


@app.callback(invoke_without_command=True)
def tools(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    st = state(ctx)
    if st.interactive:
        browse(st)
    else:
        list_tools(ctx)


@app.command("list")
def list_tools(ctx: typer.Context) -> None:
    """Lista as tools cadastradas."""
    st = state(ctx)
    emit(st, call(st, st.client.list_tools), _render_list)


@app.command("get")
def get_tool(
    ctx: typer.Context,
    tool_name: str,
    as_editable: bool = typer.Option(
        False, "--editable", help="Só os campos editáveis, em JSON — pronto para `apply -f`."
    ),
) -> None:
    """Mostra uma tool (segredos mascarados) e, para builtins, as funções expostas."""
    st = state(ctx)
    detail = call(st, st.client.get_tool, tool_name)
    if as_editable:
        # Os segredos saem mascarados; devolvê-los assim no `apply` preserva o
        # valor guardado (a API restaura quando o campo volta como a máscara).
        emit(
            st,
            {"tool_name": detail["tool_name"], "kind": detail["kind"], **{k: detail.get(k) for k in EDITABLE_FIELDS}},
        )
    else:
        emit(st, detail, _render_tool)


@app.command("apply")
def apply_tool(
    ctx: typer.Context,
    file: str = typer.Option(..., "--file", "-f", help="JSON da tool (`-` = stdin). Precisa de tool_name e kind."),
) -> None:
    """Cria a tool se não existir, senão atualiza só os campos do arquivo."""
    st = state(ctx)
    body = read_json_file(st, file)
    tool_name = body.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        fail(st, "o arquivo precisa do campo tool_name", EXIT_USAGE)

    existing = {t["tool_name"]: t for t in call(st, st.client.list_tools)}
    if tool_name in existing:
        # `kind` pode vir no arquivo (é o que `get --editable` devolve), mas mudá-lo
        # seria outra tool: ignorar em silêncio esconderia a edição de quem operou.
        kind = body.get("kind")
        if kind is not None and kind != existing[tool_name]["kind"]:
            fail(
                st,
                f"{tool_name} é kind={existing[tool_name]['kind']!r} e o kind de uma tool não muda — "
                f"crie outra tool para kind={kind!r}",
                EXIT_USAGE,
            )
        changes = {k: v for k, v in body.items() if k not in ("tool_name", "kind")}
        unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
        if unknown:
            fail(st, f"campos não editáveis: {', '.join(unknown)} (use: {', '.join(EDITABLE_FIELDS)})", EXIT_USAGE)
        result, action = call(st, st.client.update_tool, tool_name, changes), "atualizada"
    else:
        if not body.get("kind"):
            fail(st, "tool nova precisa do campo kind (builtin, api ou python)", EXIT_USAGE)
        result, action = call(st, st.client.create_tool, body), "criada"
    emit(
        st,
        result,
        lambda t: (console.print(f"[green]✓[/] tool {t['tool_name']} {action} ({t['kind']})"), _print_warnings(t)),
    )


@app.command("set")
def set_fields(
    ctx: typer.Context,
    tool_name: str,
    pairs: list[str] = typer.Argument(
        ..., help='campo=valor (valor em JSON quando possível). Ex.: enabled=false label="Busca"'
    ),
) -> None:
    """Altera só os campos informados de uma tool."""
    st = state(ctx)
    changes = parse_pairs(st, pairs, "campo")
    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        fail(st, f"campos não editáveis: {', '.join(unknown)} (use: {', '.join(EDITABLE_FIELDS)})", EXIT_USAGE)
    result = call(st, st.client.update_tool, tool_name, changes)
    emit(
        st,
        result,
        lambda t: (console.print(f"[green]✓[/] tool {t['tool_name']} atualizada"), _print_warnings(t)),
    )


def unclassified_report(tools: list[dict[str, Any]], agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """As tools sem `side_effect` e os agentes que as usam, as mais usadas primeiro."""
    rows = [
        {
            "tool_name": t["tool_name"],
            "kind": t["kind"],
            "enabled": t["enabled"],
            "side_effect_suggested": t.get("side_effect_suggested"),
            "agents": sorted(a["agent_type"] for a in agents if t["tool_name"] in (a.get("tools") or [])),
        }
        for t in tools
        if t.get("side_effect") is None
    ]
    return sorted(rows, key=lambda r: (-len(r["agents"]), r["tool_name"]))


def _render_audit(report: dict[str, Any]) -> None:
    rows = report["unclassified"]
    if not rows:
        console.print("[green]✓[/] todas as tools estão classificadas quanto a efeito colateral")
        return
    table = Table(show_edge=False, header_style="bold")
    for column in ("tool_name", "kind", "sugestão", "usada por"):
        table.add_column(column)
    for r in rows:
        suggested = r["side_effect_suggested"]
        table.add_row(
            f"[cyan]{r['tool_name']}[/]",
            r["kind"],
            "—" if suggested is None else ("sim (muda algo)" if suggested else "não (só lê)"),
            ", ".join(r["agents"]) or "[dim]nenhum agente[/]",
        )
    console.print(table)
    console.print("[dim]classifique com: kuro tools set <tool> side_effect=true|false[/]")


@app.command("audit")
def audit(
    ctx: typer.Context,
    strict: bool = typer.Option(False, "--strict", help="Sai com 1 se alguma tool estiver sem classificação."),
) -> None:
    """Lista as tools sem classificação de efeito colateral e os agentes que as usam.

    `null` em `side_effect` não é erro, mas tem consequência: a fila de revisão (`--side-effect`)
    e o selo da sessão tratam a tool como sem efeito, e um agente procedural a aceita em `tools`.
    A sugestão vem do método HTTP (só tools `api`) e nunca é gravada sozinha."""
    st = state(ctx)
    tools = call(st, st.client.list_tools)
    agents = call(st, st.client.list_agents)
    rows = unclassified_report(tools, agents)
    emit(st, {"unclassified": rows, "total_tools": len(tools)}, _render_audit)
    if strict and rows:
        raise typer.Exit(EXIT_FAILED)


@app.command("delete")
def delete_tool(
    ctx: typer.Context,
    tool_name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação (obrigatório sem TTY)."),
) -> None:
    """Remove uma tool (tools seed, ou em uso por algum agente, não saem)."""
    st = state(ctx)
    if not yes:
        if not st.interactive:
            fail(st, "confirme com --yes para remover sem TTY", EXIT_USAGE)
        if not typer.confirm(f"Remover a tool {tool_name!r}?"):
            raise typer.Exit()
    call(st, st.client.delete_tool, tool_name)
    emit(st, {"deleted": tool_name}, lambda _: console.print(f"[green]✓[/] tool {tool_name} removida"))


@app.command("catalog")
def catalog(ctx: typer.Context) -> None:
    """Catálogo de toolkits builtin disponíveis e seus parâmetros."""
    st = state(ctx)
    emit(
        st,
        call(st, st.client.tool_catalog),
        lambda entries: [
            console.print(f"[cyan]{e['builtin_id']}[/] — {e['label']}: {e['description']}") for e in entries
        ],
    )


@app.command("invoke")
def invoke(
    ctx: typer.Context,
    tool_name: str,
    arg: list[str] = typer.Option(
        [], "--arg", "-a", help="Argumento nome=valor (repetível; valor em JSON quando possível)."
    ),
    args_json: str | None = typer.Option(None, "--args-json", help="Argumentos como objeto JSON."),
    dep: list[str] = typer.Option(
        [],
        "--dep",
        "-d",
        help="Simula o `dependencies` do /chat (nome=valor, repetível) — é o que preenche "
        "um parâmetro com source='dependency'.",
    ),
    function: str | None = typer.Option(None, "--fn", help="Função da toolkit (obrigatório para kind=builtin)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Execução de teste: as tools recebem `X-Kuro-Dry-Run: true` e decidem o que simular."
    ),
) -> None:
    """Executa a tool isoladamente. Sai com código 1 se a tool falhar.

    Um parâmetro `source="dependency"` não vem dos argumentos: no `/chat` ele é
    injetado pelo servidor a partir de `dependencies`. Use `-d` para testá-lo
    sem montar um agente."""
    st = state(ctx)
    arguments = {
        **(parse_json_object(st, args_json, "--args-json") if args_json else {}),
        **parse_pairs(st, arg, "--arg"),
    }
    _invoke(st, tool_name, arguments, function, parse_pairs(st, dep, "--dep"), dry_run=dry_run)


def _invoke(
    st: State,
    tool_name: str,
    arguments: dict[str, Any],
    function: str | None,
    dependencies: dict[str, Any] | None = None,
    *,
    dry_run: bool = False,
) -> None:
    result = call(st, st.client.invoke_tool, tool_name, arguments, function, dependencies, dry_run)
    if st.json_mode:
        print_json(result)
    if not result["ok"]:  # a API responde 200 com ok=false quando a tool em si falha
        tipo = f" [{result['failure']}]" if result.get("failure") else ""
        fail(st, f"{result.get('error') or 'a tool falhou'}{tipo}", EXIT_FAILED)
    if not st.json_mode:
        _render_invoke(result)


def browse(st: State) -> None:
    while True:
        rows = call(st, st.client.list_tools)
        choices = [
            questionary.Choice(f"{t['tool_name']}  —  {t['label']} ({t['kind']})", value=t["tool_name"]) for t in rows
        ]
        choices.append(questionary.Choice("← sair", value=BACK))
        tool_name = questionary.select("Tool:", choices=choices).ask()
        if tool_name in (None, BACK):
            return
        try:
            detail = call(st, st.client.get_tool, tool_name)
            _render_tool(detail)
            if not questionary.confirm("Invocar agora?", default=True).ask():
                continue
            function = None
            if detail.get("functions"):
                function = questionary.select("Função:", choices=detail["functions"]).ask()
            raw = questionary.text("Argumentos (JSON):", default="{}").ask()
            if raw is None:
                continue
            _invoke(st, tool_name, parse_json_object(st, raw, "argumentos"), function)
        except typer.Exit:
            continue
