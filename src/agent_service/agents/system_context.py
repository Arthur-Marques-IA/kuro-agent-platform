"""O que um run acrescenta ao prompt de sistema: o contexto vigente da sessão e as `dependencies`.

O agente fica em cache e é compartilhado por todas as requisições do mesmo tipo
(`agents/registry.py`): mudar um atributo dele por requisição vazaria o contexto de
uma conversa para outra que estivesse rodando ao mesmo tempo. O texto do run viaja
então por um `ContextVar`, como as `dependencies` das tools (`tools/context.py`): cada
requisição roda na sua task, com o seu contexto, e uma task criada depois do `set`
(a do Langfuse, a do sse-starlette) o herda.

Quem lê é `agents/base.py::KuroAgent.additional_context`, que o Agno acrescenta ao fim
do prompt de sistema, **depois** de formatar as variáveis de `session_state`/
`dependencies` (`resolve_in_context`): o conteúdo dos blocos, com `{}` e `$` à
vontade, chega ao modelo como veio, sem ser lido como template. O prompt de sistema
é remontado a cada execução e não volta como histórico, e nada disto passa pela
mensagem do usuário, que é o que o Agno grava e reenvia nos turnos seguintes.

As `dependencies` do run seguem o mesmo caminho, num bloco próprio **depois** do contexto
da sessão (o fim do prompt, junto da conversa): `<additional context>` com o JSON, o
formato que o Agno punha na mensagem do usuário. Elas valem só para o turno que as
mandou, então o valor viaja por um `ContextVar` separado e nunca é lembrado entre
turnos. O dicionário é o do run e pode ganhar chaves durante ele (o hook do Mem0 põe
`mem0_memories` antes de o prompt ser montado): por isso o bloco é renderizado na hora
de montar o prompt, não antes.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import json
from typing import Any

_text: ContextVar[str | None] = ContextVar("kuro_system_context", default=None)


def set_system_context(text: str | None) -> None:
    """O texto do run em curso. Chame a cada run, com `None` quando não há contexto:
    assim o de um run anterior na mesma task nunca sobra para o próximo."""
    _text.set(text or None)


def current_system_context() -> str | None:
    return _text.get()


_dependencies: ContextVar[dict[str, Any] | None] = ContextVar("kuro_system_dependencies", default=None)

DEPENDENCIES_OPEN = "<additional context>"
DEPENDENCIES_CLOSE = "</additional context>"


def set_system_dependencies(dependencies: dict[str, Any] | None) -> None:
    """As `dependencies` do run em curso (uma cópia). Chame a cada run, com `None` quando
    não há: as de um run anterior na mesma task nunca sobram para o próximo."""
    _dependencies.set(dict(dependencies) if dependencies else {})


def add_system_dependencies(extra: dict[str, Any]) -> None:
    """Acrescenta chaves às `dependencies` do run em curso (hooks que rodam antes do
    prompt, como o do Mem0). Muda o dicionário do run no lugar, para valer também quando
    o hook roda numa thread com cópia do contexto."""
    current = _dependencies.get()
    if current is not None:
        current.update(extra)


def render_dependencies(dependencies: dict[str, Any]) -> str:
    """O bloco que o Agno punha na mensagem do usuário: o JSON entre as marcas."""
    try:
        body = json.dumps(dependencies, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError, OverflowError):
        safe: dict[str, Any] = {}
        for key, value in dependencies.items():
            try:
                json.dumps({key: value}, default=str)
                safe[key] = value
            except Exception:  # noqa: BLE001 - valor que não vira JSON: texto
                safe[key] = str(value)
        body = json.dumps(safe, indent=2, default=str, ensure_ascii=False)
    return f"{DEPENDENCIES_OPEN}\n{body}\n{DEPENDENCIES_CLOSE}"


def current_system_dependencies() -> str | None:
    """O bloco das `dependencies` do run, ou `None` sem nenhuma."""
    deps = _dependencies.get()
    return render_dependencies(deps) if deps else None


@contextmanager
def without_system_context() -> Iterator[None]:
    """Sem o texto nem as dependencies do run, num trecho síncrono (ex.: copiar o agente)."""
    token = _text.set(None)
    deps_token = _dependencies.set(None)
    try:
        yield
    finally:
        _dependencies.reset(deps_token)
        _text.reset(token)
