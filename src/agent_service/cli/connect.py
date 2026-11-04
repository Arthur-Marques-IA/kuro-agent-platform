"""`kuro connect`: conecta o servidor MCP do Kuro aos clientes de IA desta máquina pelo SSH.

A lógica toda mora em `agent_service.kuro_connect`, que só usa a biblioteca padrão para rodar
também como script avulso (`uv run <url do arquivo> root@servidor`), sem instalar o Kuro. Aqui
fica só a casca da CLI: as opções do Typer, a saída com o Rich e o `--json` de sempre.
"""

import typer
from rich.markup import escape

from agent_service import kuro_connect as core
from agent_service.cli.common import console, emit, err_console, fail, state


class RichUi(core.Ui):
    def step(self, text: str) -> None:
        if not self.quiet:
            console.print(f"[green]✓[/] {escape(text)}", highlight=False)

    def info(self, text: str) -> None:
        if not self.quiet:
            console.print(text, highlight=False, markup=False)

    def confirm(self, question: str, default: bool = True) -> bool:
        return typer.confirm(question, default=default)


def connect(
    ctx: typer.Context,
    destination: str = typer.Argument(..., help="O servidor, como no ssh: usuario@host ou um alias do ~/.ssh/config."),
    port: int | None = typer.Option(None, "--port", "-p", help="Porta do SSH (padrão: 22 ou a do ~/.ssh/config)."),
    identity: str | None = typer.Option(None, "--identity", "-i", help="Arquivo da chave SSH."),
    ssh_option: list[str] = typer.Option([], "--ssh-option", "-o", help="Opção extra do ssh, como em `ssh -o` (repetível)."),
    container: str | None = typer.Option(None, "--container", help="Container do agent-service (padrão: o único rodando)."),
    name: str = typer.Option("kuro", "--name", help="Nome do servidor MCP nos clientes."),
    scope: str = typer.Option("user", "--scope", "-s", help="Escopo do registro no Claude Code: user, project ou local."),
    client: list[str] = typer.Option(
        [], "--client", help=f"Registra só neste cliente ({', '.join(core.CLIENT_KEYS)}; repetível). Padrão: todos os instalados."
    ),
    print_only: bool = typer.Option(False, "--print", help="Não registra: só testa e mostra a configuração."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Troca um registro com o mesmo nome sem perguntar."),
    passphrase: bool = typer.Option(
        False, "--passphrase", help="Se precisar criar a chave SSH, cria com senha de proteção e a carrega no ssh-agent."
    ),
) -> None:
    """Conecta o servidor MCP do Kuro ao Claude Code, Claude Desktop, Cursor e VS Code pelo SSH —
    sem porta aberta, sem certificado e sem a chave de API na sua máquina. Sem o Kuro instalado,
    o mesmo comando roda como script: `uv run <url do kuro_connect.py> root@meu-servidor`."""
    st = state(ctx)
    unknown = [c for c in client if c not in core.CLIENT_KEYS]
    if unknown:
        fail(st, f"cliente desconhecido: {', '.join(unknown)} (use {', '.join(core.CLIENT_KEYS)})", core.EXIT_USAGE)
    opts = core.Options(destination, port, identity, list(ssh_option), container, name, scope, list(client), print_only, yes, passphrase)
    try:
        report = core.connect(opts, RichUi(st.interactive, quiet=st.json_mode))
    except core.StepFailed as exc:
        fail(st, str(exc), exc.code, steps=exc.steps)

    def render(r: dict) -> None:
        for style, text in core.summary(r, name, print_only):
            if style == "warn":
                err_console.print(f"[yellow]aviso:[/] {escape(text)}", highlight=False)
            elif style == "title":
                console.print(f"[bold]{escape(text)}[/]", highlight=False)
            else:
                console.print(text, markup=False, highlight=False, soft_wrap=True)

    emit(st, report, render)
