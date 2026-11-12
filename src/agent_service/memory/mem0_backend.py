"""Backend de memória usando Mem0.

Ponto de extensão preparado para a fase 2 (ver roadmap no README): a interface
já é compatível com `MemoryBackend`, mas ainda não é usada como default por
nenhum agente. Para ativar, chame `get_memory_backend("mem0")` e plugue o
resultado no agente desejado (ex. como fonte de memórias antes/depois do
`memory_manager` comum).

Requer o pacote opcional `mem0ai` e `settings.mem0_api_key`/`mem0_enabled`.
"""

from typing import Any

from agent_service.config import get_settings
from agent_service.memory.base import MemoryBackend


class Mem0NotConfiguredError(RuntimeError):
    pass


class Mem0MemoryBackend(MemoryBackend):
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.mem0_enabled:
            raise Mem0NotConfiguredError("Mem0 não está habilitado. Defina MEM0_ENABLED=true e MEM0_API_KEY no .env.")
        try:
            from mem0 import MemoryClient
        except ImportError as exc:
            raise Mem0NotConfiguredError("Pacote 'mem0ai' não instalado. Rode: uv add mem0ai") from exc

        self._client = MemoryClient(api_key=settings.mem0_api_key)

    def add(self, *, user_id: str, session_id: str, content: str) -> None:
        self._client.add(
            messages=[{"role": "user", "content": content}],
            user_id=user_id,
            metadata={"session_id": session_id},
        )

    def search(self, *, user_id: str, query: str, limit: int = 5) -> list[Any]:
        return self._client.search(query=query, user_id=user_id, limit=limit)
