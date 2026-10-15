"""`kuro analyze`: roda um agente `kind="analysis"` sobre um documento,
one-shot — sem sessão, devolve o objeto estruturado (response_schema).
"""

import json
import sys
from typing import Any

import typer

from agent_service.cli.chat import read_attachments, resolve_dependencies, sources_lines, stdin_is_tty
from agent_service.cli.common import EXIT_USAGE, call, console, emit, err_console, fail, parse_pairs, state

DEFAULT_ATTACHMENT_PROMPT = "Analise o(s) anexo(s)."


def analyze(
    ctx: typer.Context,
    agent_type: str = typer.Argument(..., help="agent_type do agente analista (kind='analysis')."),
    text: str | None = typer.Option(
        None, "--text", "-m", help="O documento como texto, direto na linha de comando."
    ),
    file: str | None = typer.Option(
        None, "--file", "-f", help="Arquivo com o documento (qualquer texto, inclusive .json). Sem isto: lê de stdin."
    ),
    attach: list[str] = typer.Option(
        [], "--attach", "-a", help="Anexa um arquivo (PDF, imagem, áudio...) direto ao modelo (repetível)."
    ),
    dep: list[str] = typer.Option([], "--dep", "-d", help="Dependency nome=valor (repetível)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Execução de teste: as tools recebem `X-Kuro-Dry-Run: true` e decidem o que simular."
    ),
) -> None:
    """Analisa um documento com um agente one-shot. Ex.: `kuro analyze extrator -f contrato.txt --json`,
    `kuro analyze classificador -m '{"mensagens": [...]}'` ou, com PDF direto,
    `kuro analyze extrator -a contrato.pdf --json`."""
    st = state(ctx)
    if text is not None and file:
        fail(st, "use --text ou --file, não os dois", EXIT_USAGE)
    definition = call(st, st.client.get_agent, agent_type)
    if definition.get("kind") != "analysis":
        fail(st, f"{agent_type!r} não é um agente 'analysis' (kind={definition.get('kind')!r})", EXIT_USAGE)

    attachments = read_attachments(st, attach)

    document = ""
    if text is not None:
        document = text
    elif file:
        try:
            document = open(file, encoding="utf-8").read()
        except OSError as exc:
            fail(st, f"não consegui ler {file}: {exc}", EXIT_USAGE)
    elif not stdin_is_tty():
        document = sys.stdin.read()
    elif not attachments:
        fail(st, "sem documento: use --text/-m, --file/-f, --attach/-a ou envie por stdin", EXIT_USAGE)

    if not document.strip():
        if not attachments:
            fail(st, "documento vazio", EXIT_USAGE)
        document = DEFAULT_ATTACHMENT_PROMPT

    dependencies = resolve_dependencies(st, definition, parse_pairs(st, dep, "--dep"))
    body: dict[str, Any] = {"agent_type": agent_type, "document": document, "dependencies": dependencies or None}
    if attachments:
        body["attachments"] = attachments
    if dry_run:
        body["dry_run"] = True
    result = call(st, st.client.analyze, body)
    emit(st, result, _render)


def _render(result: dict[str, Any]) -> None:
    console.print_json(json.dumps(result["result"], ensure_ascii=False))
    if result.get("sources"):
        err_console.print("[dim]fontes:[/]", highlight=False)
        for line in sources_lines(result["sources"]):
            err_console.print(line, highlight=False)
