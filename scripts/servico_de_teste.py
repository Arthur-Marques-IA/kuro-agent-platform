"""Sobe o agent-service desta árvore para teste, sem Postgres e sem provedor de LLM.

É o backend do smoke do console (`frontend/e2e/`, `npm run e2e`) e a base do
`exportar_openapi.py`. O app é o de verdade (`agent_service.main`: rotas,
autenticação, trace store, sessões, versões), com três trocas, todas feitas aqui
e nada no código do serviço:

- **banco**: SQLite num diretório temporário novo a cada subida (o mesmo que a
  suíte do pytest usa, `tests/conftest.py`). As migrações e os seeds rodam como
  no Postgres.
- **collections**: o `PgVector` do Agno vira um banco vetorial vazio em memória.
  O pgvector só existe no Postgres; o smoke não exercita RAG.
- **modelo**: todo `get_model` devolve `ModeloDeTeste`, que responde um texto
  fixo derivado da mensagem (`Resposta de teste: <mensagem>`) com uso de tokens
  fixo. Nada sai para a rede.

Também não busca a lista de modelos do Google ao subir (`warm_up`), não lê o
`.env` do repositório (roda de dentro do diretório temporário) e liga a
autenticação com uma chave admin conhecida, para o console passar pelo mesmo
caminho de produção (o BFF manda `Authorization`).

    uv run python scripts/servico_de_teste.py --port 58100

Variáveis: `KURO_TESTE_ADMIN_KEY` (padrão `e2e-admin-key`).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

ADMIN_KEY = os.environ.get("KURO_TESTE_ADMIN_KEY", "e2e-admin-key")
RESPOSTA_PREFIXO = "Resposta de teste: "


def preparar() -> Path:
    """Ambiente e trocas, antes de qualquer import do serviço. Devolve o diretório do banco."""
    pasta = Path(tempfile.mkdtemp(prefix="kuro-teste-"))
    # Sem `.env`: as Settings leem o `.env` da pasta atual, e o do repositório
    # (chaves, DATABASE_URL do Postgres) não pode vazar para o teste.
    os.chdir(pasta)
    from cryptography.fernet import Fernet

    ambiente = {
        "DATABASE_URL": f"sqlite:///{(pasta / 'agent_service.db').as_posix()}",
        "CREDENTIALS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        # O provedor `google` exige uma chave para montar o modelo; o modelo é trocado abaixo.
        "GOOGLE_API_KEY": "chave-de-teste",
        "ADMIN_API_KEY": ADMIN_KEY,
        "RUNTIME_API_KEY": "",
        "LANGFUSE_ENABLED": "false",
        "MEM0_ENABLED": "false",
        "CORS_ORIGINS": "none",
        # A telemetria do Agno (os-api.agno.com) é a única saída para a rede que sobraria.
        "AGNO_TELEMETRY": "false",
    }
    os.environ.update(ambiente)
    _trocar_banco()
    _trocar_modelo()
    _trocar_vetores()
    _sem_warm_up()
    return pasta


def _trocar_banco() -> None:
    """O `PostgresDb` do Agno (sessões, runs do Agno, conteúdo das collections) usa
    JSONB e schema `ai`, que o SQLite não tem: aqui vira o `SqliteDb` do próprio Agno,
    no mesmo arquivo. As tabelas do serviço (migrações) não mudam."""
    from functools import lru_cache

    from agno.db.sqlite import SqliteDb

    from agent_service import db

    @lru_cache
    def get_db() -> SqliteDb:
        return SqliteDb(db_url=os.environ["DATABASE_URL"])

    # Antes de qualquer módulo do serviço importar `get_db` pelo nome.
    db.get_db = get_db  # type: ignore[assignment]


def _trocar_modelo() -> None:
    from agent_service.models import provider

    def get_model(provider_name: str | None = None, model_id: str | None = None, *args: Any, **kwargs: Any):
        return ModeloDeTeste(id=model_id or "modelo-de-teste")

    # Antes de `agents/base.py` e companhia importarem `get_model` pelo nome.
    provider.get_model = get_model  # type: ignore[assignment]
    import agent_service.models as models_pkg

    models_pkg.get_model = get_model  # type: ignore[attr-defined]


def _trocar_vetores() -> None:
    import agno.vectordb.pgvector as pgvector_mod

    from agent_service.documents import collections

    collections.PgVector = VetoresEmMemoria  # type: ignore[assignment]
    pgvector_mod.PgVector = VetoresEmMemoria  # type: ignore[assignment]


def _sem_warm_up() -> None:
    """A lista de modelos do provedor vira uma fixa: o formulário de agente a pede ao
    abrir, e com a chave falsa a chamada ao Google só demoraria para falhar."""
    from agent_service.models import listing

    listing.warm_up = lambda: None  # type: ignore[assignment]

    def fixa(api_key: str | None, base_url: str | None) -> list[Any]:
        return [listing.ModelInfo(id="gemini-2.5-flash", label="Gemini 2.5 Flash")]

    for provedor in list(listing._LISTERS):
        listing._LISTERS[provedor] = fixa


# --------------------------------------------------------------------------- modelo

from agno.models.base import Model  # noqa: E402
from agno.models.message import Message  # noqa: E402
from agno.metrics import MessageMetrics  # noqa: E402
from agno.models.response import ModelResponse  # noqa: E402


def _ultima_mensagem(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            content = message.content
            if isinstance(content, str):
                return content.strip()
            return str(content or "").strip()
    return ""


@dataclass
class ModeloDeTeste(Model):
    """Responde `Resposta de teste: <última mensagem do usuário>`, sem tools e sem rede."""

    name: str = "ModeloDeTeste"
    provider: str = "teste"

    def _resposta(self, messages: list[Message]) -> ModelResponse:
        response = ModelResponse(role="assistant", content=RESPOSTA_PREFIXO + _ultima_mensagem(messages))
        response.response_usage = MessageMetrics(input_tokens=12, output_tokens=7, total_tokens=19)
        return response

    def invoke(self, messages: list[Message], assistant_message: Message, **kwargs: Any) -> ModelResponse:
        return self._resposta(messages)

    async def ainvoke(self, messages: list[Message], assistant_message: Message, **kwargs: Any) -> ModelResponse:
        return self._resposta(messages)

    def invoke_stream(self, messages: list[Message], assistant_message: Message, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self._resposta(messages)

    async def ainvoke_stream(
        self, messages: list[Message], assistant_message: Message, **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        yield self._resposta(messages)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


# --------------------------------------------------------------------------- vetores


class VetoresEmMemoria:
    """O suficiente da interface do `PgVector` para o serviço subir e listar collections.
    Nenhum documento é indexado: busca devolve vazio."""

    def __init__(self, table_name: str = "", embedder: Any = None, **kwargs: Any) -> None:
        self.table_name = table_name
        self.embedder = embedder
        self.table = None
        self.Session = None
        self.name = table_name
        self.description = None
        self.id = table_name

    def __getattr__(self, name: str) -> Any:
        # Qualquer outra operação do Agno (create, exists, delete...) é no-op.
        if name.startswith("a"):

            async def assincrono(*args: Any, **kwargs: Any) -> Any:
                return [] if "search" in name else None

            return assincrono

        def sincrono(*args: Any, **kwargs: Any) -> Any:
            return [] if "search" in name else None

        return sincrono


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=58100)
    args = parser.parse_args()

    raiz = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(raiz / "src"))
    pasta = preparar()
    print(f"[servico_de_teste] banco em {pasta}", flush=True)

    import uvicorn

    from agent_service.main import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
