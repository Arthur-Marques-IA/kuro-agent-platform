"""Memória comum: usa o MemoryManager nativo do Agno sobre o Postgres compartilhado.

É o backend default do agente conversacional (histórico de sessão + memórias de
usuário geradas automaticamente pelo próprio Agno a partir das conversas).
"""

from typing import Any

from agno.db.schemas.memory import UserMemory
from agno.memory.manager import MemoryManager

from agent_service.db import get_db
from agent_service.memory.base import MemoryBackend


class CommonMemoryBackend(MemoryBackend):
    def __init__(self) -> None:
        self._manager = MemoryManager(db=get_db())

    @property
    def manager(self) -> MemoryManager:
        """Instância a ser passada como `memory_manager=` na construção do Agent."""
        return self._manager

    def add(self, *, user_id: str, session_id: str, content: str) -> None:
        self._manager.add_user_memory(memory=UserMemory(memory=content), user_id=user_id)

    def search(self, *, user_id: str, query: str, limit: int = 5) -> list[Any]:
        return self._manager.search_user_memories(user_id=user_id, query=query, limit=limit, retrieval_method="agentic")
