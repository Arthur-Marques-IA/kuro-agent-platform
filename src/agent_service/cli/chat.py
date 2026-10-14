"""`kuro chat`: conversa com um agente — mensagem única (IA/scripts) ou REPL (dev).

A sessão fica salva por agente em `~/.kuro/sessions.json` (ou `KURO_HOME`),
então mensagens seguidas continuam a mesma conversa; `--new-session` troca.
"""

import base64
import json
import mimetypes
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from agent_service.cli.client import ApiError, Client, ServiceUnavailable
from agent_service.cli.common import (
    EXIT_FAILED,
    EXIT_USAGE,
    State,
    call,
    console,
    err_console,
    fail,
    fail_from,
    parse_json_object,
    parse_pairs,
    print_json,
    state,
)

_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}


# -- sessões locais ------------------------------------------------------------


def _sessions_file() -> Path:
    return Path(os.environ.get("KURO_HOME") or Path.home() / ".kuro") / "sessions.json"


def _load_sessions() -> dict[str, str]:
    try:
        return json.loads(_sessions_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def session_for_existing(agent_type: str) -> str | None:
    """A sessão já salva desse agente (sem criar uma nova) — usada por
    `agents feedback`, que precisa de uma conversa que já aconteceu."""
    return _load_sessions().get(agent_type)


def session_for(agent_type: str, *, new: bool = False) -> str:
    sessions = _load_sessions()
    if new or agent_type not in sessions:
        sessions[agent_type] = f"cli-{uuid.uuid4().hex[:12]}"
        path = _sessions_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
        except OSError:
            pass  # sem onde salvar: a sessão vale só para esta execução
    return sessions[agent_type]


# -- anexos ------------------------------------------------------------------------


def read_attachments(st: State, paths: list[str]) -> list[dict[str, Any]]:
    """Lê cada arquivo e monta o `attachments` da API (base64 + mime type
    adivinhado pela extensão). Sai com erro de uso se algum não puder ser lido."""
    attachments: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        try:
            content = path.read_bytes()
        except OSError as exc:
            fail(st, f"não consegui ler o anexo {raw}: {exc}", EXIT_USAGE)
        attachments.append(
            {
                "content_base64": base64.b64encode(content).decode("ascii"),
                "mime_type": mimetypes.guess_type(path.name)[0],
                "filename": path.name,
            }
        )
    return attachments


# -- dependencies ----------------------------------------------------------------


def _coerce(value: Any, field_type: str) -> Any:
    if field_type == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "sim", "s", "1", "yes", "y"):
            return True
        if lowered in ("false", "nao", "não", "n", "0", "no"):
            return False
    if field_type in ("integer", "number") and isinstance(value, str):
        try:
            return _TYPES[field_type](value)
        except ValueError:
            return value
    if field_type == "string" and not isinstance(value, str):
        return json.dumps(value)  # `--dep cpf=123` chega como int: o agente espera string
    return value


def resolve_dependencies(st: State, definition: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """Converte os valores pro tipo declarado e pede os obrigatórios que faltam
    (só com TTY; sem TTY, falha listando o que falta)."""
    deps = dict(given)
    fields = definition.get("dependency_fields") or []
    for field in fields:
        if field["name"] in deps:
            deps[field["name"]] = _coerce(deps[field["name"]], field["type"])

    missing = [f for f in fields if f["required"] and deps.get(f["name"]) is None]
    if missing and not st.interactive:
        names = ", ".join(f"{f['name']} ({f['type']})" for f in missing)
        fail(st, f"dependencies obrigatórias ausentes: {names}. Use --dep nome=valor", EXIT_USAGE,
             missing=[f["name"] for f in missing])
    for field in missing:
        label = field["label"] or field["name"]
        hint = f" — {field['description']}" if field.get("description") else ""
        raw = typer.prompt(f"{label} [{field['type']}]{hint}")
        deps[field["name"]] = _coerce(raw, field["type"])
    return deps


# -- execução ------------------------------------------------------------------------


def collect_stream(
    client: Client, body: dict[str, Any], on_text: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Envia uma mensagem pelo stream e consolida o resultado — sem console nem
    código de saída, para a CLI, o servidor MCP e a TUI usarem igual.

    `error` vem preenchido se o run falhou; falha de transporte ou HTTP sobe como
    `ServiceUnavailable`/`ApiError`."""
    result: dict[str, Any] = {
        "agent_type": body["agent_type"],
        "session_id": body["session_id"],
        "run_id": None,
        "trace_id": None,
        "content": "",
        "usage": None,
        "error": None,
        "state": None,
        "sources": [],
    }
    chunks: list[str] = []
    for event, data in client.chat_stream(body):
        if event == "run":
            result["run_id"], result["trace_id"] = data.get("run_id"), data.get("trace_id")
        elif event == "message":
            text = data.get("content") or ""
            chunks.append(text)
            if on_text is not None:
                on_text(text)
        elif event == "usage":
            result["usage"] = data
        elif event == "error":
            result["error"] = data.get("message") or "falha na execução do agente"
        elif event == "state":  # só agente procedural: etapa, coletado, faltando
            result["state"] = data
        elif event == "sources":  # documentos da base que as buscas do run devolveram
            result["sources"] = data.get("sources") or []
    result["content"] = "".join(chunks)
    return result


def send(st: State, body: dict[str, Any], *, live: bool) -> dict[str, Any]:
    """Envia uma mensagem pelo stream. Com `live`, imprime o texto conforme chega.
    Devolve o resultado consolidado; `error` preenchido se o run falhou."""
    on_text = (lambda text: console.out(text, end="", highlight=False)) if live else None
    try:
        result = collect_stream(st.client, body, on_text)
    except (ServiceUnavailable, ApiError) as exc:
        fail_from(st, exc)
    if live and result["content"]:
        console.out("")
    return result


def sources_lines(sources: list[dict[str, Any]]) -> list[Any]:
    """As fontes consultadas, uma por linha: título, URL (clicável) e score. Monta
    `Text` em vez de markup: uma URL com colchetes quebraria a tag `[link=...]`."""
    from rich.style import Style
    from rich.text import Text

    lines = []
    for i, source in enumerate(sources, 1):
        title = source.get("title") or source.get("document") or source.get("content_id") or "?"
        line = Text(f"  [{i}] {title}")
        if url := source.get("source_url"):
            line.append(" — ").append(url, style=Style(link=url))
        if source.get("score") is not None:
            line.append(f" ({source['score']:.2f})", style="dim")
        lines.append(line)
    return lines


def _footer(result: dict[str, Any]) -> None:
    if result.get("sources"):
        err_console.print("[dim]fontes:[/]", highlight=False)
        for line in sources_lines(result["sources"]):
            err_console.print(line, highlight=False)
    if result.get("state"):
        from agent_service.cli.agents import procedure_line

        err_console.print(procedure_line(result["state"]), highlight=False)
    usage = result.get("usage") or {}
    parts = [f"run {result['run_id']}"]
    if usage.get("total_tokens"):
        parts.append(f"{usage['total_tokens']} tokens")
    if usage.get("duration"):
        parts.append(f"{usage['duration']:.1f}s")
    err_console.print(f"[dim]{' · '.join(parts)}[/]", highlight=False)


def chat(
    ctx: typer.Context,
    agent_type: str = typer.Argument(..., help="agent_type do agente (veja `kuro agents list`)."),
    message: str | None = typer.Option(
        None, "--message", "-m", help="Mensagem única. Sem isto: lê de stdin (se não for TTY) ou abre o REPL."
    ),
    dep: list[str] = typer.Option([], "--dep", "-d", help="Dependency nome=valor (repetível)."),
    deps_json: str | None = typer.Option(None, "--deps-json", help="Dependencies como objeto JSON."),
    session_id: str | None = typer.Option(None, "--session-id", help="Usa esta sessão em vez da salva."),
    new_session: bool = typer.Option(False, "--new-session", help="Começa uma sessão nova (e a salva)."),
    user_id: str = typer.Option("cli", "--user-id", envvar="KURO_USER_ID", help="user_id enviado ao agente."),
    attach: list[str] = typer.Option(
        [], "--attach", "-a", help="Anexa um arquivo (imagem, áudio, vídeo, PDF...) à mensagem (repetível)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Execução de teste: as tools recebem `X-Kuro-Dry-Run: true` e decidem o que simular."
    ),
) -> None:
    """Conversa com um agente. Ex.: `kuro chat conversational -m "oi" --json`."""
    st = state(ctx)
    run_chat(
        st,
        agent_type,
        message=message,
        deps={**(parse_json_object(st, deps_json, "--deps-json") if deps_json else {}), **parse_pairs(st, dep, "--dep")},
        session_id=session_id,
        new_session=new_session,
        user_id=user_id,
        attachments=read_attachments(st, attach),
        dry_run=dry_run,
    )


def stdin_is_tty() -> bool:
    """Indireção para o teste conseguir simular um terminal (o CliRunner troca sys.stdin)."""
    return sys.stdin.isatty()


def run_chat(
    st: State,
    agent_type: str,
    *,
    message: str | None = None,
    deps: dict[str, Any] | None = None,
    session_id: str | None = None,
    new_session: bool = False,
    user_id: str = "cli",
    attachments: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> None:
    definition = call(st, st.client.get_agent, agent_type)
    if message is None and not stdin_is_tty():
        message = sys.stdin.read().strip()
        if not message:
            fail(st, "mensagem vazia: use -m ou envie texto por stdin", EXIT_USAGE)

    dependencies = resolve_dependencies(st, definition, deps or {})
    session = session_id or session_for(agent_type, new=new_session)
    # No REPL os anexos vão só com a primeira mensagem — reenviá-los a cada turno
    # os repetiria no histórico da sessão (e no custo de tokens).
    pending = list(attachments or [])

    def body(text: str) -> dict[str, Any]:
        sent = list(pending)
        pending.clear()
        payload: dict[str, Any] = {
            "agent_type": agent_type,
            "user_id": user_id,
            "session_id": session,
            "message": text,
            "dependencies": dependencies or None,
        }
        if sent:
            payload["attachments"] = sent
        if dry_run:
            payload["dry_run"] = True
        return payload

    if message is not None:
        result = send(st, body(message), live=not st.json_mode)
        if st.json_mode:
            print_json(result)
        elif not result["error"]:
            _footer(result)
        if result["error"]:
            fail(st, result["error"], EXIT_FAILED, run_id=result["run_id"])
        return

    if not st.interactive:
        fail(st, "sem TTY: passe a mensagem com -m ou por stdin", EXIT_USAGE)

    console.print(
        f"[bold]{definition['name']}[/] [dim]({agent_type} · prompt v{definition['prompt_version']} · sessão {session})[/]\n"
        "[dim]/nova = nova sessão · /sair = voltar[/]"
    )
    while True:
        try:
            text = console.input("[bold cyan]você>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not text:
            continue
        if text in ("/sair", "/exit", "/q"):
            return
        if text == "/nova":
            session = session_for(agent_type, new=True)
            console.print(f"[dim]nova sessão: {session}[/]")
            continue
        if text.startswith("/"):  # não manda um comando errado como mensagem pro agente
            err_console.print(
                f"[yellow]aqui dentro do chat só valem /nova e /sair[/] — {text} é comando do shell `kuro`.\n"
                "[dim]Saia com /sair e rode-o lá. Para enviar isso ao agente, escreva sem a barra.[/]"
            )
            continue
        console.print("[bold magenta]agente>[/] ", end="")
        try:
            result = send(st, body(text), live=True)
        except KeyboardInterrupt:
            console.print("\n[dim](interrompido)[/]")
            continue
        except typer.Exit:
            continue  # erro já impresso; o REPL segue
        if result["error"]:
            err_console.print(f"[bold red]erro:[/] {result['error']}")
        else:
            _footer(result)
