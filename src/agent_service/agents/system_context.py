"""O texto que um run acrescenta ao prompt de sistema (o contexto vigente da sessão).

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
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_text: ContextVar[str | None] = ContextVar("kuro_system_context", default=None)


def set_system_context(text: str | None) -> None:
    """O texto do run em curso. Chame a cada run, com `None` quando não há contexto:
    assim o de um run anterior na mesma task nunca sobra para o próximo."""
    _text.set(text or None)


def current_system_context() -> str | None:
    return _text.get()


@contextmanager
def without_system_context() -> Iterator[None]:
    """Sem o texto do run, num trecho síncrono (ex.: copiar o agente)."""
    token = _text.set(None)
    try:
        yield
    finally:
        _text.reset(token)
