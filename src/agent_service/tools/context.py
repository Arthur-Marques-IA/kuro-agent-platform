"""`dependencies` da requisição em curso, visível para as tools.

Um parâmetro de tool com `source="dependency"` (ver `tools/api_tool.py`) é
preenchido pelo servidor a partir do `dependencies` do `/chat` — não pelo
modelo, que nem chega a ver esse parâmetro no schema. O valor viaja por um
`ContextVar` porque a tool é chamada lá dentro do agent loop do Agno, longe
da requisição: o Agno chama `entrypoint(**argumentos)` sem por onde passar
contexto nosso.

Cada requisição do FastAPI roda na sua própria task, portanto no seu próprio
contexto — um `set` aqui não vaza para outra requisição. Vale também para a
tool executada numa thread (`asyncio.to_thread` copia o contexto).

O `dry_run` da requisição viaja do mesmo jeito: com ele ligado, toda chamada
HTTP de tool leva o header `X-Kuro-Dry-Run: true`, e `dependencies.dry_run`
fica disponível para parâmetros `source="dependency"` sem o agente declarar o
campo. Quem implementa a API da tool decide o que simular — o Kuro só avisa.

O `Idempotency-Key` de um `/chat` ou `/analyze` (`api/idempotency.py`) também: vira
`dependencies.idempotency_key` para parâmetros `source="dependency"`, e vence um valor
que viesse nas `dependencies` da requisição. É uma chave por requisição — duas chamadas
da mesma tool no mesmo turno recebem a mesma. Fora de um run (o teste de uma tool, a
etapa `action` de um procedural) vale a das `dependencies` do bloco.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_dependencies: ContextVar[dict[str, Any]] = ContextVar("tool_dependencies", default={})
_dry_run: ContextVar[bool] = ContextVar("tool_dry_run", default=False)
_idempotency_key: ContextVar[str | None] = ContextVar("tool_idempotency_key", default=None)

DRY_RUN_HEADER = "X-Kuro-Dry-Run"
DRY_RUN_DEPENDENCY = "dry_run"
"""Nome reservado em `dependencies`: o servidor preenche, quem chama não precisa."""
IDEMPOTENCY_DEPENDENCY = "idempotency_key"
"""Também preenchido pelo servidor: no procedural, a chave da etapa `action`; no `/chat`
e no `/analyze`, o header `Idempotency-Key`, quando vem."""


def set_dependencies(dependencies: dict[str, Any] | None) -> None:
    _dependencies.set(dict(dependencies or {}))


def get_dependencies() -> dict[str, Any]:
    return _dependencies.get()


def set_dry_run(dry_run: bool) -> None:
    _dry_run.set(bool(dry_run))


def is_dry_run() -> bool:
    return _dry_run.get()


def set_idempotency_key(key: str | None) -> None:
    _idempotency_key.set(key)


def get_idempotency_key() -> str | None:
    return _idempotency_key.get()


@contextmanager
def dependencies_scope(dependencies: dict[str, Any] | None, *, dry_run: bool = False):
    """Define as dependências só durante o bloco — usado pelo teste de tool
    (`POST /tools/{nome}/invoke`), que não passa por um run de agente."""
    token = _dependencies.set(dict(dependencies or {}))
    dry_token = _dry_run.set(bool(dry_run))
    # A chave do bloco é a das `dependencies` (a da etapa, no procedural), não a do request.
    key_token = _idempotency_key.set(None)
    try:
        yield
    finally:
        _idempotency_key.reset(key_token)
        _dry_run.reset(dry_token)
        _dependencies.reset(token)
