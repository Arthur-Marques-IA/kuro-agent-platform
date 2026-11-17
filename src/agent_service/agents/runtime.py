"""A camada fina entre as rotas e os tipos do Agno.

As rotas (`api/routes.py`) não conhecem os tipos do Agno: recebem o agente
montado como um valor opaco (`RunnableAgent`) e leem cada evento do run por
`read_event`, que devolve um `RunStep` com o que elas usam — o tipo do evento,
o conteúdo e, sob demanda, o uso (tokens/custo). Trocar ou atualizar o Agno
mexe aqui, não no contrato HTTP.

O que já estava atrás de um módulo do projeto continua lá: os anexos
(`agents/attachments.py`) e o laço do run com trace e timeout
(`observability/tracing.py::traced_run_events`). Este módulo não envolve o
gerador do run de propósito: uma camada a mais de gerador mudaria como o
fechamento e o cancelamento chegam ao trace (cliente que desiste do stream).
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from agno.agent import Agent
from agno.run.agent import RunEvent

RunnableAgent = Agent
"""O agente pronto para rodar, como `agents/registry.py` monta. Para as rotas é opaco."""

StepKind = Literal["content", "completed", "error", "other"]


@dataclass(frozen=True)
class RunStep:
    """Um evento do run, no vocabulário das rotas.

    `content` vem como o Agno entregou: texto num trecho (`content`) ou no fim de
    um agente conversacional, o objeto validado contra o `response_schema` no fim
    de um analista, a mensagem de erro em `error`."""

    kind: StepKind
    content: Any = None
    _metrics: Any = field(default=None, repr=False, compare=False)

    def usage(self) -> dict[str, Any] | None:
        """As métricas do run (tokens, custo, duração) em dict, só no `completed`
        que as trouxe. Calculado só quando alguém pede — o `/chat` não pede."""
        return self._metrics.to_dict() if self._metrics else None


def read_event(event: Any) -> RunStep:
    """Traduz um evento de `traced_run_events` (comparação por `==`, como o Agno
    compara: o `event.event` é o valor do `RunEvent`)."""
    name = event.event
    if name == RunEvent.run_content.value:
        return RunStep("content", event.content)
    if name == RunEvent.run_completed.value:
        return RunStep("completed", event.content, getattr(event, "metrics", None))
    if name == RunEvent.run_error.value:
        return RunStep("error", event.content)
    return RunStep("other")
