"""O servidor MCP do Kuro servido pelo próprio serviço, em `/mcp` (Streamable HTTP).

São as mesmas tools do `kuro-mcp` (`agent_service.mcp_server`), sem nada instalado
na máquina de quem usa: com o serviço atrás de HTTPS com certificado público (profile
`tls`), basta `claude mcp add --transport http kuro https://<domínio>/mcp` com a chave
admin no header.

- **Autenticação:** `/mcp` não está em `PUBLIC_PATHS` nem em `RUNTIME_PATHS`, então o
  middleware de `api/auth.py` já exige a chave **admin**, como no resto da API. As tools
  falam com a API pelo loopback com a chave admin do ambiente: quem chegou até aqui já
  tinha esse poder, nada se amplia.
- **Sem arquivos do servidor:** `document_file`, `cases_file`, `rules_file` e o `file`
  de `collection_add` leriam arquivos do container (o `.env`, `/proc/self/environ`) —
  um jeito de transformar a chave admin nos segredos do serviço. Aqui eles são recusados;
  o conteúdo vai inline.
- **Com sessão (stateful):** a confirmação de remover, restaurar e promover é uma
  pergunta do servidor ao cliente no meio da tool (elicitation), e a resposta precisa
  achar a sessão. Por isso o serviço roda com um worker só — o que já é o caso.
- **Sem a checagem de Host** (proteção contra DNS rebinding) do SDK: ela só aceitaria
  `localhost`, e aqui o pedido chega pelo domínio ou pelo IP público. Quem protege é a
  chave obrigatória.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import BaseRoute, Route

from agent_service.cli.client import Client
from agent_service.config import current_key, get_settings
from agent_service.mcp_server import build_server

PATH = "/mcp"


def _internal_client() -> Client:
    settings = get_settings()
    url = os.environ.get("KURO_MCP_API_URL") or f"http://127.0.0.1:{settings.app_port}"
    timeout = float(os.environ.get("KURO_TIMEOUT") or 300)
    return Client(url, timeout=timeout, api_key=current_key(settings.admin_api_key))


def build() -> tuple[BaseRoute, Any]:
    """A rota `/mcp` e o lifespan que mantém o gerenciador de sessões no ar."""
    server = build_server(_internal_client(), local_files=False)
    app = server.streamable_http_app(
        streamable_http_path=PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    route = next(r for r in app.routes if isinstance(r, Route) and r.path == PATH)
    manager = server.session_manager

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return route, lifespan
