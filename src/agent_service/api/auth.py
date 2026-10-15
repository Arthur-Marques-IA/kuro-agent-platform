"""Quem pode chamar o quê — chave de API com dois escopos.

Sem isto, todo vizinho de rede que alcança a porta consegue ler qualquer
conversa que passou pelo serviço (`GET /observability/runs`, `/sessions`),
reescrever o prompt de um agente em produção (`PUT /agents/{t}`) e ler as
credenciais de modelo cadastradas. Não é hipótese: nenhuma rota exigia
credencial nenhuma.

Dois escopos, porque são dois públicos com riscos diferentes:

- `runtime`: só executa — `/chat`, `/chat/stream`, `/analyze`, registrar score e a
  decisão de referência de um run (modo shadow).
  É a chave que vai para o outro módulo da plataforma. Se vazar, o estrago é
  gastar tokens, não ler o histórico de todo mundo nem trocar o prompt.
- `admin`: todo o resto — CRUD de agentes, tools, credenciais, collections,
  leitura de traces e de sessões. É a chave do console e da CLI.

**Middleware e não `Depends` por rota** de propósito: o AgentOS monta rotas
próprias por cima deste app (`/sessions`, `/knowledge/...`), e elas expõem
conversa. Uma dependência por router deixaria justamente essas de fora, que é o
tipo de falha que não aparece em teste de rota nenhuma.

As chaves vêm do ambiente (`ADMIN_API_KEY`, `RUNTIME_API_KEY`), não de uma
tabela. Cada variável aceita **uma lista separada por vírgula**, para rotacionar
sem derrubar ninguém: `RUNTIME_API_KEY=nova,antiga` vale as duas até os clientes
trocarem, e depois `RUNTIME_API_KEY=nova`. A primeira é a "atual" — é a que o
próprio serviço usa quando chama a si mesmo (o MCP em `/mcp`, a CLI no container,
o console). Várias chaves por integração, com revogação e `last_used_at`, é o passo
seguinte (ROADMAP H0) e precisa de tabela e CRUD.

**Sem nenhuma chave configurada, o serviço fica aberto** — é o comportamento de
antes, para não quebrar quem já roda local. `GET /health` avisa, e `kuro health`
mostra em vermelho. Em qualquer ambiente compartilhado, configure as duas.
"""

import re
import secrets
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_service.config import get_settings, split_keys

Scope = Literal["runtime", "admin"]

PUBLIC_PATHS = frozenset({"/health", "/ready"})
"""Aberto sempre, e só isto: é o healthcheck do container e de qualquer load
balancer na frente, que não têm como mandar header.

`/docs` e `/openapi.json` ficam **fora** desta lista de propósito. Publicar a
superfície inteira da API — cada rota administrativa, cada schema — para quem
alcança a porta é entregar o mapa antes da fechadura. Com uma chave admin eles
continuam acessíveis: `curl -H "Authorization: Bearer $ADMIN_API_KEY"
http://.../openapi.json`. Com o serviço aberto (sem chave configurada), abrir
`/docs` no navegador segue funcionando como antes."""

RUNTIME_PATHS = frozenset(
    {
        "/chat",
        "/chat/stream",
        "/analyze",
        "/observability/scores",
        "/observability/references",
        "/integration/openapi.json",
    }
)
"""O que a chave `runtime` alcança — executar um agente, avaliar a execução e ler o
OpenAPI dessas rotas (só delas: `api/integration_routes.py`).
Tudo que não está aqui (nem em PUBLIC_PATHS nem em RUNTIME_PATTERNS) exige `admin`."""

RUNTIME_PATTERNS = (
    re.compile(r"^/agents/[^/]+/procedures/[^/]+$"),
    re.compile(r"^/agents/[^/]+/openapi\.json$"),
)
"""Caminhos com parâmetro que a chave `runtime` também alcança: o estado de uma conversa
com um agente procedural (`GET`, a única rota nesse caminho) — quem integra precisa ler o
`state` de uma sessão que ele mesmo conduz — e o OpenAPI de um agente (só as rotas de
runtime, sem instructions nem tools). O funil do agente (`/agents/{t}/procedures`), o
contrato (`/agents/{t}/integration`) e o resto de `/agents` continuam `admin`."""


def configured_keys() -> dict[Scope, list[str]]:
    """As chaves válidas de cada escopo — mais de uma durante uma rotação."""
    settings = get_settings()
    chaves: dict[Scope, list[str]] = {}
    if admin := split_keys(settings.admin_api_key):
        chaves["admin"] = admin
    if runtime := split_keys(settings.runtime_api_key):
        chaves["runtime"] = runtime
    return chaves


def auth_enabled() -> bool:
    return bool(configured_keys())


def _presented_key(request: Request) -> str | None:
    """Aceita `Authorization: Bearer <chave>` ou `X-API-Key: <chave>` — o
    primeiro é o que a maioria dos clientes HTTP já sabe mandar."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.headers.get("x-api-key") or None


def _scope_of(key: str) -> Scope | None:
    """`compare_digest` em vez de `==`: comparação de segredo não deve vazar,
    pelo tempo de resposta, quantos caracteres bateram."""
    for scope, valores in configured_keys().items():
        # Compara com todas, sem parar na primeira que bate: o tempo não diz qual delas é.
        if sum(secrets.compare_digest(key.encode(), v.encode()) for v in valores):
            return scope
    return None


def required_scope(path: str) -> Scope | None:
    """`None` = rota aberta.

    A barra final é normalizada antes da comparação: o middleware roda **antes**
    do roteamento, então o redirect 307 que o FastAPI faria de `/chat/` para
    `/chat` ainda não aconteceu. Sem isto, um cliente que normaliza URL com barra
    no fim receberia 403 com a chave certa — e o dono do cliente HTTP do outro
    lado não é você."""
    normalizado = path.rstrip("/") or "/"
    if normalizado in PUBLIC_PATHS:
        return None
    if normalizado in RUNTIME_PATHS or any(p.match(normalizado) for p in RUNTIME_PATTERNS):
        return "runtime"
    return "admin"


def _authorized(presented: Scope | None, needed: Scope) -> bool:
    # `admin` faz tudo que `runtime` faz; o contrário não.
    return presented == "admin" or presented == needed


def _is_preflight(request: Request) -> bool:
    """O `OPTIONS` que o navegador manda antes de uma chamada de outra origem. Ele nunca leva
    a chave (o navegador não deixa), então exigi-la quebrava o CORS de qualquer página. Passar
    não executa rota nenhuma: o CORSMiddleware responde sozinho, ou a rota dá 405."""
    return (
        request.method == "OPTIONS"
        and "origin" in request.headers
        and "access-control-request-method" in request.headers
    )


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next):  # noqa: ANN001, ANN202
        if not auth_enabled() or _is_preflight(request):
            return await call_next(request)

        needed = required_scope(request.url.path)
        if needed is None:
            return await call_next(request)

        key = _presented_key(request)
        if key is None:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Esta rota exige uma chave de API. Mande "
                    "`Authorization: Bearer <chave>` ou `X-API-Key: <chave>`."
                },
            )
        scope = _scope_of(key)
        if scope is None or not _authorized(scope, needed):
            # Mesma resposta para chave inválida e para escopo insuficiente:
            # dizer "a chave é válida, mas não aqui" já entrega informação.
            return JSONResponse(
                status_code=403,
                content={"detail": f"Chave sem permissão para esta rota (exige escopo {needed!r})."},
            )
        return await call_next(request)
