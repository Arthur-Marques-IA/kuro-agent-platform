from agent_service.memory.base import MemoryBackend
from agent_service.memory.common import CommonMemoryBackend

__all__ = ["MemoryBackend", "CommonMemoryBackend", "get_memory_backend"]


def get_memory_backend(name: str = "common") -> MemoryBackend:
    if name == "common":
        return CommonMemoryBackend()
    raise ValueError(f"Backend de memória desconhecido: {name!r}")
