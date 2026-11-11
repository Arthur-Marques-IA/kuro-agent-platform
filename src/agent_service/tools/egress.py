"""Para onde uma tool pode falar — o controle de egress compartilhado.

Uma tool é configurada por quem opera o serviço (API/console) e, no caso de
`location="path"`, parte da URL pode até vir do modelo. Sem nenhum limite, uma
tool alcança o que o container alcança: `http://postgres:5432`, o Redis, o
Langfuse, `http://169.254.169.254/` (metadata da nuvem em AWS/GCP/Azure) ou
qualquer serviço interno da rede. Este módulo é a trava: o destino precisa
resolver para um endereço público.

Vale para os dois caminhos que saem para a rede:

- `tools/api_tool.py` (`kind="api"`), antes de cada chamada;
- `tools/python_tool.py` (`kind="python"`), pelo `httpx` guardado que o
  namespace restrito recebe no lugar do módulo real — sem isso, uma tool
  Python seria o desvio óbvio desta trava.

`TOOL_EGRESS_ALLOWLIST` (lista separada por vírgula) libera hosts internos
legítimos: `TOOL_EGRESS_ALLOWLIST=faturamento.interno,10.0.0.5`.

**Limite conhecido:** a checagem resolve o nome e o `httpx` resolve de novo ao
conectar. Um DNS hostil que devolva um IP público na primeira consulta e um
privado na segunda (DNS rebinding) passa. Fechar isso exigiria fixar o IP
resolvido na conexão, o que quebra SNI/`Host` em HTTPS. Para uma rede em que
isso seja parte do modelo de ameaça, a resposta certa é uma política de saída
no próprio ambiente (egress firewall), não aqui.
"""

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

from agent_service.config import get_settings


class EgressBlockedError(ValueError):
    """Destino recusado. Na config da tool vira 422; numa chamada, vira o
    texto de erro que a tool devolve ao modelo."""


class EgressUnresolvedError(EgressBlockedError):
    """O host não resolveu, então não dá para dizer se é público.

    Numa chamada isso é falha como outra qualquer. Na hora de salvar a tool,
    não: um host pode não resolver agora e resolver depois (DNS fora do ar,
    serviço que ainda vai subir), e recusar o cadastro por isso transformaria
    uma checagem de segurança em obstáculo aleatório. Quem cobra de verdade é
    a checagem de cada chamada."""


def _allowlist() -> frozenset[str]:
    raw = get_settings().tool_egress_allowlist or ""
    return frozenset(host.strip().lower() for host in raw.split(",") if host.strip())


_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Por que este endereço não pode ser alvo de uma tool — `None` se pode.

    A regra é `is_global`: só passa o que é roteável na internet pública. Os testes
    nomeados antes dela existem só para a mensagem dizer o motivo; o que não for
    global e não cair em nenhum deles (CGNAT, documentação, benchmark...) é barrado
    do mesmo jeito, com um motivo genérico. Testar só `is_private` deixava passar
    `100.64.0.0/10`, que não é "privada" para o `ipaddress` mas também não é pública
    — e inclui o metadata da Alibaba Cloud (`100.100.100.200`)."""
    # `::ffff:169.254.169.254` é o metadata da nuvem escrito em IPv6: desembrulha
    # antes de classificar, senão as checagens abaixo olham o endereço errado.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local — inclui o metadata da nuvem"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "endereço não especificado"
    if ip.is_reserved:
        return "faixa reservada"
    if ip.is_private:
        return "rede privada"
    if getattr(ip, "is_site_local", False):
        # fec0::/10: obsoleto, mas ainda uso interno — e o `ipaddress` o dá como global.
        return "site-local (IPv6, uso interno)"
    if ip.version == 4 and ip in _CGNAT:
        return "CGNAT (100.64.0.0/10) — inclui o metadata da Alibaba Cloud"
    if not ip.is_global:
        return "faixa de uso especial, fora da internet pública"
    return None


def _literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """O host já é um IP? Então não há o que resolver."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _from_getaddrinfo(host: str, infos: list[Any]) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _target(url: str) -> tuple[str, int] | None:
    """Valida o que dá para validar sem DNS. `None` = destino liberado, nada a checar."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise EgressBlockedError(f"esquema não permitido: {scheme or '(vazio)'} — use http ou https")

    host = parts.hostname
    if not host:
        raise EgressBlockedError(f"url sem host: {url!r}")
    if host.lower() in _allowlist():
        return None
    return host, parts.port or (443 if scheme == "https" else 80)


def _verify(host: str, addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address]) -> None:
    """Todos os endereços do host precisam ser públicos: um DNS round-robin com um
    registro interno no meio seria um jeito de passar batido."""
    for ip in addresses:
        reason = _blocked_reason(ip)
        if reason is not None:
            raise EgressBlockedError(
                f"destino bloqueado: {host} resolve para {ip} ({reason}). Uma tool só alcança "
                f"endereços públicos — libere o host em TOOL_EGRESS_ALLOWLIST se for intencional."
            )


def check_url(url: str) -> None:
    """Recusa o destino se ele não for público. Levanta `EgressBlockedError`.

    Resolve o DNS de forma bloqueante — use `acheck_url` de dentro de código
    assíncrono."""
    target = _target(url)
    if target is None:
        return
    host, port = target
    literal = _literal(host)
    if literal is not None:
        _verify(host, [literal])
        return
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressUnresolvedError(f"não consegui resolver o host {host!r}: {exc}") from exc
    _verify(host, _from_getaddrinfo(host, infos))


async def acheck_url(url: str) -> None:
    """Igual ao `check_url`, resolvendo pelo resolvedor do event loop.

    `socket.getaddrinfo` bloqueia — poucos milissegundos no caminho feliz, mas
    segundos com um resolvedor lento ou um host inalcançável, que é justamente o
    caso adversarial. Fazer isso direto no loop reintroduziria, em menor escala,
    o travamento que mover a chamada HTTP para `async` foi corrigir."""
    target = _target(url)
    if target is None:
        return
    host, port = target
    literal = _literal(host)
    if literal is not None:
        _verify(host, [literal])
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressUnresolvedError(f"não consegui resolver o host {host!r}: {exc}") from exc
    _verify(host, _from_getaddrinfo(host, infos))


def check_url_template(url: str) -> None:
    """Versão para a hora de salvar a tool, em que a URL ainda tem `{placeholders}`.

    Se o host for um placeholder não dá para checar agora — quem cobra é o
    `check_url` de cada chamada, já com a URL montada."""
    host = urlsplit(url).hostname or ""
    if "{" in host or "}" in host:
        return
    try:
        check_url(url)
    except EgressUnresolvedError:
        return


def guard_request(request: httpx.Request) -> None:
    """Event hook do httpx: roda antes de cada envio, inclusive a cada salto de
    redirect, então vale mesmo para um cliente com `follow_redirects=True`."""
    check_url(str(request.url))
