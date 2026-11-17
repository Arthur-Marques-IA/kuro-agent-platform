"""Para onde uma tool consegue falar.

Sem esta trava, uma tool configurada pela API alcança o que o container alcança:
o Postgres, o Redis e o metadata da nuvem. Os testes cobrem os dois caminhos que
saem para a rede — `kind="api"` e o `httpx` das tools `kind="python"` — porque
proteger só um deixaria o outro como desvio.
"""

import asyncio
import socket
import threading

import httpx
import pytest
from fastapi import HTTPException

from agent_service.api.tools_routes import ToolIn, ToolInvokeIn, create_tool, delete_tool, invoke_tool
from agent_service.config import get_settings
from agent_service.tools import api_tool, egress, python_tool
from agent_service.tools.egress import EgressBlockedError, EgressUnresolvedError, check_url, check_url_template
from agent_service.tools.python_tool import compile_python_tool


def resolve_para(monkeypatch, *ips: str) -> None:
    """Faz todo host resolver para estes endereços (o conftest já resolve para um público)."""

    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(egress.socket, "getaddrinfo", fake)


# -- classificação de endereços ---------------------------------------------------


@pytest.mark.parametrize(
    "ip, motivo",
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("10.1.2.3", "rede privada"),
        ("192.168.0.10", "rede privada"),
        ("172.16.5.4", "rede privada"),
        ("169.254.169.254", "link-local"),  # metadata da nuvem
        ("::ffff:169.254.169.254", "link-local"),  # o mesmo, escrito em IPv6
        ("0.0.0.0", "não especificado"),
        ("224.0.0.1", "multicast"),
        # CGNAT: `is_private` é falso para 100.64.0.0/10, mas não é endereço público
        ("100.64.0.1", "CGNAT"),
        ("100.100.100.200", "CGNAT"),  # metadata da Alibaba Cloud
        ("::ffff:100.100.100.200", "CGNAT"),  # o mesmo, escrito em IPv6
        ("fec0::1", "site-local"),  # IPv6 obsoleto: `is_global` é verdadeiro no Python 3.12
    ],
)
def test_destinos_internos_sao_bloqueados(monkeypatch, ip, motivo):
    resolve_para(monkeypatch, ip)
    with pytest.raises(EgressBlockedError) as exc:
        check_url("https://algum-host/x")
    assert motivo in str(exc.value)


def test_destino_publico_passa(monkeypatch):
    resolve_para(monkeypatch, "93.184.216.34")
    check_url("https://exemplo.com/x")  # não levanta


@pytest.mark.parametrize("ip", ["8.8.8.8", "100.128.0.1", "2001:4860:4860::8888"])
def test_publicos_vizinhos_do_cgnat_continuam_liberados(ip):
    """100.128.0.1 fica logo depois de 100.64.0.0/10: a trava não pode pegar a /8 inteira."""
    check_url(f"https://[{ip}]/x" if ":" in ip else f"https://{ip}/x")  # não levanta


def test_cgnat_liberado_pela_allowlist(monkeypatch):
    monkeypatch.setattr(get_settings(), "tool_egress_allowlist", "100.100.100.200")
    check_url("http://100.100.100.200/latest/meta-data/")  # liberado de propósito
    with pytest.raises(EgressBlockedError):
        check_url("http://100.64.0.1/")  # o resto da faixa continua bloqueado


def test_um_registro_interno_no_meio_ja_bloqueia(monkeypatch):
    """DNS round-robin com um IP interno seria o jeito mais fácil de passar batido."""
    resolve_para(monkeypatch, "93.184.216.34", "10.0.0.7")
    with pytest.raises(EgressBlockedError):
        check_url("https://misto.exemplo/x")


def test_ip_literal_nao_precisa_de_dns():
    with pytest.raises(EgressBlockedError):
        check_url("http://127.0.0.1:5432/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://exemplo.com/x", "gopher://exemplo.com"])
def test_esquemas_fora_de_http_sao_recusados(url):
    with pytest.raises(EgressBlockedError) as exc:
        check_url(url)
    assert "esquema" in str(exc.value)


def test_allowlist_libera_host_interno(monkeypatch):
    resolve_para(monkeypatch, "10.0.0.5")
    monkeypatch.setattr(get_settings(), "tool_egress_allowlist", "faturamento.interno")
    check_url("http://faturamento.interno/cobranca")  # liberado de propósito
    with pytest.raises(EgressBlockedError):
        check_url("http://outro.interno/x")  # o resto continua bloqueado


def test_a_leitura_do_resolvedor_do_sistema_funciona(resolvedor_real):
    """O único teste que fala com o resolvedor de verdade.

    Os demais substituem o `getaddrinfo` e provam a classificação dos endereços;
    este prova que sabemos chamá-lo e ler o que ele devolve. `localhost` resolve
    para loopback em qualquer máquina, então serve sem depender de rede."""
    with pytest.raises(EgressBlockedError) as exc:
        check_url("http://localhost:5432/")
    assert "loopback" in str(exc.value)


# -- na hora de salvar a tool -----------------------------------------------------


def test_host_que_nao_resolve_nao_impede_salvar(monkeypatch):
    """DNS fora do ar não pode virar recusa de cadastro — quem cobra é a chamada."""

    def falha(*args, **kwargs):
        raise socket.gaierror("sem DNS")

    monkeypatch.setattr(egress.socket, "getaddrinfo", falha)
    check_url_template("https://ainda-nao-existe.interno/x")  # não levanta
    with pytest.raises(EgressUnresolvedError):
        check_url("https://ainda-nao-existe.interno/x")


def test_host_como_placeholder_so_e_checado_na_chamada(monkeypatch):
    resolve_para(monkeypatch, "10.0.0.5")
    check_url_template("https://{host}/x")  # não dá para checar agora
    with pytest.raises(EgressBlockedError):
        check_url("https://postgres/x")


def test_criar_tool_apontando_para_dentro_falha_com_422(monkeypatch):
    resolve_para(monkeypatch, "127.0.0.1")
    with pytest.raises(HTTPException) as exc:
        create_tool(
            ToolIn(
                tool_name="vizinho_interno",
                kind="api",
                label="Serviço interno",
                config={"method": "GET", "url": "http://postgres:5432/", "parameters": []},
            )
        )
    assert exc.value.status_code == 422
    assert "bloqueado" in str(exc.value.detail)


# -- na chamada da tool -----------------------------------------------------------


def test_parametro_de_path_nao_consegue_escolher_um_host_interno(monkeypatch):
    """A URL pode ser montada com um parâmetro que o modelo preenche: por isso a
    checagem acontece com a URL pronta, não só com o template salvo."""
    tool = create_tool(
        ToolIn(
            tool_name="host_variavel",
            kind="api",
            label="Host pelo modelo",
            config={
                "method": "GET",
                "url": "https://{host}/dados",
                "parameters": [{"name": "host", "type": "string", "location": "path", "required": True}],
            },
        )
    )
    assert tool["tool_name"] == "host_variavel"
    try:
        resolve_para(monkeypatch, "169.254.169.254")
        result = asyncio.run(invoke_tool("host_variavel", ToolInvokeIn(arguments={"host": "metadata.google.internal"})))
        # O modelo lê a recusa no texto; o teste da tool marca como falha de configuração.
        assert result["ok"] is False and result["failure"] == "config"
        assert "recusada" in result["result"] and "link-local" in result["result"]
    finally:
        delete_tool("host_variavel")


def test_o_cliente_das_tools_nao_segue_redirect():
    """Seguir um 302 levaria a um destino que ninguém checou."""

    async def pegar():
        return api_tool.get_client()

    assert asyncio.run(pegar()).follow_redirects is False


def test_cada_event_loop_tem_o_seu_cliente():
    """O pool do httpx guarda conexões presas ao loop que as criou: um cliente
    global estouraria ao ser reusado noutro loop."""

    async def pegar():
        return api_tool.get_client()

    primeiro, segundo = asyncio.run(pegar()), asyncio.run(pegar())
    assert primeiro is not segundo


def test_resolucao_de_dns_nao_bloqueia_o_event_loop(monkeypatch):
    """`acheck_url` resolve pelo resolvedor do loop, que joga o `getaddrinfo`
    numa thread — senão o caminho assíncrono voltaria a travar no DNS lento."""
    resolvendo_em = {}

    def devagar(host, port, *args, **kwargs):
        resolvendo_em["thread"] = threading.current_thread()
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(egress.socket, "getaddrinfo", devagar)
    asyncio.run(egress.acheck_url("https://exemplo.com/x"))
    assert resolvendo_em["thread"] is not threading.main_thread()


# -- o mesmo vale para as tools Python --------------------------------------------


def _tool_python(code: str, monkeypatch):
    monkeypatch.setattr(get_settings(), "custom_python_tools_enabled", True)
    return compile_python_tool(
        tool_name="py_rede", config={"code": code, "entrypoint": "handler", "timeout_seconds": 5.0}, enabled=True
    )


def test_tool_python_nao_alcanca_a_rede_interna(monkeypatch):
    """Sem isto, `import httpx` numa tool Python seria o desvio da trava."""
    fn = _tool_python(
        "import httpx\ndef handler() -> str:\n    return httpx.get('http://169.254.169.254/latest/meta-data/').text\n",
        monkeypatch,
    )
    resolve_para(monkeypatch, "169.254.169.254")
    with pytest.raises(EgressBlockedError):
        fn()


def test_tool_python_nao_recebe_o_cliente_cru_do_httpx(monkeypatch):
    """`httpx.Client` teria `event_hooks` que a própria tool poderia limpar."""
    fn = _tool_python(
        "import httpx\ndef handler() -> str:\n    return str(httpx.Client())\n",
        monkeypatch,
    )
    with pytest.raises(RuntimeError) as exc:
        fn()
    assert "httpx.get" in str(exc.value)


def test_tool_python_alcanca_a_rede_publica(monkeypatch):
    fn = _tool_python(
        "import httpx\ndef handler() -> str:\n    return httpx.get('https://exemplo.com/x').text\n",
        monkeypatch,
    )
    resolve_para(monkeypatch, "93.184.216.34")
    transporte = httpx.MockTransport(lambda request: httpx.Response(200, text="conteudo publico"))
    monkeypatch.setattr(
        python_tool,
        "_guarded_httpx_client",
        httpx.Client(transport=transporte, event_hooks={"request": [egress.guard_request]}),
    )
    assert fn() == "conteudo publico"


def test_tool_python_em_dry_run_manda_o_header():
    from agent_service.tools.context import dependencies_scope

    request = httpx.Request("POST", "https://exemplo.com/acordos")
    python_tool._mark_dry_run(request)
    assert "X-Kuro-Dry-Run" not in request.headers
    with dependencies_scope({}, dry_run=True):
        python_tool._mark_dry_run(request)
    assert request.headers["X-Kuro-Dry-Run"] == "true"


def test_tool_python_em_dry_run_manda_o_header_pelo_cliente_real(monkeypatch):
    """Ponta a ponta: o cliente que a tool usa de verdade (com os dois hooks), rodando
    como o Agno roda tool síncrona (`asyncio.to_thread`) — o flag tem que chegar lá."""
    import asyncio

    from agent_service.tools.context import dependencies_scope

    recebidos: list[httpx.Request] = []
    cliente_real = httpx.Client

    def cliente_com_rede_falsa(*args, **kwargs):
        transporte = httpx.MockTransport(lambda r: (recebidos.append(r), httpx.Response(200, text="ok"))[1])
        return cliente_real(*args, transport=transporte, **kwargs)

    monkeypatch.setattr(httpx, "Client", cliente_com_rede_falsa)
    monkeypatch.setattr(python_tool, "_guarded_httpx_client", None)
    resolve_para(monkeypatch, "93.184.216.34")
    fn = _tool_python(
        "import httpx\ndef handler() -> str:\n    return httpx.post('https://exemplo.com/acordos').text\n",
        monkeypatch,
    )

    async def como_o_agno(dry_run: bool) -> str:
        with dependencies_scope({}, dry_run=dry_run):
            return await asyncio.to_thread(fn)

    assert asyncio.run(como_o_agno(True)) == "ok"
    assert recebidos[-1].headers["X-Kuro-Dry-Run"] == "true"
    asyncio.run(como_o_agno(False))
    assert "X-Kuro-Dry-Run" not in recebidos[-1].headers
