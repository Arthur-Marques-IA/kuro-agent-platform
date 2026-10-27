"""Entrada da CLI `kuro` (ver `[project.scripts]` no pyproject).

`kuro` sem argumentos, com TTY, abre um shell: `/agents`, `/tools`,
`/chat <agente>`, `/runs`... — cada linha é o mesmo comando da CLI (a `/`
é opcional), então tudo que funciona no shell funciona em um script.
"""

import json
import os
import shlex
import sys
from typing import Any

import click
import typer
from rich.table import Table

from agent_service.cli import agents, collections, jobs, runs, sessions, tools
from agent_service.cli.analyze import analyze
from agent_service.cli.chat import chat
from agent_service.cli.connect import connect
from agent_service.cli.eval import eval_command
from agent_service.cli.client import ApiError, Client, ServiceUnavailable
from agent_service.cli.common import (
    EXIT_FAILED,
    EXIT_USAGE,
    EXIT_UNAVAILABLE,
    State,
    call,
    console,
    emit,
    err_console,
    fail,
    print_json,
    state,
)
from agent_service.cli.connection import DEFAULT_URL, resolve_api_key
from agent_service.config import current_key
from agent_service.public_url import LOOPBACK, ip_https, public_url

app = typer.Typer(
    name="kuro",
    help="Opera o agent-service pelo terminal. `--json` em qualquer comando para saída de máquina.",
    no_args_is_help=False,
    rich_markup_mode="markdown",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(agents.app, name="agents")
app.add_typer(tools.app, name="tools")
app.add_typer(runs.app, name="runs")
app.add_typer(collections.app, name="collections")
app.add_typer(sessions.app, name="sessions")
app.add_typer(jobs.app, name="jobs")
app.command("chat")(chat)
app.command("analyze")(analyze)
app.command("eval")(eval_command)
app.command("connect")(connect)

providers_app = typer.Typer(help="Provedores de modelo (LLM) suportados.")
app.add_typer(providers_app, name="providers")
credentials_app = typer.Typer(help="Credenciais (chaves) de modelo: listar e testar.")
app.add_typer(credentials_app, name="credentials")


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    url: str = typer.Option(
        DEFAULT_URL, "--url", envvar=["KURO_API_URL", "AGENT_SERVICE_URL"], help="URL do agent-service."
    ),
    json_mode: bool = typer.Option(False, "--json", envvar="KURO_JSON", help="Saída JSON (stdout) e erros JSON (stderr)."),
    no_input: bool = typer.Option(
        False, "--no-input", envvar="KURO_NO_INPUT", help="Nunca pergunta nada (falha se faltar argumento)."
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="Timeout das requisições, em segundos."),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="KURO_API_KEY",
        help="Chave de API do serviço (escopo admin). Normalmente vem de KURO_API_KEY; sem ela, "
        "num serviço local, da ADMIN_API_KEY do .env da pasta atual.",
    ),
    ca_bundle: str | None = typer.Option(
        None,
        "--ca-bundle",
        envvar="KURO_CA_BUNDLE",
        help="Arquivo .pem da CA que assinou o certificado do serviço (rede interna com CA própria).",
    ),
    insecure: bool = typer.Option(
        False, "--insecure", help="Não valida o certificado TLS. Só para teste local — nunca em produção."
    ),
) -> None:
    if insecure:
        # Uma flag cujo propósito é ser insegura não pode passar despercebida:
        # sem aviso ela acaba num perfil de shell ou script de CI e ninguém nota.
        # Vai para o stderr, então não suja a saída de dados do `--json`.
        err_console.print(
            "[yellow]aviso:[/] --insecure — o certificado TLS não está sendo validado. "
            "Só para teste local; em produção use --ca-bundle.",
            highlight=False,
        )
    api_key = resolve_api_key(api_key, url)
    ctx.obj = State(
        client=Client(url, timeout=timeout, api_key=api_key, verify=False if insecure else (ca_bundle or True)),
        json_mode=json_mode,
        no_input=no_input,
    )
    if ctx.invoked_subcommand is None:
        if ctx.obj.interactive:
            shell(ctx)
        else:
            console.print(ctx.get_help())


# -- health ----------------------------------------------------------------------


MCP_SOURCE = "git+https://github.com/Arthur-Marques-IA/microservice_agents"
"""De onde o `uvx` instala o `kuro-mcp` na máquina de quem opera — sem clone.
Troque com `--source` ou `KURO_MCP_SOURCE` (um fork, uma tag: `...@v0.3.0`)."""


def _mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "…"


_LOOPBACK = LOOPBACK
_ip_https = ip_https


def _public_url(explicit: str | None, internal: str) -> tuple[str, str]:
    """O endereço do serviço visto da máquina de quem opera, e de onde ele saiu
    (ver `agent_service.public_url`)."""
    return public_url(explicit, internal)


def _ca_file_name(url: str) -> str:
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]")
    return "kuro-ca-" + "".join(c if c.isalnum() else "-" for c in host) + ".pem"


@app.command("mcp-config")
def mcp_config(
    ctx: typer.Context,
    public_url: str | None = typer.Option(
        None,
        "--url",
        envvar="KURO_PUBLIC_URL",
        help="Endereço do serviço como a máquina de quem opera o enxerga (ex.: https://kuro.empresa.com).",
    ),
    show_key: bool = typer.Option(False, "--show-key", help="Mostra a chave inteira (senão ela sai mascarada)."),
    source: str = typer.Option(MCP_SOURCE, "--source", envvar="KURO_MCP_SOURCE", help="De onde o uvx instala o kuro-mcp."),
) -> None:
    """Como conectar o servidor MCP do Kuro a este serviço, pronto para copiar. Rode no servidor:
    `docker compose exec agent-service kuro mcp-config --show-key`.

    Primeiro o caminho pelo SSH (`kuro connect`), que não precisa de porta aberta; com um
    domínio (`KURO_API_DOMAIN`, profile `tls`), o MCP por URL em `/mcp`; e o `kuro-mcp` local
    apontado para a URL (o domínio ou, sem domínio, o HTTPS por IP do profile `ip`, com o
    certificado da CA para salvar). `--url` sobrepõe o endereço."""
    st = state(ctx)
    key = current_key(resolve_api_key(ctx.find_root().params.get("api_key"), st.client.base_url)) or ""
    url, url_source = _public_url(public_url, st.client.base_url)
    ip = _ip_https() if url_source == "ip" else None
    warnings = []
    if url_source == "local":
        warnings.append(
            "Sem domínio (KURO_API_DOMAIN) e sem o profile `ip`, o serviço só escuta na própria máquina: "
            "conecte pelo SSH (`kuro connect`, abaixo), que não precisa de porta aberta. A URL abaixo só "
            "serve para um MCP rodando aqui; para expô-la, `docker compose --profile ip up -d`."
        )
    elif url.startswith("http://") and not any(host in url for host in _LOOPBACK):
        warnings.append(
            f"{url} é HTTP: a chave de API e as mensagens trafegam em texto claro. Prefira o profile `ip` "
            "(HTTPS pelo IP) ou um domínio com o profile `tls`."
        )
    if not key:
        warnings.append(
            "Sem chave de API: com a autenticação ligada, o MCP vai receber 401. Rode com KURO_API_KEY "
            "(a ADMIN_API_KEY) definida — dentro do container ela já vem configurada."
        )
    shown = key if show_key else (_mask(key) if key else "")
    package = f"agent-service[mcp] @ {source}"
    args = ["--from", package, "kuro-mcp"]
    key_flag = f" --env KURO_API_KEY={shown}" if key else ""
    tail = f'{key_flag} -- uvx --from "{package}" kuro-mcp'
    env = {"KURO_API_URL": url, **({"KURO_API_KEY": "${KURO_API_KEY}"} if key else {})}

    ca_file = setup = None
    if ip:
        ca_file = _ca_file_name(url)
        pem = ip[1]
        setup = {
            "bash": f"mkdir -p ~/.kuro && cat > ~/.kuro/{ca_file} <<'EOF'\n{pem}\nEOF",
            "powershell": (
                'New-Item -ItemType Directory -Force "$HOME\\.kuro" | Out-Null\n'
                f"@'\n{pem}\n'@ | Set-Content -Encoding ascii \"$HOME\\.kuro\\{ca_file}\""
            ),
        }
        claude_cmd = f'claude mcp add kuro -s user --env KURO_API_URL={url} --env KURO_CA_BUNDLE="$HOME/.kuro/{ca_file}"{tail}'
        claude_ps = f'claude mcp add kuro -s user --env KURO_API_URL={url} --env KURO_CA_BUNDLE="$HOME\\.kuro\\{ca_file}"{tail}'
        env["KURO_CA_BUNDLE"] = "${KURO_CA_BUNDLE}"
    else:
        claude_cmd = claude_ps = f"claude mcp add kuro -s user --env KURO_API_URL={url}{tail}"

    # Pelo SSH: o host público, quando se sabe qual é; o usuário do SSH, não (o do container não é).
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0] if url_source in ("domain", "ip", "explicit") else ""
    ssh_cmd = f'uvx --from "{package}" kuro connect usuario@{host or "servidor"}'
    # Por URL só com certificado público: o Claude Code (Node) não confia na CA própria do
    # profile `ip`, e HTTP puro levaria a chave em texto claro.
    http_cmd = None
    if url_source == "domain" or (url_source == "explicit" and url.startswith("https://")):
        auth_header = f' --header "Authorization: Bearer {shown}"' if key else ""
        http_cmd = f"claude mcp add --transport http kuro {url}/mcp -s user{auth_header}"

    report = {
        "ssh_command": ssh_cmd,
        "http_command": http_cmd,
        "url": url,
        "url_source": url_source,
        "api_key": shown or None,
        "api_key_masked": bool(key) and not show_key,
        "ca_file": f"~/.kuro/{ca_file}" if ca_file else None,
        "ca_pem": ip[1] if ip else None,
        "setup": setup,
        "claude_command": claude_cmd,
        "claude_command_powershell": claude_ps,
        "mcp_json": {"mcpServers": {"kuro": {"command": "uvx", "args": args, "env": env}}},
        "requires": "uv na máquina de quem opera (https://docs.astral.sh/uv/) e acesso de leitura ao repositório",
        "warnings": warnings,
    }

    def render(r: dict[str, Any]) -> None:
        for w in r["warnings"]:
            err_console.print(f"[yellow]aviso:[/] {w}", highlight=False)
        # markup=False em tudo que é comando: o Rich leria `[mcp]` (de `agent-service[mcp]`)
        # como marcação e o apagaria.
        console.print("[bold]Servidor MCP do Kuro[/] — rode na sua máquina.\n")
        console.print(
            "[bold]Pelo SSH[/] (recomendado: não precisa de porta aberta, de certificado nem da chave na sua "
            "máquina; precisa do uv). Troque `usuario` pelo seu usuário do SSH:"
        )
        console.print(r["ssh_command"], markup=False, highlight=False, soft_wrap=True)
        if r["http_command"]:
            console.print("\n[bold]Ou pela URL[/] (sem instalar nada; o serviço atende o MCP em /mcp):")
            console.print(r["http_command"], markup=False, highlight=False, soft_wrap=True)
        console.print(
            "\n[bold]Ou com o kuro-mcp na sua máquina[/], falando com a URL"
            + (" (a porta precisa estar aberta no firewall do provedor):" if r["setup"] else ":")
        )
        step = 1
        if r["setup"]:
            console.print(
                f"[bold]{step}. Salve o certificado da CA deste servidor[/] (uma vez; é o que faz o HTTPS pelo IP "
                "ser confiável). Ele veio pela sua sessão no servidor, então é o autêntico."
            )
            console.print("\n[dim]bash / zsh (Linux, macOS):[/]")
            console.print(r["setup"]["bash"], markup=False, highlight=False, soft_wrap=True)
            console.print("\n[dim]PowerShell (Windows):[/]")
            console.print(r["setup"]["powershell"], markup=False, highlight=False, soft_wrap=True)
            step += 1
            console.print(f"\n[bold]{step}. Registre no Claude Code[/]:")
            console.print("\n[dim]bash / zsh (Linux, macOS):[/]")
            console.print(r["claude_command"], markup=False, highlight=False, soft_wrap=True)
            console.print("\n[dim]PowerShell (Windows):[/]")
            console.print(r["claude_command_powershell"], markup=False, highlight=False, soft_wrap=True)
        else:
            console.print(f"[bold]{step}. Claude Code[/] (um comando, no terminal):")
            console.print(r["claude_command"], markup=False, highlight=False, soft_wrap=True)
        if r["api_key_masked"]:
            console.print("[dim]A chave está mascarada: rode de novo com --show-key para copiá-la.[/]")
        extra = " e KURO_CA_BUNDLE (o caminho do certificado salvo)" if r["setup"] else ""
        console.print(
            f"\n[bold]Ou um .mcp.json[/] no projeto (a chave fica fora do arquivo; defina KURO_API_KEY{extra} no ambiente):"
        )
        console.print_json(json.dumps(r["mcp_json"]))
        console.print("\n[dim]Confira no Claude Code com /mcp, e peça \"use a tool health do kuro\". Guia: docs/mcp.md[/]")

    emit(st, report, render)


@app.command("health")
def health(ctx: typer.Context) -> None:
    """Diagnóstico: serviço no ar, autenticação, Langfuse, tools Python e provedores configurados."""
    st = state(ctx)
    report: dict[str, Any] = {"url": st.client.base_url}
    report["service"] = call(st, st.client.health)
    checks = {
        "observability": st.client.observability_config,
        "python_tools": st.client.python_tools_config,
        "credentials": st.client.list_credentials,
    }
    for key, fn in checks.items():
        try:
            report[key] = fn()
        except ServiceUnavailable as exc:
            fail(st, str(exc), EXIT_UNAVAILABLE)
        except ApiError as exc:
            report[key] = {"error": exc.detail, "status": exc.status}
    if isinstance(report["credentials"], list):
        report["credentials"] = [
            {k: c.get(k) for k in ("id", "provider", "label", "enabled", "configured", "last_test_ok")}
            for c in report["credentials"]
        ]

    def render(r: dict[str, Any]) -> None:
        versao = r["service"].get("version")
        console.print(f"[green]●[/] agent-service em {r['url']}" + (f" [dim](v{versao})[/]" if versao else ""))
        fechado = r["service"].get("auth") == "enabled"
        console.print(
            f"{'[green]●[/]' if fechado else '[red]●[/]'} autenticação "
            + (
                "exigida"
                if fechado
                else "[red]desligada — quem alcança esta porta lê as conversas e edita os "
                "prompts[/] (defina ADMIN_API_KEY e RUNTIME_API_KEY)"
            )
        )
        cifra = r["service"].get("model_credentials")
        if cifra is not None:
            ok = cifra == "enabled"
            console.print(
                f"{'[green]●[/]' if ok else '[yellow]●[/]'} cadastro de chaves de modelo "
                + ("ligado" if ok else f"[yellow]{cifra}[/] (gere com `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`)")
            )
        obs = r["observability"]
        recording = obs.get("enabled")
        on = obs.get("langfuse", recording)
        console.print(
            f"{'[green]●[/]' if recording else '[yellow]●[/]'} execuções "
            + ("registradas — `kuro runs` tem dados" if recording else "não registradas — `kuro runs` não terá dados")
        )
        console.print(f"{'[green]●[/]' if on else '[dim]●[/]'} Langfuse {'ligado (exportador)' if on else 'desligado'}")
        py = r["python_tools"].get("enabled")
        console.print(f"{'[green]●[/]' if py else '[dim]●[/]'} tools Python {'ligadas' if py else 'desligadas'}")
        if isinstance(r["credentials"], list):
            ready = sorted({c["provider"] for c in r["credentials"] if c["enabled"] and c["configured"]})
            hint = "" if ready else " (google ainda usa GOOGLE_API_KEY do .env, se houver)"
            console.print(f"{'[green]●[/]' if ready else '[yellow]●[/]'} provedores com credencial ativa: {', '.join(ready) or 'nenhum'}{hint}")

    emit(st, report, render)


# -- dash --------------------------------------------------------------------------


@app.command("dash")
def dash(
    ctx: typer.Context,
    agent: str | None = typer.Option(None, "--agent", "-a", help="Começa filtrado neste agente."),
    interval: float = typer.Option(3.0, "--interval", min=1.0, help="Segundos entre as atualizações das execuções."),
) -> None:
    """Painel em tela cheia: execuções ao vivo (enter abre o trace) e o panorama. Precisa do extra `tui`."""
    st = state(ctx)
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or st.json_mode:
        fail(st, "dash é uma tela interativa e precisa de TTY; sem TTY use `kuro runs tail --json`", EXIT_USAGE)
    try:
        from agent_service.tui.app import KuroDash, prefer_truecolor
    except ImportError:
        fail(st, "kuro dash precisa do extra tui: `uv sync --extra tui` (ou `uv run --extra tui kuro dash`)", EXIT_USAGE)
    prefer_truecolor()  # antes de criar o app: é na criação que o Rich escolhe a paleta
    KuroDash(st.client, interval=interval, agent=agent).run()


# -- providers / credentials -----------------------------------------------------


@providers_app.callback(invoke_without_command=True)
def providers(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        list_providers(ctx)


@providers_app.command("models")
def provider_models(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="google | openai | anthropic | ollama"),
    credential_id: str | None = typer.Option(None, "--credential", help="Credencial a usar; sem isto, a padrão do provedor."),
    refresh: bool = typer.Option(False, "--refresh", help="Ignora o cache de 15 min e pergunta de novo ao provedor."),
) -> None:
    """Modelos que o provedor oferece agora, lidos da API dele — o que vale em `model_id`."""
    st = state(ctx)
    result = call(st, st.client.provider_models, provider, credential_id=credential_id, refresh="true" if refresh else None)

    def render(r: dict[str, Any]) -> None:
        for m in r["models"]:
            default = " [dim](padrão do catálogo)[/]" if m["id"] == r["default_model_id"] else ""
            channel = m.get("channel")
            tag = f" [yellow]{channel}[/]" if channel in ("preview", "alias") else ""
            console.print(f"[cyan]{m['id']}[/]  {m['label']}{tag}{default}", highlight=False)

    emit(st, result, render)


@providers_app.command("list")
def list_providers(ctx: typer.Context) -> None:
    """Provedores suportados e quantas credenciais cada um tem."""
    st = state(ctx)

    def render(rows: list[dict[str, Any]]) -> None:
        table = Table(show_edge=False, header_style="bold")
        for column in ("provider", "nome", "credenciais", "modelo padrão"):
            table.add_column(column)
        for p in rows:
            count = f"{p['configured_count']}/{p['credential_count']} com chave" if p["credential_count"] else "—"
            table.add_row(f"[cyan]{p['provider']}[/]", p["label"], count, p["default_model_id"])
        console.print(table)

    emit(st, call(st, st.client.list_providers), render)


@credentials_app.callback(invoke_without_command=True)
def credentials(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        list_credentials(ctx, provider=None)


@credentials_app.command("list")
def list_credentials(
    ctx: typer.Context,
    provider: str | None = typer.Option(None, "--provider", "-p", help="Filtra por provedor."),
) -> None:
    """Credenciais cadastradas (a chave nunca aparece, só os últimos 4 caracteres)."""
    st = state(ctx)
    rows = [c for c in call(st, st.client.list_credentials) if provider is None or c["provider"] == provider]

    def render(items: list[dict[str, Any]]) -> None:
        if not items:
            console.print("Nenhuma credencial cadastrada — crie pelo console web (Modelos).")
            return
        table = Table(show_edge=False, header_style="bold")
        for column in ("id", "provider", "nome", "ativa", "chave", "último teste", "agentes"):
            table.add_column(column)
        for c in items:
            test = {True: "[green]ok[/]", False: "[red]falhou[/]"}.get(c["last_test_ok"], "—")
            key = c["key_hint"] or ("sim" if c["configured"] else "[red]sem chave[/]")
            table.add_row(
                f"[cyan]{c['id']}[/]", c["provider"], c["label"], "sim" if c["enabled"] else "não",
                key, test, ", ".join(c["agents_using"]) or "—",
            )
        console.print(table)

    emit(st, rows, render)


@credentials_app.command("test")
def test_credential(ctx: typer.Context, credential_id: str) -> None:
    """Valida a chave salva de uma credencial (sem gastar tokens). Sai com 1 se falhar."""
    st = state(ctx)
    result = call(st, st.client.test_credential, credential_id)
    if st.json_mode:
        print_json(result)
    if not result["ok"]:
        fail(st, result.get("message") or "teste da credencial falhou", EXIT_FAILED)
    if not st.json_mode:
        console.print(f"[green]✓[/] credencial {credential_id} ok")


def _read_api_key(st: State, *, from_env: str | None, from_stdin: bool, prompt: str) -> str:
    """A chave nunca entra como argumento de comando: ela ficaria no histórico do
    shell e na lista de processos. Só por variável de ambiente, stdin ou prompt oculto."""
    if from_env:
        value = os.environ.get(from_env)
        if not value:
            fail(st, f"a variável de ambiente {from_env} está vazia", EXIT_USAGE)
        return value.strip()
    if from_stdin or not st.interactive:
        value = "" if sys.stdin.isatty() else sys.stdin.read()
        if not value.strip():
            fail(
                st,
                "sem chave: use --api-key-env NOME_DA_VARIAVEL ou envie por stdin "
                "(a chave não pode ir como argumento)",
                EXIT_USAGE,
            )
        return value.strip()
    return typer.prompt(prompt, hide_input=True).strip()


@credentials_app.command("add")
def add_credential(
    ctx: typer.Context,
    provider: str = typer.Option(..., "--provider", "-p", help="google, openai, anthropic, ollama..."),
    label: str = typer.Option(..., "--label", "-l", help="Apelido — ex.: 'Produção', 'Cliente X'."),
    base_url: str | None = typer.Option(None, "--base-url", help="Só para provedores que aceitam endpoint próprio."),
    api_key_env: str | None = typer.Option(
        None, "--api-key-env", help="Nome da variável de ambiente com a chave (não o valor dela)."
    ),
    api_key_stdin: bool = typer.Option(False, "--api-key-stdin", help="Lê a chave do stdin."),
    disabled: bool = typer.Option(False, "--disabled", help="Cadastra sem habilitar."),
) -> None:
    """Cadastra uma chave de modelo. A chave é lida por prompt oculto, stdin ou variável de ambiente."""
    st = state(ctx)
    api_key = _read_api_key(
        st, from_env=api_key_env, from_stdin=api_key_stdin, prompt=f"Chave de API de {provider}"
    )
    created = call(
        st,
        st.client.create_credential,
        {"provider": provider, "label": label, "api_key": api_key, "base_url": base_url, "enabled": not disabled},
    )
    emit(st, created, lambda c: console.print(f"[green]✓[/] credencial {c['id']} criada ({c['key_hint']})"))


@credentials_app.command("edit")
def edit_credential(
    ctx: typer.Context,
    credential_id: str,
    label: str | None = typer.Option(None, "--label", "-l"),
    base_url: str | None = typer.Option(None, "--base-url"),
    enable: bool = typer.Option(False, "--enable", help="Habilita a credencial."),
    disable: bool = typer.Option(False, "--disable", help="Desabilita (agentes fixados nela param de responder)."),
    rotate_key: bool = typer.Option(False, "--rotate-key", help="Substitui a chave (prompt oculto ou stdin)."),
    api_key_env: str | None = typer.Option(None, "--api-key-env", help="Variável de ambiente com a chave nova."),
) -> None:
    """Muda apelido, endpoint, estado ou a chave de uma credencial."""
    st = state(ctx)
    if enable and disable:
        fail(st, "--enable e --disable são mutuamente exclusivos", EXIT_USAGE)

    body: dict[str, Any] = {}
    if label is not None:
        body["label"] = label
    if base_url is not None:
        body["base_url"] = base_url
    if enable or disable:
        body["enabled"] = enable
    if rotate_key or api_key_env:
        body["api_key"] = _read_api_key(
            st, from_env=api_key_env, from_stdin=False, prompt="Chave de API nova"
        )
    if not body:
        fail(st, "nada a alterar: use --label, --base-url, --enable/--disable ou --rotate-key", EXIT_USAGE)

    updated = call(st, st.client.update_credential, credential_id, body)
    emit(st, updated, lambda c: console.print(f"[green]✓[/] credencial {c['id']} atualizada"))


@credentials_app.command("delete")
def delete_credential(
    ctx: typer.Context,
    credential_id: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação (obrigatório sem TTY)."),
) -> None:
    """Remove uma credencial (agentes fixados nela voltam para a padrão do provedor)."""
    st = state(ctx)
    if not yes:
        if not st.interactive:
            fail(st, "confirme com --yes para remover sem TTY", EXIT_USAGE)
        if not typer.confirm(f"Remover a credencial {credential_id}?"):
            raise typer.Exit()
    call(st, st.client.delete_credential, credential_id)
    emit(st, {"deleted": credential_id}, lambda _: console.print("[green]✓[/] credencial removida"))


# -- shell -----------------------------------------------------------------------

_SHELL_HELP = """[bold]Comandos[/] (a `/` é opcional; qualquer comando da CLI funciona aqui)
  /agents              escolher um agente → testar, ver, editar, versões
  /chat <agente>       conversar direto com um agente
  /analyze <agente>    analisar um documento (agentes kind=analysis), one-shot
  /eval <agente> -f casos.jsonl   avaliar contra um dataset (decisão campo a campo)
  /agents feedback <agente>   ensinar o agente a partir de uma conversa
  /agents integrate <agente>   como chamar o agente de outro módulo
  /tools               escolher uma tool → ver e invocar
  /collections         bases de conhecimento (RAG) dos agentes
  /runs                execuções recentes   ·  /runs show <run_id>  ·  /runs tail
  /dash                painel em tela cheia: execuções ao vivo e panorama
  /sessions            conversas guardadas  ·  /sessions show <session_id>
  /providers           provedores de modelo ·  /credentials  chaves cadastradas
  /health              diagnóstico          ·  /mcp-config  como conectar o servidor MCP
  /help                esta ajuda           ·  <comando> --help  detalhes
  /sair                sair"""


def shell(ctx: typer.Context) -> None:
    command = typer.main.get_command(app)
    root_params = ctx.params
    console.print(f"[bold]kuro[/] [dim]· {root_params['url']} · /help para comandos[/]")
    while True:
        try:
            line = console.input("[bold cyan]kuro>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not line:
            continue
        if line.startswith("/"):
            line = line[1:]
        if line in ("sair", "exit", "quit", "q"):
            return
        if line in ("help", "ajuda", "?"):
            console.print(_SHELL_HELP)
            continue
        try:
            args = shlex.split(line, posix=True)
        except ValueError as exc:
            err_console.print(f"[red]erro:[/] {exc}")
            continue
        globals_ = ["--url", root_params["url"], "--timeout", str(root_params["timeout"])]
        if root_params.get("api_key"):
            globals_ += ["--api-key", root_params["api_key"]]
        if root_params.get("ca_bundle"):
            globals_ += ["--ca-bundle", root_params["ca_bundle"]]
        if root_params.get("insecure"):
            globals_ += ["--insecure"]
        try:
            command.main(args=globals_ + hoist_global_flags(args), prog_name="kuro", standalone_mode=False)
        except click.exceptions.Exit:
            pass
        except click.ClickException as exc:
            exc.show()
        except click.exceptions.Abort:
            console.print()
        except KeyboardInterrupt:
            console.print()
        except SystemExit as exc:  # um comando que chamou sys.exit não derruba o shell
            err_console.print(f"[dim](comando encerrou com código {exc.code})[/]")
        except Exception:  # noqa: BLE001 - erro inesperado não pode matar o shell em silêncio
            err_console.print_exception(max_frames=5)
            err_console.print("[red]erro inesperado no comando acima[/] — o shell continua; /sair para sair")


GLOBAL_FLAGS = ("--json", "--no-input")


def hoist_global_flags(argv: list[str]) -> list[str]:
    """Aceita `--json`/`--no-input` em qualquer posição (`kuro agents list --json`),
    não só antes do comando — é o primeiro erro que uma IA comete. Para antes de `--`."""
    head, sep, tail = (argv[: argv.index("--")], ["--"], argv[argv.index("--") + 1 :]) if "--" in argv else (argv, [], [])
    flags = [a for a in head if a in GLOBAL_FLAGS]
    return flags + [a for a in head if a not in GLOBAL_FLAGS] + sep + tail


def main() -> None:
    # Windows: sem isto, texto com acento quebra num pipe (stdout/stderr em cp1252)
    # e chega corrompido quando vem por stdin (`type doc.txt | kuro collections add`).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    app(args=hoist_global_flags(sys.argv[1:]), prog_name="kuro")


if __name__ == "__main__":
    main()
