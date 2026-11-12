"""Ponto de entrada do microserviço.

Monta um FastAPI próprio com o contrato estável (`agent_service.api.routes`)
e o passa como `base_app` para o `AgentOS` do Agno, que adiciona por cima as
rotas nativas de execução/streaming de agentes, sessões e knowledge (usadas
pelo console e pelo playground em os.agno.com). Ver README para como conectar
o playground. Cada execução fica no trace store local (`observability/run_store.py`).

**Importar este módulo não toca no banco nem lê configuração.** Tudo acontece na subida
(o lifespan), nesta ordem:

1. origens de CORS e autenticação (`check_startup_auth`): configuração errada derruba a
   subida antes de qualquer acesso ao banco;
2. sob o lock de migração (`migrations.migration_lock`):
   a. migração e seeds (`prepare_database`);
   b. o app de verdade (`build_app`): o AgentOS lê os agentes e as collections do banco já
      migrado — e o Agno cria a extensão `vector` e as tabelas de vetores dele;
   c. os lifespans do app: as tabelas `agno_*`, o MCP em `/mcp`, o acerto das fontes de site e
      o worker de jobs.
   Duas réplicas subindo juntas fazem isso uma de cada vez: o `CREATE ... IF NOT EXISTS` do
   Agno não é seguro em paralelo (a segunda falharia com `duplicate key`).

Por isso `app` é um invólucro ASGI fino (`Service`): o app do AgentOS só existe depois do
passo 2b. O uvicorn só abre a porta depois da subida; com `--lifespan off` (sem subida), toda
requisição recebe 503.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from agno.os import AgentOS
from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.routing import Router
from starlette.types import Receive, Scope, Send

from agent_service.agents.registry import all_agents
from agent_service.agents.seed import seed_default_agents
from agent_service.api import auth, cors, errors, mcp_routes
from agent_service.api.agents_routes import router as agents_router
from agent_service.api.collections_routes import router as collections_router
from agent_service.api.integration_routes import openapi_router as integration_openapi_router
from agent_service.api.integration_routes import router as integration_router
from agent_service.api.jobs_routes import router as jobs_router
from agent_service.api.model_credentials_routes import router as model_credentials_router
from agent_service.api.model_providers_routes import router as model_providers_router
from agent_service.api.observability_routes import router as observability_router
from agent_service.api.routes import router
from agent_service.api.sources_routes import router as sources_router
from agent_service.api.tools_routes import router as tools_router
from agent_service.config import get_settings
from agent_service.db import get_db
from agent_service.documents.collections import all_collections
from agent_service.documents.store import seed_default_collection
from agent_service.documents.web_sync import scheduler_lifespan
from agent_service.jobs import worker as jobs_worker
from agent_service.migrations import migration_lock, upgrade_database
from agent_service.models.listing import warm_up as warm_up_model_listing
from agent_service.observability.tracing import configure_tracing
from agent_service.tools.seed import seed_default_tools

logger = logging.getLogger(__name__)


def prepare_database() -> None:
    """Migração e seeds. Roda sob o lock de migração: a segunda réplica, ao pegá-lo, acha o
    banco migrado e os seeds feitos (eles são idempotentes, checam antes de criar) e segue."""
    upgrade_database(get_db().db_engine)
    seed_default_collection()
    seed_default_tools()
    seed_default_agents()


def build_app(cors_origins: list[str] | None) -> FastAPI:
    """O app de verdade: rotas do contrato, o MCP em `/mcp` e o AgentOS por cima.

    Lê os agentes e as collections do banco (`all_agents`, `all_collections`), então só
    roda depois de `prepare_database`."""
    settings = get_settings()

    # O MCP em `/mcp` (Streamable HTTP): as mesmas tools do `kuro-mcp`, para quem conecta
    # direto por URL. O lifespan do base_app entra no do AgentOS, que o combina com os dele.
    mcp_route, mcp_lifespan = mcp_routes.build()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # O MCP, a fila de jobs (`jobs/worker.py`) e o acerto das fontes de site com ela.
        async with mcp_lifespan(app), scheduler_lifespan(app), jobs_worker.lifespan(app):
            yield

    base_app = FastAPI(title=settings.app_name, lifespan=lifespan)
    base_app.router.routes.append(mcp_route)
    base_app.include_router(router)
    base_app.include_router(agents_router)
    base_app.include_router(collections_router)
    base_app.include_router(sources_router)
    base_app.include_router(jobs_router)
    base_app.include_router(integration_router)
    base_app.include_router(integration_openapi_router)
    base_app.include_router(tools_router)
    base_app.include_router(observability_router)
    base_app.include_router(model_providers_router)
    base_app.include_router(model_credentials_router)

    agent_os = AgentOS(
        id=settings.app_name,
        name=settings.app_name,
        db=get_db(),
        agents=all_agents(),
        knowledge=all_collections(),
        base_app=base_app,
        cors_allowed_origins=cors_origins or None,
        # O tracing nativo do AgentOS gravaria os mesmos spans no Postgres; a fonte
        # única de traces é o Langfuse, ligado em `configure_tracing()`.
        tracing=False,
    )

    app = agent_os.get_app()
    # `none` (lista vazia) o AgentOS trocaria pela lista padrão dele: aplica depois.
    cors.enforce(app, cors_origins)

    # Depois do AgentOS montar as rotas dele: o middleware precisa cobrir `/sessions`
    # e `/knowledge/...` também, que é onde a conversa fica.
    auth.install(app)
    errors.install(app)
    return app


class Service:
    """O ASGI que o uvicorn sobe (`agent_service.main:app`).

    No lifespan prepara tudo e monta o app (`build_app`); depois só repassa as requisições
    a ele. O protocolo de lifespan fica com o `Router` do Starlette: uma falha na subida
    (ex.: sem chave de API) vira `lifespan.startup.failed`, e o uvicorn sai com a mensagem
    em vez de seguir servindo."""

    def __init__(self) -> None:
        self.app: FastAPI | None = None
        self._router = Router(lifespan=self._lifespan)

    @asynccontextmanager
    async def _lifespan(self, _app: Any) -> AsyncIterator[Any]:
        settings = get_settings()
        # Antes de tudo: uma lista de origens inválida (`*`, um caminho) derruba a subida com o motivo.
        cors_origins = cors.parse_origins(settings.cors_origins)
        # Sem chave de API, só sobe com ALLOW_OPEN=1 (`api/auth.py`). Antes de tocar no banco.
        auth.check_startup_auth()

        configure_tracing()
        async with AsyncExitStack() as stack:
            # O lock é solto quando a subida termina, não no desligamento: o lifespan do app
            # (`stack`) continua aberto depois do `with`.
            with migration_lock(get_db().db_engine):
                prepare_database()
                app = build_app(cors_origins)
                state = await stack.enter_async_context(app.router.lifespan_context(app))
            warm_up_model_listing()
            self.app = app
            try:
                yield state
            finally:
                self.app = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._router.lifespan(scope, receive, send)
            return
        app = self.app
        if app is None:
            # Sem subida: um servidor com o lifespan desligado, ou um TestClient sem `with`.
            if scope["type"] == "http":
                response = JSONResponse({"detail": "O serviço ainda está subindo."}, status_code=503)
                await response(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1013})
            return
        await app(scope, receive, send)


app = Service()


if __name__ == "__main__":
    import uvicorn

    _settings = get_settings()
    uvicorn.run("agent_service.main:app", host=_settings.app_host, port=_settings.app_port, reload=True)
