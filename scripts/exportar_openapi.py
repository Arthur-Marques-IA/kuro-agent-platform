"""Exporta o OpenAPI do agent-service sem subir servidor nem Postgres.

Monta o app como o smoke do console (`servico_de_teste.preparar`: SQLite
temporário, sem rede) e grava `app.openapi()` em JSON, com as chaves em ordem
estável. É a entrada do `npm run gen:types` (`frontend/src/lib/api-types.gen.ts`).

    uv run python scripts/exportar_openapi.py --out frontend/.cache/openapi.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def openapi_do_kuro() -> dict:
    """Só as rotas do serviço (`base_app` de `main.py`), sem as que o AgentOS do Agno
    monta por cima.

    O `app.openapi()` completo traz as ~140 operações do Agno e, onde os caminhos
    coincidem (`GET /agents`, `GET /health`), documenta a do Agno — mas quem
    responde é a do serviço, registrada antes. Os routers são os mesmos, na mesma
    ordem, de `main.py`; o MCP em `/mcp` não é rota JSON e fica de fora."""
    from fastapi import FastAPI

    from agent_service import main as servico

    app = FastAPI(title=servico.settings.app_name)
    # Mesma lista e mesma ordem dos `include_router` de `main.py`: rota nova num router
    # existente entra sozinha; um router novo precisa entrar aqui também.
    for router in (
        servico.router,
        servico.agents_router,
        servico.collections_router,
        servico.sources_router,
        servico.jobs_router,
        servico.integration_router,
        servico.integration_openapi_router,
        servico.tools_router,
        servico.observability_router,
        servico.model_providers_router,
        servico.model_credentials_router,
    ):
        app.include_router(router)

    def rotas(alvo) -> set[tuple[str, str]]:
        return {
            (route.path, method)
            for route in alvo.routes
            if getattr(getattr(route, "endpoint", None), "__module__", "").startswith("agent_service.api.")
            and getattr(route, "include_in_schema", False)
            for method in getattr(route, "methods", None) or ()
        }

    faltando = rotas(servico.base_app) - rotas(app)
    if faltando:
        raise SystemExit(f"rotas do serviço fora do OpenAPI exportado (router novo em main.py?): {sorted(faltando)}")
    return app.openapi()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="arquivo JSON de saída")
    args = parser.parse_args()
    # `preparar` muda a pasta atual para a temporária: o caminho relativo é resolvido antes.
    out = Path(args.out).resolve()

    sys.path.insert(0, str(RAIZ / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from servico_de_teste import preparar

    preparar()
    spec = openapi_do_kuro()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"OpenAPI ({len(spec.get('paths', {}))} caminhos) em {out}")


if __name__ == "__main__":
    main()
