"""Ponto de entrada do microserviço.

Monta um FastAPI próprio com o contrato estável (`agent_service.api.routes`)
e o passa como `base_app` para o `AgentOS` do Agno, que adiciona por cima as
rotas nativas de execução/streaming de agentes, sessões e knowledge (usadas
pelo console e pelo playground em os.agno.com). Ver README para como conectar
o playground. Cada execução fica no trace store local (`observability/run_store.py`).
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from agno.os import AgentOS
from fastapi import FastAPI

from agent_service.agents.registry import all_agents
from agent_service.agents.seed import seed_default_agents
from agent_service.api import auth, cors, errors, mcp_routes
from agent_service.api.agents_routes import router as agents_router
from agent_service.api.collections_routes import router as collections_router
from agent_service.api.integration_routes import openapi_router as integration_openapi_router
from agent_service.api.integration_routes import router as integration_router
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
from agent_service.migrations import upgrade_database
from agent_service.models.listing import warm_up as warm_up_model_listing
from agent_service.observability.tracing import configure_tracing
from agent_service.tools.seed import seed_default_tools

configure_tracing()

settings = get_settings()
# Antes de tudo: uma lista de origens inválida (`*`, um caminho) derruba a subida com o motivo.
cors_origins = cors.parse_origins(settings.cors_origins)

upgrade_database()
seed_default_collection()
seed_default_tools()
seed_default_agents()
warm_up_model_listing()

# O MCP em `/mcp` (Streamable HTTP): as mesmas tools do `kuro-mcp`, para quem conecta
# direto por URL. O lifespan do base_app entra no do AgentOS, que o combina com os dele.
mcp_route, mcp_lifespan = mcp_routes.build()



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # O MCP e o agendador das fontes de site (`documents/web_sync.py`).
    async with mcp_lifespan(app), scheduler_lifespan(app):
        yield


base_app = FastAPI(title=settings.app_name, lifespan=lifespan)
base_app.router.routes.append(mcp_route)
base_app.include_router(router)
base_app.include_router(agents_router)
base_app.include_router(collections_router)
base_app.include_router(sources_router)
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
    # única de traces é o Langfuse, ligado em `configure_tracing()` acima.
    tracing=False,
)

app = agent_os.get_app()
# `none` (lista vazia) o AgentOS trocaria pela lista padrão dele: aplica depois.
cors.enforce(app, cors_origins)

# Depois do AgentOS montar as rotas dele: o middleware precisa cobrir `/sessions`
# e `/knowledge/...` também, que é onde a conversa fica.
auth.install(app)
errors.install(app)


if __name__ == "__main__":
    agent_os.serve(app="agent_service.main:app", host=settings.app_host, port=settings.app_port, reload=True)
