"""O endereço do serviço visto de fora — o que vai nos exemplos para quem opera e quem integra.

De dentro do container o serviço só conhece o endereço interno (`http://localhost:8000`,
ou o loopback do `/mcp`), que não serve lá fora. O `.env` (que o compose passa inteiro ao
container) e o profile `ip` dizem o resto. Usado pelo `kuro mcp-config` e pelo contrato de
integração (`GET /agents/{t}/integration`), que antes saía com `http://localhost:8000`
quando pedido de dentro do container (pelo MCP, por exemplo).

Sem dependência da CLI nem do FastAPI: a API e a CLI importam daqui.
"""

import os
from pathlib import Path

LOOPBACK = ("localhost", "127.0.0.1", "0.0.0.0")

PUBLIC_DIR = "/kuro-public"
"""Onde o profile `ip` publica a URL e a CA raiz (volume `kuro_public`, ver docker/caddy-ip.sh).
`KURO_PUBLIC_DIR` troca o caminho (testes, ou o comando rodado fora do container)."""


def is_loopback(url: str) -> bool:
    return any(host in url for host in LOOPBACK)


def ip_https() -> tuple[str, str] | None:
    """(url, ca_pem) do HTTPS por IP, se o profile `ip` estiver de pé."""
    base = Path(os.environ.get("KURO_PUBLIC_DIR") or PUBLIC_DIR)
    try:
        url = (base / "url").read_text(encoding="utf-8").strip()
        ca = (base / "ca.crt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return (url, ca) if url and "BEGIN CERTIFICATE" in ca else None


def public_url(explicit: str | None, internal: str | None = None) -> tuple[str, str]:
    """O endereço do serviço visto de fora, e de onde ele saiu:

    1. `explicit` (`--url` ou `KURO_PUBLIC_URL`): quem sabe disse → `explicit`;
    2. `KURO_API_DOMAIN` com um domínio de verdade: o profile `tls` serve HTTPS nele → `domain`;
    3. o profile `ip` de pé: HTTPS no IP público, com a CA própria do Caddy → `ip`;
    4. `internal` (o endereço pelo qual o cliente chegou), se não for loopback → `client`;
    5. senão o serviço só escuta no host, em `AGENT_SERVICE_BIND` (127.0.0.1:58000) → `local`.
    """
    if explicit:
        return explicit.rstrip("/"), "explicit"
    domain = os.environ.get("KURO_API_DOMAIN", "").strip().strip("/")
    if domain and domain != "localhost" and not domain.endswith(".localhost"):
        return f"https://{domain.removeprefix('https://')}", "domain"
    ip = ip_https()
    if ip:
        return ip[0], "ip"
    if internal and not is_loopback(internal):
        return internal.rstrip("/"), "client"
    port = os.environ.get("AGENT_SERVICE_BIND", "127.0.0.1:58000").rsplit(":", 1)[-1] or "58000"
    return f"http://127.0.0.1:{port}", "local"
