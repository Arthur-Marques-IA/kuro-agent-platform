"""Memória de longo prazo e resumo de sessão, com o custo sob controle.

Os gerenciadores do Agno funcionam, mas do jeito padrão custam mais do que o
necessário num atendimento:

- **memória**: todas as memórias do usuário entram em todo prompt, e quem as
  extrai é o modelo do próprio agente. Aqui entram só as `limit` mais recentes, e
  a extração usa o modelo auxiliar (`AUX_MODEL_ID`, barato), quando configurado;
- **resumo de sessão**: o do Agno relê a conversa inteira depois de *cada*
  mensagem. Aqui ele é incremental — resumo anterior + as trocas que ainda não
  entraram — e só roda quando essas trocas enchem a janela de histórico
  (`num_history_runs`). Entre um resumo e outro, a conversa recente vai inteira
  no histórico, então nada se perde; o que sai da janela já está no resumo.
"""

import logging
from typing import Any

from agno.memory.manager import MemoryManager
from agno.models.base import Model
from agno.models.message import Message
from agno.session.summary import SessionSummaryManager

from agent_service.config import get_settings
from agent_service.db import get_db

logger = logging.getLogger(__name__)

MEMORY_TOOL_OUTPUT = "Memória atualizada."
"""O que o trace mostra no lugar do "No response from model" do Agno: o modelo de
memória só chama as funções de gravar e não escreve texto — isso é sucesso."""


def aux_model(agent_provider: str | None, agent_model_id: str | None, credential_id: str | None) -> Model | None:
    """O modelo das tarefas de apoio (extrair memória, resumir a sessão).

    `AUX_MODEL_ID` vazio = `None`: o Agno usa o modelo do próprio agente. Com ele
    definido, vale para o provedor de `AUX_MODEL_PROVIDER` (padrão: o do agente) —
    a credencial do agente só é reaproveitada quando o provedor é o mesmo."""
    from agent_service.models.provider import get_model

    settings = get_settings()
    if not settings.aux_model_id:
        return None
    provider = (settings.aux_model_provider or agent_provider or settings.default_model_provider).lower()
    same_provider = provider == (agent_provider or settings.default_model_provider).lower()
    return get_model(provider, settings.aux_model_id, credential_id=credential_id if same_provider else None)


class LimitedMemoryManager(MemoryManager):
    """`MemoryManager` que só põe no prompt as `limit` memórias mais recentes."""

    def __init__(self, *, limit: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.limit = limit

    def _latest(self, memories: Any) -> Any:
        if not memories or self.limit <= 0:
            return memories
        ordered = sorted(memories, key=lambda m: getattr(m, "updated_at", None) or 0)
        return ordered[-self.limit :]

    def get_user_memories(self, user_id: str | None = None) -> Any:  # type: ignore[override]
        return self._latest(super().get_user_memories(user_id=user_id))

    async def aget_user_memories(self, user_id: str | None = None) -> Any:  # type: ignore[override]
        return self._latest(await super().aget_user_memories(user_id=user_id))


def memory_manager(model: Model | None) -> LimitedMemoryManager:
    return LimitedMemoryManager(db=get_db(), model=model, limit=get_settings().memory_context_limit)


_SUMMARY_PROMPT = (
    "Você mantém o resumo de uma conversa de atendimento. Atualize o resumo anterior com as trocas novas: "
    "guarde o que importa para continuar o atendimento (quem é a pessoa, o que ela quer, o que já foi "
    "oferecido, respondido ou combinado, pendências). Seja conciso, em português, sem inventar nada."
)


class IncrementalSummaryManager(SessionSummaryManager):
    """Resumo incremental: resumo anterior + trocas novas, a cada `every` trocas."""

    def __init__(self, *, every: int, **kwargs: Any) -> None:
        super().__init__(session_summary_prompt=_SUMMARY_PROMPT, last_n_runs=max(every, 1), **kwargs)
        self.every = max(every, 1)

    def _due(self, session: Any) -> bool:
        runs = [r for r in (getattr(session, "runs", None) or []) if not getattr(r, "parent_run_id", None)]
        # Só quando a conversa passa da janela de histórico, e uma vez a cada janela:
        # antes disso o histórico já leva tudo e o resumo seria gasto à toa.
        return len(runs) > self.every and len(runs) % self.every == 0

    def get_system_message(self, conversation: list[Message], response_format: Any) -> Message:  # type: ignore[override]
        message = super().get_system_message(conversation, response_format)
        previous = getattr(self, "_previous_summary", None)
        if previous:
            message.content = f"Resumo anterior:\n{previous}\n\n{message.content}"
        return message

    def _remember_previous(self, session: Any) -> None:
        summary = getattr(session, "summary", None)
        self._previous_summary = getattr(summary, "summary", None) if summary else None

    def create_session_summary(self, session: Any, run_metrics: Any = None) -> Any:  # type: ignore[override]
        if not self._due(session):
            return None
        self._remember_previous(session)
        return super().create_session_summary(session=session, run_metrics=run_metrics)

    async def acreate_session_summary(self, session: Any, run_metrics: Any = None) -> Any:  # type: ignore[override]
        if not self._due(session):
            return None
        self._remember_previous(session)
        return await super().acreate_session_summary(session=session, run_metrics=run_metrics)


def summary_manager(model: Model | None, every: int) -> IncrementalSummaryManager:
    return IncrementalSummaryManager(model=model, every=every)


def memory_kwargs(mode: str, model: Model | None) -> dict[str, Any]:
    """Os parâmetros do `Agent` para cada modo de memória de longo prazo.

    - `none`: nada — nem tool, nem bloco no prompt, nem chamada extra;
    - `auto`: o Agno extrai a memória depois de cada resposta, em paralelo com o
      fim do run, com o modelo auxiliar; o modelo principal não vê tool nenhuma;
    - `agentic` (e o legado `common`): o modelo principal ganha a tool
      `update_user_memory` e decide quando gravar;
    - `mem0`: removido; um agente que ainda o tenha gravado roda como `none`."""
    if mode in ("none", "mem0"):
        return {"memory_manager": None, "enable_agentic_memory": False, "update_memory_on_run": False,
                "add_memories_to_context": False}
    manager = memory_manager(model)
    if mode == "auto":
        return {"memory_manager": manager, "enable_agentic_memory": False, "update_memory_on_run": True,
                "add_memories_to_context": True}
    return {"memory_manager": manager, "enable_agentic_memory": True, "update_memory_on_run": False,
            "add_memories_to_context": True}
