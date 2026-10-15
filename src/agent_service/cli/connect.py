"""`kuro connect`: conecta o servidor MCP do Kuro ao Claude Code pelo SSH.

Roda na máquina de quem usa. O MCP passa a ser `ssh <servidor> docker exec -i <container>
kuro-mcp`: o processo roda dentro do container e fala com o serviço por dentro, então não
precisa de porta aberta, de certificado nem da chave na máquina de quem usa — a chave já
está no container. Quem opera um Kuro sempre tem SSH funcionando; uma porta nova, nem
sempre (o firewall do painel do provedor fica fora da VPS).

Cada passo é conferido antes do próximo, e o registro só acontece depois de um teste de
verdade: o mesmo comando que o Claude Code vai rodar sobe o MCP e chama a tool `health`.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

from agent_service.cli.common import EXIT_FAILED, EXIT_UNAVAILABLE, EXIT_USAGE, console, emit, err_console, fail, state

COMPOSE_SERVICE = "agent-service"


@dataclass
class Result:
    code: int
    out: str
    err: str


Runner = Callable[[Sequence[str], str | None, float], Result]


def run(cmd: Sequence[str], stdin: str | None = None, timeout: float = 60) -> Result:
    try:
        done = subprocess.run(list(cmd), input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(124, "", f"sem resposta em {timeout:.0f} s")
    return Result(done.returncode, done.stdout, done.stderr)


@dataclass
class Ssh:
    destination: str
    port: int | None = None
    identity: str | None = None
    options: list[str] = field(default_factory=list)

    def base(self, *, batch: bool = True) -> list[str]:
        cmd = ["ssh", "-T"]
        if batch:
            cmd += ["-o", "BatchMode=yes"]
        if self.port:
            cmd += ["-p", str(self.port)]
        if self.identity:
            cmd += ["-i", self.identity]
        for option in self.options:
            cmd += ["-o", option]
        return cmd + [self.destination]

    def remote(self, *args: str) -> list[str]:
        """Um comando no servidor. O ssh junta os argumentos num texto só e o shell de lá o
        separa de novo: sem as aspas do `shlex.join`, `python -c "import mcp.server"` chegaria
        como `python -c import mcp.server`."""
        return [*self.base(), shlex.join(args)]


def mcp_command(ssh: Ssh, container: str) -> list[str]:
    """O comando que o Claude Code roda. Sem `&&`, `$(...)` nem aspas: no Windows o
    `claude` costuma ser um `.cmd`, e os argumentos passam pelo cmd.exe.

    `KURO_MCP_LOCAL_FILES=0`: o processo roda no container, então um caminho de arquivo
    seria do servidor, não de quem pediu — as tools pedem o conteúdo inline.

    Keepalive (`KEEPALIVE`): a sessão dura o dia inteiro, e com a rede caída (Wi-Fi trocado,
    notebook que dormiu) a conexão fica meio aberta — a tool pendurava até o timeout do cliente.
    Assim o ssh desiste em ~45 s e o cliente MCP vê o servidor cair, em vez de esperar calado."""
    return [*ssh.base()[:-1], *KEEPALIVE, ssh.destination,
            "docker", "exec", "-i", "-e", "KURO_MCP_LOCAL_FILES=0", container, "kuro-mcp"]


KEEPALIVE = ("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=15")
"""Opções do ssh do comando registrado: um sinal a cada 15 s, desiste depois de 3 sem resposta."""


def mcp_env() -> dict[str, str]:
    """O que o processo do MCP precisa no ambiente além do mínimo que os clientes passam.

    O `ssh` do Windows lê `%PROGRAMDATA%\\ssh\\ssh_config` e, sem a variável, sai com 255 sem
    dizer nada. Os clientes MCP sobem o servidor com um ambiente reduzido (PATH, USERPROFILE...)
    que não a inclui — o SDK do MCP e o Claude Desktop, por exemplo."""
    if sys.platform == "win32":
        return {"PROGRAMDATA": os.environ.get("PROGRAMDATA") or r"C:\ProgramData"}
    return {}


# -- passos ------------------------------------------------------------------------


class StepFailed(Exception):
    def __init__(self, message: str, code: int = EXIT_FAILED):
        super().__init__(message)
        self.code = code


class SshAuthFailed(StepFailed):
    """O servidor respondeu, mas não deixou entrar sem interação. `reason` diz por quê, porque
    a saída de cada caso é outra:

    - `password`: sem chave aceita, e o servidor aceita senha — dá para instalar a chave com ela;
    - `passphrase`: a chave daqui tem senha de proteção e não está carregada num ssh-agent — o
      Claude Code não tem como digitá-la; carregar no agent resolve, instalar outra chave não;
    - `key_only`: o servidor só aceita chave, e nenhuma daqui foi aceita — pedir senha não adianta.
    """

    def __init__(self, message: str, reason: str):
        super().__init__(message, EXIT_UNAVAILABLE)
        self.reason = reason


AGENT_HELP = (
    "Carregue a chave num ssh-agent, que o Claude Code usa sem perguntar nada: no Windows, num "
    "PowerShell de administrador, `Get-Service ssh-agent | Set-Service -StartupType Automatic; "
    "Start-Service ssh-agent` e depois `ssh-add {key}`; no macOS, `ssh-add --apple-use-keychain {key}`; "
    "no Linux, `ssh-add {key}` numa sessão com agent (o Claude Code precisa ter sido aberto nela)."
)


def _private_keys(ssh: Ssh) -> list[Path]:
    """As chaves que o ssh tentaria: a do `-i`, ou as padrão que existem."""
    if ssh.identity:
        return [Path(ssh.identity).expanduser()]
    return [k for k in (Path.home() / ".ssh" / n for n in ("id_ed25519", "id_ecdsa", "id_rsa")) if k.exists()]


def _locked_key(ssh: Ssh, runner: Runner) -> Path | None:
    """Uma chave daqui com senha de proteção e nenhuma chave carregada no agent — ou `None`."""
    locked = None
    for key in _private_keys(ssh):
        # Sem senha, `-y` imprime a chave pública; com senha, `-P ""` falha em vez de perguntar.
        probe_key = runner(["ssh-keygen", "-y", "-P", "", "-f", str(key)], None, 15)
        if probe_key.code != 0 and "passphrase" in (probe_key.err + probe_key.out).lower():
            locked = locked or key
    if locked is None:
        return None
    # 0: o agent tem chaves (então a chave foi oferecida e recusada); 1: agent vazio; 2: sem agent.
    return locked if runner(["ssh-add", "-l"], None, 15).code != 0 else None


def _auth_methods(err: str) -> set[str]:
    """`Permission denied (publickey,password).` → {"publickey", "password"}."""
    found = re.search(r"Permission denied \(([^)]*)\)", err)
    return {m.strip() for m in found.group(1).split(",")} if found else set()


def diagnose_auth(ssh: Ssh, runner: Runner, err: str) -> SshAuthFailed:
    locked = _locked_key(ssh, runner)
    if locked is not None:
        return SshAuthFailed(
            f"a chave SSH {locked} tem senha de proteção (passphrase) e não está carregada num ssh-agent: "
            "o Claude Code não tem como digitá-la. " + AGENT_HELP.format(key=locked),
            "passphrase",
        )
    methods = _auth_methods(err)
    if methods and not methods & {"password", "keyboard-interactive"}:
        keys = ", ".join(str(k) for k in _private_keys(ssh)) or "nenhuma chave em ~/.ssh"
        return SshAuthFailed(
            f"{ssh.destination} só aceita chave SSH ({', '.join(sorted(methods))}), e nenhuma daqui foi aceita "
            f"({keys}). Ponha a sua chave pública no ~/.ssh/authorized_keys do servidor pelo console do "
            "provedor da VPS (ou peça a quem tem acesso), ou aponte a chave certa com -i.",
            "key_only",
        )
    return SshAuthFailed("o SSH pediu senha", "password")


def check_ssh(ssh: Ssh, runner: Runner) -> None:
    # `accept-new` só neste teste: o primeiro acesso grava a chave do host (como o `ssh`
    # interativo perguntaria), e uma chave diferente da gravada continua sendo recusada.
    first = runner([*ssh.base()[:-1], "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15", ssh.destination, "true"], None, 30)
    if first.code == 0:
        return
    err = first.err.strip()
    if "Permission denied" in err or "publickey" in err:
        raise diagnose_auth(ssh, runner, err)
    if "Host key verification failed" in err or "REMOTE HOST IDENTIFICATION HAS CHANGED" in err:
        raise StepFailed(
            f"a chave do host {ssh.destination} não bate com a que está no seu known_hosts. Se o servidor foi "
            f"reinstalado, confira e rode `ssh-keygen -R <host>`; senão, não continue. ({err})",
            code=EXIT_UNAVAILABLE,
        )
    raise StepFailed(f"não consegui entrar por SSH em {ssh.destination}: {err or f'saída {first.code}'}", EXIT_UNAVAILABLE)


def find_container(ssh: Ssh, runner: Runner, wanted: str | None) -> str:
    if wanted:
        return wanted
    found = runner(
        ssh.remote("docker", "ps", "--filter", f"label=com.docker.compose.service={COMPOSE_SERVICE}", "--format", "{{.Names}}"),
        None,
        30,
    )
    err = found.err.strip()
    if found.code != 0:
        if "permission denied" in err.lower() and "docker" in err.lower():
            raise StepFailed(
                "o usuário do SSH não tem acesso ao Docker. Entre como root ou ponha o usuário no grupo "
                f"docker (`sudo usermod -aG docker $USER`, e entre de novo). ({err})"
            )
        if "not found" in err.lower():
            raise StepFailed(f"o Docker não está instalado (ou não está no PATH) em {ssh.destination}. ({err})")
        raise StepFailed(f"`docker ps` falhou no servidor: {err or f'saída {found.code}'}")
    names = [n for n in found.out.split() if n]
    if not names:
        raise StepFailed(
            f"nenhum container do {COMPOSE_SERVICE} rodando em {ssh.destination}. Suba o serviço na pasta do "
            "projeto: `docker compose up -d`."
        )
    if len(names) > 1:
        raise StepFailed(f"mais de um Kuro rodando ({', '.join(names)}): escolha com --container.", EXIT_USAGE)
    return names[0]


def check_kuro_mcp(ssh: Ssh, runner: Runner, container: str) -> None:
    # O script `kuro-mcp` existe até numa imagem antiga (sem o pacote `mcp`), então o que
    # se confere é o import, não o executável.
    found = runner(ssh.remote("docker", "exec", container, "python", "-c", "import mcp.server"), None, 30)
    if found.code != 0:
        raise StepFailed(
            f"o container {container} não tem o kuro-mcp: a imagem é de antes do `kuro connect`. Atualize o "
            "projeto no servidor e reconstrua: `git pull && docker compose up -d --build`."
        )


def probe(command: list[str], env: dict[str, str], timeout: float = 90) -> dict[str, Any]:
    """Sobe o MCP com o mesmo comando e o mesmo ambiente do registro e chama `health`, como o
    cliente faria. O SDK parte do ambiente mínimo, como os clientes MCP: o que faltar aqui
    faltaria lá."""
    import anyio
    from mcp import Client as McpClient
    from mcp import StdioServerParameters

    async def call() -> dict[str, Any]:
        params = StdioServerParameters(command=command[0], args=command[1:], env=env or None)
        with anyio.fail_after(timeout):
            async with McpClient(params) as mcp:
                result = await mcp.call_tool("health", {})
        text = " ".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            raise StepFailed(f"o MCP subiu, mas a tool health falhou: {text}")
        return result.structured_content or json.loads(text)

    try:
        return anyio.run(call)
    except StepFailed:
        raise
    except TimeoutError as exc:
        raise StepFailed(f"o MCP não respondeu em {timeout:.0f} s pelo SSH") from exc
    except Exception as exc:  # noqa: BLE001 - qualquer falha aqui é "o teste não passou"
        raise StepFailed(f"o MCP não subiu pelo SSH: {type(exc).__name__}: {exc}") from exc


# -- registro --------------------------------------------------------------------


def claude_bin() -> str | None:
    return shutil.which("claude")


def registered(claude: str, name: str, runner: Runner) -> bool:
    return runner([claude, "mcp", "get", name], None, 30).code == 0


def register(
    claude: str, name: str, scope: str, command: list[str], env: dict[str, str], runner: Runner, existing: bool
) -> None:
    """Registra no Claude Code, trocando o registro de mesmo nome quando `existing`."""
    if existing:
        # Sem -s: remove de onde estiver. Com -s, um registro em outro escopo continuaria lá
        # e o `add` seguinte falharia.
        removed = runner([claude, "mcp", "remove", name, "-s", scope], None, 30)
        if removed.code != 0:
            removed = runner([claude, "mcp", "remove", name], None, 30)
        if removed.code != 0:
            raise StepFailed(f"não consegui remover o registro antigo de {name!r}: {removed.err.strip() or removed.out.strip()}")
    env_flags = [flag for key, value in env.items() for flag in ("--env", f"{key}={value}")]
    added = runner([claude, "mcp", "add", name, "-s", scope, *env_flags, "--", *command], None, 30)
    if added.code != 0:
        raise StepFailed(f"`claude mcp add` falhou: {added.err.strip() or added.out.strip()}")


# -- chave SSH ---------------------------------------------------------------------


def _public_key() -> Path | None:
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        pub = Path.home() / ".ssh" / f"{name}.pub"
        if pub.exists():
            return pub
    return None


def install_key(ssh: Ssh, passphrase: bool = False) -> None:
    """Cria a chave (se não houver) e a instala no servidor, pedindo a senha uma vez.
    Interativo: só roda com TTY, e o `ssh` pergunta a senha direto no terminal.

    `passphrase`: a chave nova nasce com senha de proteção (o `ssh-keygen` pergunta) e vai para o
    ssh-agent na hora — sem o agent, o Claude Code não teria como digitá-la a cada conexão."""
    pub = _public_key()
    if pub is None:
        key = Path.home() / ".ssh" / "id_ed25519"
        key.parent.mkdir(mode=0o700, exist_ok=True)
        if passphrase:
            console.print(f"Criando a chave SSH {key}: o ssh-keygen vai pedir a senha de proteção.")
            created = subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(key), "-q"]).returncode
        else:
            console.print(f"Criando a chave SSH {key} (sem senha de proteção; para pôr uma, use --passphrase).")
            created = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q"]).returncode
        if created != 0:
            raise StepFailed("o ssh-keygen falhou")
        if passphrase and subprocess.run(["ssh-add", str(key)]).returncode != 0:
            raise StepFailed(
                f"a chave {key} foi criada, mas não consegui carregá-la no ssh-agent. " + AGENT_HELP.format(key=key)
                + " Depois, rode o kuro connect de novo."
            )
        pub = key.with_suffix(".pub")
    console.print(f"Instalando {pub.name} em {ssh.destination}: o ssh vai pedir a senha uma última vez.")
    # O Windows não tem ssh-copy-id: a chave vai pelo stdin.
    remote = "umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys"
    cmd = [*ssh.base(batch=False)[:-1], "-o", "StrictHostKeyChecking=accept-new", ssh.destination, remote]
    if subprocess.run(cmd, input=pub.read_text(encoding="utf-8"), text=True).returncode != 0:
        raise StepFailed("não consegui instalar a chave no servidor")


# -- comando -----------------------------------------------------------------------


def connect(
    ctx: typer.Context,
    destination: str = typer.Argument(..., help="O servidor, como no ssh: usuario@host ou um alias do ~/.ssh/config."),
    port: int | None = typer.Option(None, "--port", "-p", help="Porta do SSH (padrão: 22 ou a do ~/.ssh/config)."),
    identity: str | None = typer.Option(None, "--identity", "-i", help="Arquivo da chave SSH."),
    ssh_option: list[str] = typer.Option([], "--ssh-option", "-o", help="Opção extra do ssh, como em `ssh -o` (repetível)."),
    container: str | None = typer.Option(None, "--container", help="Container do agent-service (padrão: o único rodando)."),
    name: str = typer.Option("kuro", "--name", help="Nome do servidor MCP no Claude Code."),
    scope: str = typer.Option("user", "--scope", "-s", help="Escopo do registro no Claude Code: user, project ou local."),
    print_only: bool = typer.Option(False, "--print", help="Não registra: só testa e mostra o comando e o .mcp.json."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Troca um registro com o mesmo nome sem perguntar."),
    passphrase: bool = typer.Option(
        False, "--passphrase", help="Se precisar criar a chave SSH, cria com senha de proteção e a carrega no ssh-agent."
    ),
) -> None:
    """Conecta o servidor MCP do Kuro ao Claude Code pelo SSH — sem porta aberta, sem
    certificado e sem a chave de API na sua máquina. Rode na sua máquina:
    `uvx --from "agent-service[mcp] @ git+https://..." kuro connect root@meu-servidor`."""
    st = state(ctx)
    ssh = Ssh(destination, port, identity, list(ssh_option))
    steps: list[str] = []

    def done(text: str) -> None:
        steps.append(text)
        if not st.json_mode:
            console.print(f"[green]✓[/] {text}", highlight=False)

    def stop(exc: StepFailed) -> None:
        fail(st, str(exc), exc.code, steps=steps)

    if shutil.which("ssh") is None:
        fail(st, "não achei o `ssh` nesta máquina (no Windows: Configurações → Recursos opcionais → Cliente OpenSSH)", EXIT_USAGE)

    try:
        try:
            check_ssh(ssh, run)
        except SshAuthFailed as exc:
            if exc.reason != "password":
                raise
            if not st.interactive:
                raise StepFailed(
                    f"o SSH em {destination} pediu senha, e o Claude Code não tem como digitá-la. Instale uma chave "
                    "SSH (rode este comando num terminal interativo, que ele faz isso por você) e tente de novo.",
                    EXIT_UNAVAILABLE,
                ) from exc
            if not typer.confirm(f"O SSH em {destination} pede senha. Instalar uma chave SSH para entrar sem senha?", default=True):
                raise StepFailed("sem chave SSH o Claude Code não consegue entrar no servidor", EXIT_UNAVAILABLE) from exc
            install_key(ssh, passphrase)
            check_ssh(ssh, run)
        done(f"SSH em {destination} sem senha")
        target = find_container(ssh, run, container)
        done(f"container {target}")
        check_kuro_mcp(ssh, run, target)
        command = mcp_command(ssh, target)
        env = mcp_env()
        health = probe(command, env)
        service = health.get("service") or {}
        done(f"MCP respondeu pelo SSH: Kuro {service.get('version', '?')}, autenticação {service.get('auth', '?')}")
    except StepFailed as exc:
        stop(exc)

    report: dict[str, Any] = {
        "destination": destination,
        "container": target,
        "command": command,
        "service": service,
        "env": env,
        "mcp_json": {"mcpServers": {name: {"command": command[0], "args": command[1:], **({"env": env} if env else {})}}},
        "registered": False,
        "replaced": False,
        "steps": steps,
    }

    claude = None if print_only else claude_bin()
    if claude:
        existing = registered(claude, name, run)
        if existing and not yes:
            if not st.interactive:
                fail(st, f"já existe um servidor MCP {name!r} no Claude Code: use --yes para trocá-lo ou --name para outro nome", EXIT_USAGE, steps=steps)
            if not typer.confirm(f"Já existe um servidor MCP {name!r} no Claude Code. Trocar por este?", default=True):
                fail(st, "nada registrado (use --name para registrar com outro nome)", EXIT_USAGE, steps=steps)
        try:
            register(claude, name, scope, command, env, run, existing)
        except StepFailed as exc:
            stop(exc)
        report["replaced"] = existing
        report["registered"] = True
        done(f"registrado no Claude Code como {name!r} (escopo {scope})")

    def render(r: dict[str, Any]) -> None:
        if r["registered"]:
            console.print(f"\nPronto. Abra uma sessão nova do Claude Code e peça: \"use a tool health do {name}\".")
            return
        if not print_only:
            err_console.print("[yellow]aviso:[/] não achei o `claude` nesta máquina; nada foi registrado.", highlight=False)
        console.print("\n[bold]Comando do servidor MCP[/] (para qualquer cliente MCP por stdio):")
        console.print(" ".join(r["command"]), markup=False, highlight=False, soft_wrap=True)
        console.print("\n[bold].mcp.json[/] (Claude Code, Claude Desktop, Cursor):")
        console.print_json(json.dumps(r["mcp_json"]))

    emit(st, report, render)

