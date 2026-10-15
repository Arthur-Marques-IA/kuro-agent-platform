"""`kuro connect`: cada passo, com `ssh` e `claude` falsos (nada roda de verdade).

O teste de ponta a ponta, com um sshd de verdade e o container, está descrito em
docs/mcp.md; aqui fica o que decide o comportamento: a ordem dos passos, as mensagens
de cada falha e o comando que vai para o Claude Code."""

import json

import pytest
from typer.testing import CliRunner

from agent_service.cli import connect as connect_mod
from agent_service.cli import main as cli_main

runner = CliRunner()
CONTAINER = "microservice_agents-agent-service-1"
HEALTH = {"service": {"status": "ok", "version": "0.2.0", "auth": "enabled"}}


class Fake:
    """Responde por prefixo do comando: `ssh ... true`, `docker ps`, `claude mcp get`..."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.ssh_ok = True
        self.ssh_err = ""
        self.containers = [CONTAINER]
        self.has_kuro_mcp = True
        self.registered = False
        self.add_ok = True
        self.key_locked = False  # a chave daqui tem passphrase
        self.agent_code = 2  # `ssh-add -l`: 0 com chaves, 1 vazio, 2 sem agent

    def __call__(self, cmd, stdin=None, timeout=60):
        cmd = list(cmd)
        self.calls.append(cmd)
        R = connect_mod.Result
        if cmd[0] == "ssh":
            if cmd[-1] == "true":
                return R(0, "", "") if self.ssh_ok else R(255, "", self.ssh_err)
            # Um comando remoto vai num argumento só, já com as aspas do shell de lá.
            if cmd[-1].startswith("docker ps"):
                return R(0, "\n".join(self.containers) + "\n", "")
            if cmd[-1].endswith("python -c 'import mcp.server'"):
                return R(0, "", "") if self.has_kuro_mcp else R(1, "", "ModuleNotFoundError: No module named 'mcp'")
        if cmd[0] == "ssh-keygen":
            if self.key_locked:
                return R(255, "", f"Load key \"{cmd[-1]}\": incorrect passphrase supplied to decrypt private key")
            return R(0, "ssh-ed25519 AAAA... eu@notebook\n", "")
        if cmd[:2] == ["ssh-add", "-l"]:
            return R(self.agent_code, "", "")
        if cmd[:3] == ["claude", "mcp", "get"]:
            return R(0 if self.registered else 1, "", "")
        if cmd[:3] == ["claude", "mcp", "remove"]:
            return R(0, "", "")
        if cmd[:3] == ["claude", "mcp", "add"]:
            return R(0, "", "") if self.add_ok else R(1, "", "deu ruim")
        raise AssertionError(f"comando inesperado: {cmd}")

    def claude(self, verb):
        return [c for c in self.calls if c[:3] == ["claude", "mcp", verb]]


@pytest.fixture
def fake(monkeypatch):
    fake = Fake()
    probed: list[list[str]] = []
    monkeypatch.setattr(connect_mod, "run", fake)
    monkeypatch.setattr(connect_mod, "probe", lambda command, env, timeout=90: probed.append(command) or HEALTH)
    monkeypatch.setattr(connect_mod.sys, "platform", "linux")
    monkeypatch.setattr(connect_mod, "claude_bin", lambda: "claude")
    monkeypatch.setattr(connect_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    # As chaves desta máquina não entram no teste; quem quer uma a põe aqui.
    fake.keys = []
    monkeypatch.setattr(connect_mod, "_private_keys", lambda ssh: list(fake.keys))
    fake.probed = probed
    return fake


def _run(*args):
    return runner.invoke(cli_main.app, ["--json", "connect", *args])


def _error(result) -> str:
    return json.loads(result.stderr)["error"]


def test_testa_antes_e_registra_o_ssh_no_claude_code(fake):
    result = _run("root@69.62.89.141")
    assert result.exit_code == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["registered"] and not out["replaced"] and out["container"] == CONTAINER
    command = out["command"]
    assert command[:4] == ["ssh", "-T", "-o", "BatchMode=yes"]
    assert command[-7:] == ["docker", "exec", "-i", "-e", "KURO_MCP_LOCAL_FILES=0", CONTAINER, "kuro-mcp"]
    # O teste usa exatamente o comando que vai para o Claude Code, e vem antes do registro.
    assert fake.probed == [command]
    assert fake.claude("add") == [["claude", "mcp", "add", "kuro", "-s", "user", "--", *command]]
    assert out["mcp_json"]["mcpServers"]["kuro"] == {"command": "ssh", "args": command[1:]}


def test_comando_registrado_tem_keepalive_antes_do_destino(fake):
    """Com a rede caída, a conexão meio aberta pendurava a tool até o timeout do cliente.
    As opções vão antes do destino: depois dele virariam parte do comando remoto."""
    command = json.loads(_run("root@69.62.89.141").stdout)["command"]
    destino = command.index("root@69.62.89.141")
    opcoes = [command[i + 1] for i in range(destino) if command[i] == "-o"]
    assert {"ServerAliveInterval=15", "ServerAliveCountMax=3", "ConnectTimeout=15"} <= set(opcoes)
    assert command[destino + 1] == "docker"


def test_comando_registrado_nao_tem_nada_que_o_cmd_do_windows_interprete(fake):
    command = json.loads(_run("deploy@kuro", "-p", "2222", "-i", "~/.ssh/kuro").stdout)["command"]
    assert ["-p", "2222"] == command[command.index("-p"): command.index("-p") + 2]
    assert not any(ch in arg for arg in command for ch in "&|<>^$`\"'")


def test_ssh_com_senha_sem_terminal_explica_e_nao_registra(fake):
    fake.ssh_ok, fake.ssh_err = False, "root@69.62.89.141: Permission denied (publickey,password)."
    result = _run("root@69.62.89.141")
    assert result.exit_code == 3
    assert "senha" in _error(result) and "chave SSH" in _error(result)
    assert not fake.claude("add")


def test_chave_com_passphrase_sem_agent_manda_carregar_no_agent(fake):
    """Oferecer instalar outra chave não resolveria: a chave certa existe, só está trancada."""
    fake.ssh_ok, fake.ssh_err = False, "root@kuro: Permission denied (publickey,password)."
    fake.keys, fake.key_locked = ["/home/eu/.ssh/id_ed25519"], True
    result = _run("root@kuro")
    assert result.exit_code == 3
    erro = _error(result)
    assert "passphrase" in erro and "ssh-add" in erro and "id_ed25519" in erro
    assert ["ssh-add", "-l"] in fake.calls
    assert not fake.claude("add")


def test_chave_com_passphrase_ja_no_agent_nao_e_o_problema(fake):
    """Com a chave no agent, ela foi oferecida e recusada: o caso é o de sempre (senha)."""
    fake.ssh_ok, fake.ssh_err = False, "root@kuro: Permission denied (publickey,password)."
    fake.keys, fake.key_locked, fake.agent_code = ["/home/eu/.ssh/id_ed25519"], True, 0
    erro = _error(_run("root@kuro"))
    assert "pediu senha" in erro and "passphrase" not in erro


def test_servidor_que_so_aceita_chave_nao_pede_senha(fake):
    fake.ssh_ok, fake.ssh_err = False, "root@kuro: Permission denied (publickey)."
    fake.keys = ["/home/eu/.ssh/id_ed25519"]
    result = _run("root@kuro")
    assert result.exit_code == 3
    erro = _error(result)
    assert "só aceita chave" in erro and "authorized_keys" in erro and "id_ed25519" in erro


def test_diagnostico_respeita_a_chave_do_dash_i():
    ssh = connect_mod.Ssh("root@kuro", identity="~/.ssh/kuro")
    assert connect_mod._private_keys(ssh) == [connect_mod.Path("~/.ssh/kuro").expanduser()]


def test_chave_do_host_diferente_manda_parar(fake):
    fake.ssh_ok, fake.ssh_err = False, "Host key verification failed."
    result = _run("root@69.62.89.141")
    assert result.exit_code == 3 and "não continue" in _error(result)


def test_sem_container_manda_subir_o_servico(fake):
    fake.containers = []
    result = _run("root@vps")
    assert result.exit_code == 1 and "docker compose up -d" in _error(result)
    assert not fake.probed


def test_dois_kuros_pede_o_container(fake):
    fake.containers = ["a-agent-service-1", "b-agent-service-1"]
    result = _run("root@vps")
    assert result.exit_code == 2 and "--container" in _error(result)
    assert _run("root@vps", "--container", "b-agent-service-1").exit_code == 0


def test_imagem_antiga_manda_reconstruir(fake):
    fake.has_kuro_mcp = False
    result = _run("root@vps")
    assert result.exit_code == 1 and "--build" in _error(result)
    assert not fake.probed and not fake.claude("add")


def test_mcp_que_nao_responde_nao_e_registrado(fake, monkeypatch):
    def broken(command, env, timeout=90):
        raise connect_mod.StepFailed("o MCP não subiu pelo SSH: boom")

    monkeypatch.setattr(connect_mod, "probe", broken)
    result = _run("root@vps")
    assert result.exit_code == 1 and "boom" in _error(result)
    assert json.loads(result.stderr)["steps"]  # diz até onde chegou
    assert not fake.claude("add")


def test_registro_existente_so_e_trocado_com_yes(fake):
    fake.registered = True
    result = _run("root@vps")
    assert result.exit_code == 2 and "--yes" in _error(result)
    assert not fake.claude("remove") and not fake.claude("add")

    result = _run("root@vps", "--yes")
    assert result.exit_code == 0 and json.loads(result.stdout)["replaced"]
    assert fake.claude("remove") and fake.claude("add")


def test_print_nao_registra(fake):
    result = _run("root@vps", "--print")
    assert result.exit_code == 0
    assert not json.loads(result.stdout)["registered"]
    assert not fake.claude("get") and not fake.claude("add")
    assert fake.probed  # mas testa


def test_sem_claude_code_mostra_a_configuracao(fake, monkeypatch):
    monkeypatch.setattr(connect_mod, "claude_bin", lambda: None)
    out = json.loads(_run("root@vps").stdout)
    assert not out["registered"] and out["mcp_json"]["mcpServers"]["kuro"]["command"] == "ssh"


def test_no_windows_o_ssh_leva_o_programdata(fake, monkeypatch):
    """Sem PROGRAMDATA o ssh do Windows sai com 255 sem dizer nada, e os clientes MCP sobem o
    servidor com um ambiente reduzido que não a inclui."""
    monkeypatch.setattr(connect_mod.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")
    out = json.loads(_run("root@vps").stdout)
    assert out["env"] == {"PROGRAMDATA": r"C:\ProgramData"}
    assert out["mcp_json"]["mcpServers"]["kuro"]["env"] == {"PROGRAMDATA": r"C:\ProgramData"}
    add = fake.claude("add")[0]
    assert add[add.index("--env") + 1] == r"PROGRAMDATA=C:\ProgramData"
    assert add.index("--env") < add.index("--")
