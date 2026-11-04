# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Conecta o servidor MCP do Kuro aos seus clientes de IA pelo SSH.

Roda na máquina de quem usa, sem instalar o Kuro (só a biblioteca padrão do Python):

    uv run https://raw.githubusercontent.com/Arthur-Marques-IA/kuro-agent-platform/master/src/agent_service/kuro_connect.py root@meu-servidor

O mesmo código é o `kuro connect` da CLI. O MCP passa a ser `ssh <servidor> ~/.kuro/bin/kuro-mcp`:
um script pequeno, instalado no servidor por este comando, que acha o container do serviço e roda
o `kuro-mcp` dentro dele. Não precisa de porta aberta, de certificado nem da chave de API na sua
máquina, porque a chave já está no container. Quem opera um Kuro sempre tem SSH funcionando; uma
porta nova, nem sempre (o firewall do painel do provedor fica fora da VPS).

Cada passo é conferido antes do próximo, e o registro só acontece depois de um teste de verdade:
o mesmo comando que o cliente vai rodar sobe o MCP e chama a tool `health`. Depois, registra em
cada cliente que achar nesta máquina: Claude Code, Claude Desktop, Cursor e VS Code.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COMPOSE_SERVICE = "agent-service"

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_UNAVAILABLE = 0, 1, 2, 3


@dataclass
class Result:
    code: int
    out: str
    err: str


Runner = Callable[[Sequence[str], "str | None", float], Result]


def run(cmd: Sequence[str], stdin: str | None = None, timeout: float = 60) -> Result:
    try:
        done = subprocess.run(
            list(cmd), input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return Result(124, "", f"sem resposta em {timeout:.0f} s")
    except OSError as exc:
        return Result(127, "", str(exc))
    return Result(done.returncode, done.stdout or "", done.stderr or "")


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


KEEPALIVE = ("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=15")
"""Opções do ssh do comando registrado: um sinal a cada 15 s, desiste depois de 3 sem resposta."""

WRAPPER_PATH = "~/.kuro/bin/kuro-mcp"
WRAPPER = """#!/bin/sh
# Instalado pelo `kuro connect`: sobe o servidor MCP do Kuro dentro do container do agent-service.
# Usa o container do argumento enquanto ele estiver rodando (com dois Kuros na máquina, cada
# registro fica no seu). Se ele sumiu (pasta renomeada, clone novo do projeto), acha o novo pelo
# rótulo do docker compose.
container="$1"
if [ -z "$container" ] || [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" != "true" ]; then
  found=$(docker ps --filter label=com.docker.compose.service=agent-service --format '{{.Names}}')
  case "$found" in
    "") echo "kuro-mcp: nenhum container do agent-service rodando; suba o serviço com docker compose up -d" >&2; exit 1 ;;
    *"
"*) echo "kuro-mcp: o container $container não está rodando e há mais de um Kuro; rode o kuro connect de novo com --container" >&2; exit 1 ;;
  esac
  container="$found"
fi
exec docker exec -i -e KURO_MCP_LOCAL_FILES=0 "$container" kuro-mcp
"""
"""`KURO_MCP_LOCAL_FILES=0`: o processo roda no container, então um caminho de arquivo seria do
servidor, não de quem pediu — as tools pedem o conteúdo inline."""


def mcp_command(ssh: Ssh, container: str, wrapper: bool = True) -> list[str]:
    """O comando que os clientes rodam. Sem `&&`, `$(...)` nem aspas: no Windows o `claude`
    costuma ser um `.cmd`, e os argumentos passam pelo cmd.exe.

    `wrapper`: o script de `WRAPPER_PATH` no servidor (o padrão), que recebe o container de
    agora e acha outro se ele sumir. Sem o script (não deu para instalar), o `docker exec`
    direto, com o nome do container fixo.

    Keepalive (`KEEPALIVE`): a sessão dura o dia inteiro, e com a rede caída (Wi-Fi trocado,
    notebook que dormiu) a conexão fica meio aberta — a tool pendurava até o timeout do cliente.
    Assim o ssh desiste em ~45 s e o cliente MCP vê o servidor cair, em vez de esperar calado."""
    head = [*ssh.base()[:-1], *KEEPALIVE, ssh.destination]
    if wrapper:
        return [*head, WRAPPER_PATH, container]
    return [*head, "docker", "exec", "-i", "-e", "KURO_MCP_LOCAL_FILES=0", container, "kuro-mcp"]


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
        self.steps: list[str] = []


class SshAuthFailed(StepFailed):
    """O servidor respondeu, mas não deixou entrar sem interação. `reason` diz por quê, porque
    a saída de cada caso é outra:

    - `password`: sem chave aceita, e o servidor aceita senha — dá para instalar a chave com ela;
    - `passphrase`: a chave daqui tem senha de proteção e não está carregada num ssh-agent — o
      cliente MCP não tem como digitá-la; carregar no agent resolve, instalar outra chave não;
    - `key_only`: o servidor só aceita chave, e nenhuma daqui foi aceita — pedir senha não adianta.
    """

    def __init__(self, message: str, reason: str):
        super().__init__(message, EXIT_UNAVAILABLE)
        self.reason = reason


AGENT_HELP = (
    "Carregue a chave num ssh-agent, que o cliente MCP usa sem perguntar nada: no Windows, num "
    "PowerShell de administrador, `Get-Service ssh-agent | Set-Service -StartupType Automatic; "
    "Start-Service ssh-agent` e depois `ssh-add {key}`; no macOS, `ssh-add --apple-use-keychain {key}`; "
    "no Linux, `ssh-add {key}` numa sessão com agent (o cliente precisa ter sido aberto nela)."
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
            "o cliente MCP não tem como digitá-la. " + AGENT_HELP.format(key=locked),
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
    first = runner(
        [*ssh.base()[:-1], "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15", ssh.destination, "true"],
        None,
        30,
    )
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


def install_wrapper(ssh: Ssh, runner: Runner) -> bool:
    """Grava (ou regrava, igual) o `WRAPPER` em `WRAPPER_PATH` no servidor. Escreve num
    temporário e troca no fim: um MCP subindo no meio nunca lê o script pela metade.

    O `tr` tira o `\\r`: no Windows, o stdin em modo texto vira `\\r\\n`, e um `#!/bin/sh\\r` não roda."""
    script = (
        "set -e; mkdir -p ~/.kuro/bin; tr -d '\\r' > ~/.kuro/bin/kuro-mcp.tmp; "
        "chmod 755 ~/.kuro/bin/kuro-mcp.tmp; mv ~/.kuro/bin/kuro-mcp.tmp ~/.kuro/bin/kuro-mcp"
    )
    return runner(ssh.remote("sh", "-c", script), WRAPPER, 30).code == 0


# -- teste do MCP ------------------------------------------------------------------

PROTOCOL_VERSION = "2025-06-18"

# O ambiente que o SDK do MCP (e os clientes) passam ao servidor: o resto não chega lá. O teste
# parte do mesmo mínimo, para o que faltar aqui faltar também no cliente — foi assim que se achou
# o PROGRAMDATA do ssh do Windows.
INHERITED_ENV = (
    ("APPDATA", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE",
     "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "USERNAME", "USERPROFILE")
    if sys.platform == "win32"
    else ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")
)


def probe(command: list[str], env: dict[str, str], timeout: float = 90) -> dict[str, Any]:
    """Sobe o MCP com o mesmo comando e o mesmo ambiente do registro e chama `health`, como o
    cliente faria: `initialize`, `notifications/initialized` e `tools/call`, em JSON por linha
    (o transporte stdio do MCP). A leitura fica numa thread porque `select` não serve para pipe
    no Windows."""
    full_env = {k: os.environ[k] for k in INHERITED_ENV if k in os.environ} | env
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full_env
        )
    except OSError as exc:
        raise StepFailed(f"o MCP não subiu: {exc}") from exc
    lines: queue.Queue[bytes | None] = queue.Queue()
    stderr: list[str] = []

    def pump_out() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.put(line)
        lines.put(None)

    def pump_err() -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr.append(line.decode("utf-8", "replace").rstrip())

    threading.Thread(target=pump_out, daemon=True).start()
    err_reader = threading.Thread(target=pump_err, daemon=True)
    err_reader.start()
    deadline = time.monotonic() + timeout

    def send(message: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(message) + "\n").encode())  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def why() -> str:
        tail = " | ".join(s for s in stderr[-3:] if s)
        return f": {tail}" if tail else ""

    def answer(request_id: int) -> dict[str, Any]:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise StepFailed(f"o MCP não respondeu em {timeout:.0f} s pelo SSH{why()}")
            try:
                line = lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
                err_reader.join(1)  # o motivo costuma estar nas últimas linhas do stderr
                raise StepFailed(f"o MCP fechou a conexão antes de responder (saída {proc.returncode}){why()}")
            try:
                message = json.loads(line)
            except ValueError:
                continue  # linha que não é JSON-RPC (um aviso do shell de lá, por exemplo)
            if message.get("id") == request_id and "method" not in message:
                return message
            if "method" in message and "id" in message:  # um pedido do servidor: não sabemos atender
                send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "não suportado"}})

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "kuro-connect", "version": "1"},
        }})
        init = answer(1)
        if "error" in init:
            raise StepFailed(f"o MCP recusou a conexão: {init['error'].get('message')}")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "health", "arguments": {}}})
        reply = answer(2)
    except (BrokenPipeError, OSError) as exc:
        raise StepFailed(f"o MCP fechou a conexão{why() or f': {exc}'}") from exc
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
            proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    if "error" in reply:
        raise StepFailed(f"o MCP subiu, mas a tool health falhou: {reply['error'].get('message')}")
    result = reply.get("result") or {}
    text = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
    if result.get("isError"):
        raise StepFailed(f"o MCP subiu, mas a tool health falhou: {text}")
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text}


# -- clientes ------------------------------------------------------------------------


def _config_dir(app: str) -> Path:
    """Onde os apps de desktop guardam a configuração: %APPDATA% no Windows, Application
    Support no macOS e ~/.config no Linux."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / app
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / app


def claude_desktop_dir() -> Path:
    """A pasta da configuração do Claude Desktop. No Windows, a instalação nova (MSIX) tirou os
    dados do %APPDATA%\\Claude, que fica só com um marcador `.user-data-left.<id>`, e os pôs em
    %LOCALAPPDATA%\\Claude-Data\\<id>: gravar no lugar antigo não teria efeito nenhum."""
    base = _config_dir("Claude")
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Claude-Data"
        for marker in sorted(base.glob(".user-data-left.*")) if base.is_dir() else []:
            moved = local / marker.name.removeprefix(".user-data-left.")
            if moved.is_dir():
                return moved
        if not base.is_dir() and local.is_dir():
            found = [d for d in local.iterdir() if d.is_dir()]
            if len(found) == 1:
                return found[0]
    return base


def _entry(command: list[str], env: dict[str, str]) -> dict[str, Any]:
    return {"command": command[0], "args": command[1:], **({"env": env} if env else {})}


def read_json_config(path: Path, key: str, name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """O arquivo de configuração como objeto, ou `StepFailed` se não dá para reescrevê-lo sem
    perder nada. JSON com comentário (JSONC) não é reescrito: perderia os comentários."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise StepFailed(
            f"{path} não é JSON puro (tem comentário ou vírgula sobrando?), e eu não reescrevo o arquivo para "
            f"não perder nada. Ponha à mão, em \"{key}\": \"{name}\": {json.dumps(entry, ensure_ascii=False)}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get(key, {}), dict):
        raise StepFailed(f"{path} não tem o formato esperado (um objeto com \"{key}\"): não mexi nele")
    return data


def write_json_entry(path: Path, key: str, name: str, entry: dict[str, Any]) -> None:
    """Põe `entry` em `path[key][name]` sem mexer no resto do arquivo. Guarda o original em
    `<arquivo>.bak` e grava num temporário antes de trocar, para um arquivo pela metade nunca
    ficar no lugar."""
    data = read_json_config(path, key, name, entry)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    data.setdefault(key, {})[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".kuro-tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _json_has(path: Path, key: str, name: str) -> bool:
    try:
        return name in (json.loads(path.read_text(encoding="utf-8")).get(key) or {})
    except (OSError, ValueError, AttributeError):
        # JSONC ou ilegível: procura o nome como chave, para não trocar um registro sem perguntar.
        try:
            return re.search(rf'"{re.escape(name)}"\s*:', path.read_text(encoding="utf-8")) is not None
        except OSError:
            return False


@dataclass
class ClientApp:
    """Um cliente MCP desta máquina: como saber se está instalado, se já tem o servidor e
    como registrar. `activate` diz à pessoa o que fazer para o cliente carregar o servidor."""

    key: str
    label: str
    activate: str

    def present(self) -> bool:
        raise NotImplementedError

    def has(self, name: str, runner: Runner) -> bool:
        raise NotImplementedError

    def check(self, name: str, command: list[str], env: dict[str, str]) -> None:
        """Falha (`StepFailed`) se o registro não vai dar certo. Roda para todos os clientes antes
        de qualquer `add`, para nunca deixar um cliente registrado e outro não."""

    def add(self, name: str, command: list[str], env: dict[str, str], runner: Runner, existing: bool) -> None:
        raise NotImplementedError


@dataclass
class ClaudeCode(ClientApp):
    scope: str = "user"

    def present(self) -> bool:
        return shutil.which("claude") is not None

    def _bin(self) -> str:
        return shutil.which("claude") or "claude"

    def has(self, name: str, runner: Runner) -> bool:
        return runner([self._bin(), "mcp", "get", name], None, 30).code == 0

    def add(self, name: str, command: list[str], env: dict[str, str], runner: Runner, existing: bool) -> None:
        claude = self._bin()
        if existing:
            # Primeiro no escopo pedido; sem -s, remove de onde estiver. Com -s, um registro em
            # outro escopo continuaria lá e o `add` seguinte falharia.
            removed = runner([claude, "mcp", "remove", name, "-s", self.scope], None, 30)
            if removed.code != 0:
                removed = runner([claude, "mcp", "remove", name], None, 30)
            if removed.code != 0:
                raise StepFailed(f"não consegui remover o registro antigo de {name!r}: {removed.err.strip() or removed.out.strip()}")
        env_flags = [flag for key, value in env.items() for flag in ("--env", f"{key}={value}")]
        added = runner([claude, "mcp", "add", name, "-s", self.scope, *env_flags, "--", *command], None, 30)
        if added.code != 0:
            raise StepFailed(f"`claude mcp add` falhou: {added.err.strip() or added.out.strip()}")


@dataclass
class JsonFileClient(ClientApp):
    """Cliente configurado por um arquivo JSON com `mcpServers` (Claude Desktop, Cursor)."""

    path: Path = Path()
    marker: Path = Path()  # existe quando o app está instalado

    def present(self) -> bool:
        return self.marker.exists()

    def has(self, name: str, runner: Runner) -> bool:
        return self.path.exists() and _json_has(self.path, "mcpServers", name)

    def check(self, name: str, command: list[str], env: dict[str, str]) -> None:
        read_json_config(self.path, "mcpServers", name, _entry(command, env))

    def add(self, name: str, command: list[str], env: dict[str, str], runner: Runner, existing: bool) -> None:
        write_json_entry(self.path, "mcpServers", name, _entry(command, env))


@dataclass
class VsCode(ClientApp):
    """O VS Code registra pelo próprio `code --add-mcp`, que troca o servidor de mesmo nome. O
    `mcp.json` do perfil só é lido, para saber se já existe: ele pode ter comentários."""

    path: Path = Path()

    def present(self) -> bool:
        return shutil.which("code") is not None

    def has(self, name: str, runner: Runner) -> bool:
        return self.path.exists() and _json_has(self.path, "servers", name)

    def add(self, name: str, command: list[str], env: dict[str, str], runner: Runner, existing: bool) -> None:
        spec = {"name": name, "type": "stdio", **_entry(command, env)}
        added = runner([shutil.which("code") or "code", "--add-mcp", json.dumps(spec, ensure_ascii=False)], None, 60)
        if added.code != 0:
            raise StepFailed(f"`code --add-mcp` falhou: {added.err.strip() or added.out.strip()}")


def client_apps(scope: str = "user") -> list[ClientApp]:
    desktop = claude_desktop_dir()
    cursor = Path.home() / ".cursor"
    return [
        ClaudeCode("claude-code", "Claude Code", "abra uma sessão nova (numa já aberta, `/mcp`)", scope=scope),
        JsonFileClient(
            "claude-desktop", "Claude Desktop", "feche o Claude Desktop por completo (inclusive da bandeja) e abra de novo",
            path=desktop / "claude_desktop_config.json", marker=desktop,
        ),
        JsonFileClient("cursor", "Cursor", "reinicie o Cursor", path=cursor / "mcp.json", marker=cursor),
        VsCode("vscode", "VS Code", "no VS Code, Command Palette → `MCP: List Servers` → kuro → Start",
               path=_config_dir("Code") / "User" / "mcp.json"),
    ]


CLIENT_KEYS = ("claude-code", "claude-desktop", "cursor", "vscode")


def other_clients(name: str, command: list[str], env: dict[str, str]) -> dict[str, Any]:
    """A configuração para colar em clientes que não registramos sozinhos."""
    env_flags = " ".join(f"--env {k}={v}" for k, v in env.items())
    return {
        "mcp_json": {"mcpServers": {name: _entry(command, env)}},
        "codex": f"codex mcp add {name} {env_flags + ' ' if env_flags else ''}-- {shlex.join(command)}",
    }


# -- chave SSH ---------------------------------------------------------------------


def _public_key() -> Path | None:
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        pub = Path.home() / ".ssh" / f"{name}.pub"
        if pub.exists():
            return pub
    return None


def install_key(ssh: Ssh, ui: Ui, passphrase: bool = False) -> None:
    """Cria a chave (se não houver) e a instala no servidor, pedindo a senha uma vez.
    Interativo: só roda com TTY, e o `ssh` pergunta a senha direto no terminal.

    `passphrase`: a chave nova nasce com senha de proteção (o `ssh-keygen` pergunta) e vai para o
    ssh-agent na hora — sem o agent, o cliente MCP não teria como digitá-la a cada conexão."""
    pub = _public_key()
    if pub is None:
        key = Path.home() / ".ssh" / "id_ed25519"
        key.parent.mkdir(mode=0o700, exist_ok=True)
        if passphrase:
            ui.info(f"Criando a chave SSH {key}: o ssh-keygen vai pedir a senha de proteção.")
            created = subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(key), "-q"]).returncode
        else:
            ui.info(f"Criando a chave SSH {key} (sem senha de proteção; para pôr uma, use --passphrase).")
            created = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q"]).returncode
        if created != 0:
            raise StepFailed("o ssh-keygen falhou")
        if passphrase and subprocess.run(["ssh-add", str(key)]).returncode != 0:
            raise StepFailed(
                f"a chave {key} foi criada, mas não consegui carregá-la no ssh-agent. " + AGENT_HELP.format(key=key)
                + " Depois, rode o kuro connect de novo."
            )
        pub = key.with_suffix(".pub")
    ui.info(f"Instalando {pub.name} em {ssh.destination}: o ssh vai pedir a senha uma última vez.")
    # O Windows não tem ssh-copy-id: a chave vai pelo stdin.
    remote = "umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys"
    cmd = [*ssh.base(batch=False)[:-1], "-o", "StrictHostKeyChecking=accept-new", ssh.destination, remote]
    if subprocess.run(cmd, input=pub.read_text(encoding="utf-8"), text=True).returncode != 0:
        raise StepFailed("não consegui instalar a chave no servidor")


# -- o comando ---------------------------------------------------------------------


@dataclass
class Options:
    destination: str
    port: int | None = None
    identity: str | None = None
    ssh_options: list[str] = field(default_factory=list)
    container: str | None = None
    name: str = "kuro"
    scope: str = "user"
    clients: list[str] = field(default_factory=list)  # vazio: todos os que estiverem instalados
    print_only: bool = False
    yes: bool = False
    passphrase: bool = False


class Ui:
    """Como falar com a pessoa. O `kuro connect` da CLI troca por uma versão com o Rich."""

    def __init__(self, interactive: bool, quiet: bool = False):
        self.interactive = interactive
        self.quiet = quiet

    def step(self, text: str) -> None:
        if not self.quiet:
            print(f"✓ {text}", flush=True)

    def info(self, text: str) -> None:
        if not self.quiet:
            print(text, flush=True)

    def confirm(self, question: str, default: bool = True) -> bool:
        answer = input(f"{question} [{'S/n' if default else 's/N'}] ").strip().lower()
        return default if not answer else answer in ("s", "sim", "y", "yes")


def connect(opts: Options, ui: Ui, runner: Runner | None = None) -> dict[str, Any]:
    """Os passos, na ordem. Devolve o relatório; numa falha, `StepFailed` com `.steps` (até
    onde chegou)."""
    runner = runner or run
    ssh = Ssh(opts.destination, opts.port, opts.identity, list(opts.ssh_options))
    steps: list[str] = []

    def done(text: str) -> None:
        steps.append(text)
        ui.step(text)

    try:
        if shutil.which("ssh") is None:
            raise StepFailed(
                "não achei o `ssh` nesta máquina (no Windows: Configurações → Recursos opcionais → Cliente OpenSSH)",
                EXIT_USAGE,
            )
        try:
            check_ssh(ssh, runner)
        except SshAuthFailed as exc:
            if exc.reason != "password":
                raise
            if not ui.interactive:
                raise StepFailed(
                    f"o SSH em {opts.destination} pediu senha, e o cliente MCP não tem como digitá-la. Instale uma "
                    "chave SSH (rode este comando num terminal interativo, que ele faz isso por você) e tente de novo.",
                    EXIT_UNAVAILABLE,
                ) from exc
            if not ui.confirm(f"O SSH em {opts.destination} pede senha. Instalar uma chave SSH para entrar sem senha?"):
                raise StepFailed("sem chave SSH o cliente MCP não consegue entrar no servidor", EXIT_UNAVAILABLE) from exc
            install_key(ssh, ui, opts.passphrase)
            check_ssh(ssh, runner)
        done(f"SSH em {opts.destination} sem senha")
        target = find_container(ssh, runner, opts.container)
        done(f"container {target}")
        check_kuro_mcp(ssh, runner, target)
        wrapper = install_wrapper(ssh, runner)
        if wrapper:
            done(f"{WRAPPER_PATH} no servidor (se o container mudar de nome, ele acha o novo)")
        command = mcp_command(ssh, target, wrapper)
        env = mcp_env()
        health = probe(command, env)
        service = health.get("service") or {}
        done(f"MCP respondeu pelo SSH: Kuro {service.get('version', '?')}, autenticação {service.get('auth', '?')}")

        report: dict[str, Any] = {
            "destination": opts.destination,
            "container": target,
            "wrapper": WRAPPER_PATH if wrapper else None,
            "command": command,
            "service": service,
            "env": env,
            **other_clients(opts.name, command, env),
            "registered": [],
            "replaced": [],
            "activate": {},
            "steps": steps,
        }
        if opts.print_only:
            return report

        apps = [a for a in client_apps(opts.scope) if a.key in opts.clients] if opts.clients else [
            a for a in client_apps(opts.scope) if a.present()
        ]
        missing = [a.label for a in apps if opts.clients and not a.present()]
        if missing:
            raise StepFailed(f"não achei nesta máquina: {', '.join(missing)}", EXIT_USAGE)
        for app in apps:
            app.check(opts.name, command, env)
        existing = {a.key: a.has(opts.name, runner) for a in apps}
        to_replace = [a for a in apps if existing[a.key]]
        replacing = ", ".join(a.label for a in to_replace)
        if to_replace and not opts.yes and not ui.interactive:
            raise StepFailed(
                f"já existe um servidor MCP {opts.name!r} em {replacing}: use --yes para trocá-lo ou --name para outro nome",
                EXIT_USAGE,
            )
        if apps and ui.interactive and not opts.yes:
            # Uma pergunta só: onde vai, e o que será trocado. Quem quer outra lista usa --client.
            question = f"Registrar o servidor MCP {opts.name!r} em {', '.join(a.label for a in apps)}?"
            if to_replace:
                question += f" (troca o {opts.name!r} que já existe em {replacing})"
            if not ui.confirm(question):
                raise StepFailed("nada registrado (escolha os clientes com --client, ou outro nome com --name)", EXIT_USAGE)
        for app in apps:
            app.add(opts.name, command, env, runner, existing[app.key])
            report["registered"].append(app.key)
            if existing[app.key]:
                report["replaced"].append(app.key)
            report["activate"][app.key] = app.activate
            where = f" (escopo {opts.scope})" if app.key == "claude-code" else ""
            done(f"registrado no {app.label} como {opts.name!r}{where}")
        return report
    except StepFailed as exc:
        exc.steps = steps
        raise


def summary(report: dict[str, Any], name: str, print_only: bool) -> list[tuple[str, str]]:
    """O fechamento para quem lê no terminal: `(estilo, texto)`, com estilo `title`, `code`,
    `warn` ou vazio."""
    labels = {a.key: a.label for a in client_apps()}
    out: list[tuple[str, str]] = []
    if report["registered"]:
        out.append(("title", "\nPronto. Para ativar:"))
        for key in report["registered"]:
            out.append(("", f"  • {labels[key]}: {report['activate'][key]}."))
        out.append(("", f"Depois, peça ao assistente: \"use a tool health do {name}\"."))
    elif not print_only:
        out.append(("warn", "Não achei nenhum cliente MCP nesta máquina (Claude Code, Claude Desktop, Cursor, VS Code); nada foi registrado."))
    out.append(("title", "\nPara outro cliente MCP, o .mcp.json:"))
    out.append(("code", json.dumps(report["mcp_json"], ensure_ascii=False, indent=2)))
    out.append(("title", "\nCodex:"))
    out.append(("code", report["codex"]))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kuro_connect",
        description="Conecta o servidor MCP do Kuro aos seus clientes de IA (Claude Code, Claude Desktop, Cursor, "
        "VS Code) pelo SSH: sem porta aberta, sem certificado e sem a chave de API na sua máquina.",
    )
    parser.add_argument("destination", help="o servidor, como no ssh: usuario@host ou um alias do ~/.ssh/config")
    parser.add_argument("-p", "--port", type=int, help="porta do SSH (padrão: 22 ou a do ~/.ssh/config)")
    parser.add_argument("-i", "--identity", help="arquivo da chave SSH")
    parser.add_argument("-o", "--ssh-option", action="append", default=[], help="opção extra do ssh, como em `ssh -o` (repetível)")
    parser.add_argument("--container", help="container do agent-service (padrão: o único rodando)")
    parser.add_argument("--name", default="kuro", help="nome do servidor MCP nos clientes (padrão: kuro)")
    parser.add_argument("-s", "--scope", default="user", choices=("user", "project", "local"), help="escopo no Claude Code")
    parser.add_argument("--client", action="append", default=[], choices=CLIENT_KEYS,
                        help="registra só neste cliente (repetível; padrão: todos os instalados)")
    parser.add_argument("--print", dest="print_only", action="store_true", help="não registra: só testa e mostra a configuração")
    parser.add_argument("-y", "--yes", action="store_true", help="troca um registro com o mesmo nome sem perguntar")
    parser.add_argument("--passphrase", action="store_true",
                        help="se precisar criar a chave SSH, cria com senha de proteção e a carrega no ssh-agent")
    parser.add_argument("--json", action="store_true", help="saída em JSON (erros em JSON no stderr)")
    parser.add_argument("--no-input", action="store_true", help="nunca pergunta nada")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # o ✓ e os acentos num console antigo do Windows
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    interactive = not args.json and not args.no_input and sys.stdin.isatty() and sys.stdout.isatty()
    opts = Options(
        args.destination, args.port, args.identity, args.ssh_option, args.container, args.name, args.scope,
        args.client, args.print_only, args.yes, args.passphrase,
    )
    try:
        report = connect(opts, Ui(interactive, quiet=args.json))
    except StepFailed as exc:
        if args.json:
            sys.stderr.write(json.dumps({"error": str(exc), "steps": exc.steps}, ensure_ascii=False) + "\n")
        else:
            print(f"erro: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for _style, text in summary(report, args.name, args.print_only):
            print(text)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
