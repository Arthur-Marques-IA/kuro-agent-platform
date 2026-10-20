"""Interface comum para backends de memória plugáveis.

O agente conversacional do MVP usa a memória "comum" (Postgres, via
agno.memory.manager.MemoryManager) para histórico/fatos de sessão. Esta
interface existe para permitir trocar ou combinar isso com outros backends
sem alterar o código do agente.
"""

from abc import ABC, abstractmethod
from typing import Any


class MemoryBackend(ABC):
    """Contrato mínimo que qualquer backend de memória precisa cumprir."""

    @abstractmethod
    def add(self, *, user_id: str, session_id: str, content: str) -> None:
        """Registra um fato/mensagem relevante para memória de longo prazo."""

    @abstractmethod
    def search(self, *, user_id: str, query: str, limit: int = 5) -> list[Any]:
        """Busca memórias relevantes para o usuário dado uma query."""
