"""`kuro connect` (e o mesmo código como script avulso): cada passo, com `ssh`, `claude` e
`code` falsos (nada roda de verdade), mais o teste do MCP contra um servidor stdio de verdade.

O teste de ponta a ponta, com um sshd de verdade e o container, está descrito em
docs/mcp.md; aqui fica o que decide o comportamento: a ordem dos passos, as mensagens
de cada falha, o comando que vai para os clientes e como cada cliente é registrado."""

import ast
import json
import sys
import textwrap

import pytest
from typer.testing import CliRunner

from agent_service import kuro_connect as core
from agent_service.cli import main as cli_main

runner = CliRunner()
CONTAINER = "microservice_agents-agent-service-1"
HEALTH = {"service": {"status": "ok", "version": "0.2.0", "auth": "enabled"}}
WRAPPER = ["~/.kuro/bin/kuro-mcp"]


class Fake:
    """Responde por prefixo do comando: `ssh ... true`, `docker ps`, `claude mcp get`..."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.stdin: list[str | None] = []
        self.ssh_ok = True
        self.ssh_err = ""
        self.containers = [CONTAINER]
        self.has_kuro_mcp = True
        self.wrapper_ok = True
        self.registered = False
        self.add_ok = True
        self.key_locked = False  # a chave daqui tem passphrase
        self.agent_code = 2  # `ssh-add -l`: 0 com chaves, 1 vazio, 2 sem agent

    def __call__(self, cmd, stdin=None, timeout=60):
        cmd = list(cmd)
        self.calls.append(cmd)
        self.stdin.append(stdin)
        R = core.Result
        if cmd[0] == "ssh":
            if cmd[-1] == "true":
                return R(0, "", "") if self.ssh_ok else R(255, "", self.ssh_err)
            # Um comando remoto vai num argumento só, já com as aspas do shell de lá.
            if cmd[-1].startswith("docker ps"):
                return R(0, "\n".join(self.containers) + "\n", "")
            if cmd[-1].endswith("python -c 'import mcp.server'"):
                return R(0, "", "") if self.has_kuro_mcp else R(1, "", "ModuleNotFoundError: No module named 'mcp'")
            if cmd[-1].startswith("sh -c"):
                return R(0, "", "") if self.wrapper_ok else R(1, "", "mkdir: cannot create directory: Read-only file system")
        if cmd[0] == "ssh-keygen":
            if self.key_locked:
                return R(255, "", f"Load key \"{cmd[-1]}\": incorrect passphrase supplied to decrypt private key")
            return R(0, "ssh-ed25519 AAAA... eu@notebook\n", "")
        if cmd[:2] == ["ssh-add", "-l"]:
            return R(self.agent_code, "", "")
        if cmd[0].endswith("claude"):
            verb = cmd[2]
            if verb == "get":
                return R(0 if self.registered else 1, "", "")
            if verb == "remove":
                return R(0, "", "")
            if verb == "add":
                return R(0, "", "") if self.add_ok else R(1, "", "deu ruim")
        if cmd[0].endswith("code") and cmd[1] == "--add-mcp":
            return R(0, f"Added MCP servers: {json.loads(cmd[2])['name']}\n", "")
        raise AssertionError(f"comando inesperado: {cmd}")

    def claude(self, verb):
        return [["claude", *c[1:]] for c in self.calls if c[0].endswith("claude") and c[2] == verb]

    def wrapper_installs(self):
        return [s for c, s in zip(self.calls, self.stdin) if c[0] == "ssh" and c[-1].startswith("sh -c")]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    fake = Fake()
    probed: list[list[str]] = []
    monkeypatch.setattr(core, "run", fake)
    monkeypatch.setattr(core, "probe", lambda command, env, timeout=90: probed.append(command) or HEALTH)
    monkeypatch.setattr(core.sys, "platform", "linux")
    # Só o ssh e o Claude Code instalados; quem quer outro cliente o põe em `fake.bins`.
    fake.bins = {"ssh", "claude"}
    monkeypatch.setattr(core.shutil, "which", lambda name: f"/usr/bin/{name}" if name in fake.bins else None)
    # A máquina do teste é um diretório vazio: nenhum Claude Desktop, Cursor nem VS Code de verdade.
    fake.home = tmp_path / "home"
    fake.home.mkdir()
    monkeypatch.setattr(core.Path, "home", lambda: fake.home)
    # Todo lugar onde um cliente guarda configuração, em qualquer sistema: um teste que esquecesse
    # um deles gravaria na configuração de verdade de quem roda os testes.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake.home / ".config"))
    monkeypatch.setenv("APPDATA", str(fake.home / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(fake.home / "Local"))
    for platform in ("linux", "darwin", "win32"):
        monkeypatch.setattr(core.sys, "platform", platform)
        for app in core.client_apps():
            assert fake.home in getattr(app, "path", fake.home / "x").parents, (platform, app.key)
    monkeypatch.setattr(core.sys, "platform", "linux")
    # As chaves desta máquina não entram no teste; quem quer uma a põe aqui.
    fake.keys = []
    monkeypatch.setattr(core, "_private_keys", lambda ssh: list(fake.keys))
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
    assert out["registered"] == ["claude-code"] and not out["replaced"] and out["container"] == CONTAINER
    command = out["command"]
    assert command[:4] == ["ssh", "-T", "-o", "BatchMode=yes"]
    assert command[-3:] == ["root@69.62.89.141", *WRAPPER, CONTAINER]
    # O teste usa exatamente o comando que vai para o cliente, e vem antes do registro.
    assert fake.probed == [command]
    assert fake.claude("add") == [["claude", "mcp", "add", "kuro", "-s", "user", "--", *command]]
    assert out["mcp_json"]["mcpServers"]["kuro"] == {"command": "ssh", "args": command[1:]}
    assert out["codex"].startswith("codex mcp add kuro -- ssh -T")


def test_instala_o_script_do_servidor_que_acha_o_container_sozinho(fake):
    """O cliente chama o script com o container de agora: com dois Kuros na máquina, cada
    registro fica no seu; se o container sumir (pasta renomeada, clone novo), o script acha o
    novo pelo rótulo do compose."""
    out = json.loads(_run("root@vps").stdout)
    assert fake.wrapper_installs() == [core.WRAPPER]
    assert out["command"][-2:] == [*WRAPPER, CONTAINER] and out["wrapper"] == "~/.kuro/bin/kuro-mcp"
    assert "{{.State.Running}}" in core.WRAPPER  # só procura outro se este não está rodando
    assert "label=com.docker.compose.service=agent-service" in core.WRAPPER
    assert "KURO_MCP_LOCAL_FILES=0" in core.WRAPPER


def test_rodar_de_novo_regrava_o_mesmo_script(fake):
    _run("root@vps")
    _run("root@vps", "--yes")
    assert fake.wrapper_installs() == [core.WRAPPER, core.WRAPPER]


def test_container_escolhido_vai_para_o_script(fake):
    fake.containers = ["a-agent-service-1", "b-agent-service-1"]
    out = json.loads(_run("root@vps", "--container", "b-agent-service-1").stdout)
    assert out["command"][-2:] == [*WRAPPER, "b-agent-service-1"]


def test_sem_poder_gravar_o_script_usa_o_docker_exec_direto(fake):
    fake.wrapper_ok = False
    out = json.loads(_run("root@vps").stdout)
    assert out["wrapper"] is None
    assert out["command"][-7:] == ["docker", "exec", "-i", "-e", "KURO_MCP_LOCAL_FILES=0", CONTAINER, "kuro-mcp"]
    assert fake.probed == [out["command"]]


def test_comando_registrado_tem_keepalive_antes_do_destino(fake):
    """Com a rede caída, a conexão meio aberta pendurava a tool até o timeout do cliente.
    As opções vão antes do destino: depois dele virariam parte do comando remoto."""
    command = json.loads(_run("root@69.62.89.141").stdout)["command"]
    destino = command.index("root@69.62.89.141")
    opcoes = [command[i + 1] for i in range(destino) if command[i] == "-o"]
    assert {"ServerAliveInterval=15", "ServerAliveCountMax=3", "ConnectTimeout=15"} <= set(opcoes)
    assert command[destino + 1:] == [*WRAPPER, CONTAINER]


def test_comando_registrado_nao_tem_nada_que_o_cmd_do_windows_interprete(fake):
    command = json.loads(_run("deploy@kuro", "-p", "2222", "-i", "~/.ssh/kuro").stdout)["command"]
    assert ["-p", "2222"] == command[command.index("-p"): command.index("-p") + 2]
    assert not any(ch in arg for arg in command for ch in "&|<>^$`\"'%")


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
    ssh = core.Ssh("root@kuro", identity="~/.ssh/kuro")
    assert core._private_keys(ssh) == [core.Path("~/.ssh/kuro").expanduser()]


def test_chave_do_host_diferente_manda_parar(fake):
    fake.ssh_ok, fake.ssh_err = False, "Host key verification failed."
    result = _run("root@69.62.89.141")
    assert result.exit_code == 3 and "não continue" in _error(result)


def test_sem_ssh_nesta_maquina_explica(fake):
    fake.bins = {"claude"}
    result = _run("root@vps")
    assert result.exit_code == 2 and "OpenSSH" in _error(result)


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
        raise core.StepFailed("o MCP não subiu pelo SSH: boom")

    monkeypatch.setattr(core, "probe", broken)
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
    assert result.exit_code == 0 and json.loads(result.stdout)["replaced"] == ["claude-code"]
    assert fake.claude("remove") and fake.claude("add")


def test_print_nao_registra(fake):
    result = _run("root@vps", "--print")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["registered"] == []
    assert not fake.claude("get") and not fake.claude("add")
    assert fake.probed  # mas testa


def test_sem_cliente_nenhum_mostra_a_configuracao(fake):
    fake.bins = {"ssh"}
    result = runner.invoke(cli_main.app, ["connect", "root@vps"])
    assert result.exit_code == 0
    assert "nada foi registrado" in result.stderr and '"mcpServers"' in result.stdout


def test_no_windows_o_ssh_leva_o_programdata(fake, monkeypatch):
    """Sem PROGRAMDATA o ssh do Windows sai com 255 sem dizer nada, e os clientes MCP sobem o
    servidor com um ambiente reduzido que não a inclui."""
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")
    monkeypatch.setenv("APPDATA", str(fake.home / "AppData"))
    out = json.loads(_run("root@vps").stdout)
    assert out["env"] == {"PROGRAMDATA": r"C:\ProgramData"}
    assert out["mcp_json"]["mcpServers"]["kuro"]["env"] == {"PROGRAMDATA": r"C:\ProgramData"}
    add = fake.claude("add")[0]
    assert add[add.index("--env") + 1] == r"PROGRAMDATA=C:\ProgramData"
    assert add.index("--env") < add.index("--")


# -- os outros clientes -------------------------------------------------------------


def _desktop(fake):
    folder = fake.home / ".config" / "Claude"
    folder.mkdir(parents=True)
    return folder / "claude_desktop_config.json"


def test_claude_desktop_ganha_o_servidor_sem_perder_os_outros(fake):
    config = _desktop(fake)
    config.write_text(json.dumps({"mcpServers": {"outro": {"command": "npx"}}, "globalShortcut": "Ctrl+K"}), encoding="utf-8")
    out = json.loads(_run("root@vps").stdout)
    assert out["registered"] == ["claude-code", "claude-desktop"]
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["mcpServers"]["outro"] == {"command": "npx"} and data["globalShortcut"] == "Ctrl+K"
    assert data["mcpServers"]["kuro"] == {"command": "ssh", "args": out["command"][1:]}
    # O original fica guardado, e o Claude Desktop precisa ser reaberto.
    assert json.loads(config.with_name(config.name + ".bak").read_text(encoding="utf-8"))["mcpServers"] == {"outro": {"command": "npx"}}
    assert "abra de novo" in out["activate"]["claude-desktop"]


def test_claude_desktop_sem_arquivo_ainda(fake):
    config = _desktop(fake)
    assert _run("root@vps").exit_code == 0
    assert "kuro" in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]


def test_claude_desktop_com_kuro_ja_registrado_pede_yes(fake):
    config = _desktop(fake)
    config.write_text(json.dumps({"mcpServers": {"kuro": {"command": "velho"}}}), encoding="utf-8")
    result = _run("root@vps")
    assert result.exit_code == 2 and "Claude Desktop" in _error(result)
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["kuro"] == {"command": "velho"}
    assert not fake.claude("add")  # não registra metade dos clientes
    assert _run("root@vps", "--yes").exit_code == 0
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["kuro"]["command"] == "ssh"


def test_arquivo_com_comentario_nao_e_reescrito(fake):
    config = _desktop(fake)
    original = '{\n  // meus servidores\n  "mcpServers": {}\n}\n'
    config.write_text(original, encoding="utf-8")
    result = _run("root@vps")
    assert result.exit_code == 1 and "à mão" in _error(result)
    assert config.read_text(encoding="utf-8") == original
    # Recusado antes de registrar qualquer cliente: nada fica pela metade.
    assert not fake.claude("add")


def test_claude_desktop_da_microsoft_store_no_windows(fake, monkeypatch):
    """A instalação nova do Claude Desktop no Windows (MSIX) tira os dados de %APPDATA%/Claude,
    que fica só com um marcador, e os põe em %LOCALAPPDATA%/Claude-Data/<id>."""
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(fake.home / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(fake.home / "Local"))
    (fake.home / "Roaming" / "Claude").mkdir(parents=True)
    (fake.home / "Roaming" / "Claude" / ".user-data-left.claude_abc123").touch()
    real = fake.home / "Local" / "Claude-Data" / "claude_abc123"
    real.mkdir(parents=True)
    (real / "claude_desktop_config.json").write_text(json.dumps({"preferences": {"x": 1}}), encoding="utf-8")
    out = json.loads(_run("root@vps").stdout)
    assert "claude-desktop" in out["registered"]
    data = json.loads((real / "claude_desktop_config.json").read_text(encoding="utf-8"))
    assert data["preferences"] == {"x": 1} and "kuro" in data["mcpServers"]
    assert not (fake.home / "Roaming" / "Claude" / "claude_desktop_config.json").exists()


def test_claude_desktop_instalacao_antiga_no_windows(fake, monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(fake.home / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(fake.home / "Local"))
    (fake.home / "Roaming" / "Claude").mkdir(parents=True)
    assert core.claude_desktop_dir() == fake.home / "Roaming" / "Claude"


def test_cursor(fake):
    (fake.home / ".cursor").mkdir()
    out = json.loads(_run("root@vps").stdout)
    assert "cursor" in out["registered"]
    data = json.loads((fake.home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["kuro"]["command"] == "ssh"


def test_vscode_pelo_code_add_mcp(fake):
    fake.bins.add("code")
    out = json.loads(_run("root@vps").stdout)
    assert "vscode" in out["registered"]
    add = [c for c in fake.calls if c[0].endswith("code")][0]
    spec = json.loads(add[2])
    assert spec == {"name": "kuro", "type": "stdio", "command": "ssh", "args": out["command"][1:]}


def test_vscode_ja_registrado_conta_como_existente(fake):
    fake.bins.add("code")
    mcp = fake.home / ".config" / "Code" / "User" / "mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text('{\n\t// comentário\n\t"servers": {"kuro": {"type": "stdio"}}\n}', encoding="utf-8")
    assert _run("root@vps").exit_code == 2


class AskingUi(core.Ui):
    def __init__(self, answer):
        super().__init__(interactive=True, quiet=True)
        self.answer, self.questions = answer, []

    def confirm(self, question, default=True):
        self.questions.append(question)
        return self.answer


def test_no_terminal_pergunta_uma_vez_onde_registrar(fake):
    """Ninguém ganha um servidor no VS Code sem saber: a pergunta lista os clientes achados e o
    que será trocado."""
    fake.bins.add("code")
    fake.registered = True
    ui = AskingUi(False)
    with pytest.raises(core.StepFailed, match="nada registrado"):
        core.connect(core.Options("root@vps"), ui)
    assert ui.questions == ["Registrar o servidor MCP 'kuro' em Claude Code, VS Code? (troca o 'kuro' que já existe em Claude Code)"]
    assert not fake.claude("add")

    ui = AskingUi(True)
    report = core.connect(core.Options("root@vps"), ui)
    assert report["registered"] == ["claude-code", "vscode"] and report["replaced"] == ["claude-code"]
    assert len(ui.questions) == 1


def test_client_registra_so_onde_pedido(fake):
    (fake.home / ".cursor").mkdir()
    out = json.loads(_run("root@vps", "--client", "cursor").stdout)
    assert out["registered"] == ["cursor"] and not fake.claude("add")


def test_client_que_nao_esta_instalado(fake):
    result = _run("root@vps", "--client", "claude-desktop")
    assert result.exit_code == 2 and "Claude Desktop" in _error(result)


def test_client_desconhecido(fake):
    assert _run("root@vps", "--client", "emacs").exit_code == 2


# -- o teste do MCP, contra um servidor stdio de verdade ---------------------------

SERVER = textwrap.dedent(
    """
    import json, sys
    MODE = sys.argv[1]
    if MODE == "morre":
        print("docker: Error response from daemon: No such container: x", file=sys.stderr, flush=True)
        sys.exit(1)
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            print("aviso do shell que não é JSON", flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"}}}), flush=True)
        elif msg.get("method") == "tools/call":
            if MODE == "pergunta":  # o servidor pede algo antes de responder
                print(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "roots/list"}), flush=True)
                assert json.loads(sys.stdin.readline())["error"]["code"] == -32601
            erro = MODE == "erro"
            body = {"service": {"version": "9.9"}}
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text", "text": "401 sem chave" if erro else json.dumps(body)}],
                "isError": erro, **({} if erro else {"structuredContent": body})}}), flush=True)
    """
)


@pytest.fixture
def server(tmp_path):
    path = tmp_path / "server.py"
    path.write_text(SERVER, encoding="utf-8")
    return lambda mode: [sys.executable, str(path), mode]


def test_probe_fala_o_protocolo(server):
    assert core.probe(server("ok"), {}, timeout=30) == {"service": {"version": "9.9"}}


def test_probe_responde_pedido_do_servidor(server):
    assert core.probe(server("pergunta"), {}, timeout=30)["service"]["version"] == "9.9"


def test_probe_com_health_falhando(server):
    with pytest.raises(core.StepFailed, match="401 sem chave"):
        core.probe(server("erro"), {}, timeout=30)


def test_probe_mostra_o_stderr_de_quem_morreu(server):
    with pytest.raises(core.StepFailed, match="No such container"):
        core.probe(server("morre"), {}, timeout=30)


def test_probe_sem_o_executavel():
    with pytest.raises(core.StepFailed, match="não subiu"):
        core.probe(["kuro-comando-que-nao-existe"], {}, timeout=5)


# -- o script avulso ---------------------------------------------------------------


def test_modulo_so_usa_a_biblioteca_padrao():
    """É o que deixa `uv run <url>` rodar sem instalar o Kuro (nem clonar o repositório)."""
    tree = ast.parse(core.__loader__.get_source(core.__name__))
    modules = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    modules |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}, modules - set(sys.stdlib_module_names)
    assert "# /// script" in core.__loader__.get_source(core.__name__)


def test_script_avulso_em_json(fake, capsys):
    assert core.main(["root@vps", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["registered"] == ["claude-code"]


def test_script_avulso_com_erro(fake, capsys):
    fake.containers = []
    assert core.main(["root@vps", "--json"]) == 1
    erro = json.loads(capsys.readouterr().err)
    assert "docker compose up -d" in erro["error"] and erro["steps"]


def test_script_avulso_em_texto(fake, capsys):
    assert core.main(["root@vps", "--no-input"]) == 0
    out = capsys.readouterr().out
    assert "✓ SSH em root@vps sem senha" in out and "Claude Code:" in out and "use a tool health do kuro" in out
