"""De onde vem a conexão com o serviço quando não há flags de linha de comando.

A CLI resolve URL, chave e certificado pelas opções do `typer` (que leem as mesmas
variáveis); o servidor MCP e a TUI não têm essas flags e chamam `client_from_env`.
Os nomes são os mesmos nos três, então quem já configurou a CLI não configura de novo.

Sem `KURO_API_KEY`, no host do servidor, a chave sai do `.env` do serviço: quem roda
`kuro` fora do container (como o `kuro dash`, que a imagem não traz), na pasta do
repositório ou pelo `kuro` global instalado dele, não precisa exportar a `ADMIN_API_KEY`
à mão (ver `env_file_key`).
"""

import os
from pathlib import Path

from dotenv import dotenv_values

from agent_service.cli.client import Client
from agent_service.config import current_key
from agent_service.public_url import is_loopback

# 127.0.0.1, não localhost: no Windows `localhost` tenta IPv6 (::1) primeiro e o
# port-forward do Docker Desktop nesse caminho derruba conexões de forma intermitente.
DEFAULT_URL = "http://127.0.0.1:58000"

AUTH_HINT = (
    "defina KURO_API_KEY com a ADMIN_API_KEY do serviço, rode o kuro na pasta do repositório "
    "(ele lê a ADMIN_API_KEY do .env dali) ou use `docker compose exec agent-service kuro ...`"
)
"""A próxima ação quando o serviço responde 401 — na CLI, na TUI e no MCP."""

_TRUE = ("1", "true", "yes", "sim")


def _checkout_root() -> Path | None:
    """A raiz do repositório de onde o `kuro` roda, se ele roda de um checkout (`uv run`,
    ou `uv tool install -e` — o `kuro` global no servidor); `None` numa instalação comum."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / "pyproject.toml").is_file() and (root / "src" / "agent_service").is_dir() else None


def _env_dirs() -> list[Path]:
    """Onde procurar o `.env`: a pasta atual (sem subir pastas) e o checkout do `kuro`."""
    dirs = [Path.cwd()]
    root = _checkout_root()
    if root is not None and root not in dirs:
        dirs.append(root)
    return dirs


def env_file_key(url: str, directories: list[Path] | None = None) -> str | None:
    """A `ADMIN_API_KEY` do primeiro `.env` que a tiver, ou `None`: o da pasta atual e,
    depois, o do checkout de onde o `kuro` roda — o `kuro` instalado globalmente com
    `uv tool install -e <repo>` acha a chave do serviço de qualquer pasta.

    Só para um serviço em loopback: esse `.env` é o do serviço desta máquina, e mandar
    a chave dele para outro endereço (um `KURO_API_URL` de produção, rodando no clone
    local) entregaria a chave a quem não é dono dela. Numa rotação (`nova,antiga`)
    vai a nova."""
    if not is_loopback(url):
        return None
    for directory in directories if directories is not None else _env_dirs():
        path = directory / ".env"
        try:
            if not path.is_file():
                continue
            key = current_key(dotenv_values(path).get("ADMIN_API_KEY"))
        except (OSError, UnicodeDecodeError):
            continue
        if key:
            return key
    return None


def resolve_api_key(explicit: str | None, url: str) -> str | None:
    """A chave explícita (`--api-key` / `KURO_API_KEY`) sempre vence; sem ela, a do `.env`."""
    return explicit or env_file_key(url)


def client_from_env(timeout: float = 120.0) -> Client:
    """`KURO_API_URL` (ou `AGENT_SERVICE_URL`), `KURO_API_KEY`, `KURO_CA_BUNDLE` e
    `KURO_INSECURE=1` (só para teste local com certificado autoassinado)."""
    url = os.environ.get("KURO_API_URL") or os.environ.get("AGENT_SERVICE_URL") or DEFAULT_URL
    insecure = os.environ.get("KURO_INSECURE", "").strip().lower() in _TRUE
    verify: bool | str = False if insecure else (os.environ.get("KURO_CA_BUNDLE") or True)
    api_key = resolve_api_key(os.environ.get("KURO_API_KEY") or None, url)
    return Client(url, timeout=timeout, api_key=api_key, verify=verify)
