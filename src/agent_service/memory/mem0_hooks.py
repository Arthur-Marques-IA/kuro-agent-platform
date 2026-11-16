"""Plugagem do Mem0 num Agent via pre/post hooks do Agno.

A memória comum (`memory/common.py`) é gerenciada nativamente pelo Agno através
de `memory_manager=`. O Mem0 é um serviço externo, então a integração aqui usa
os hooks de execução do agente em vez de um `MemoryManager`:

- `mem0_pre_hook` roda antes do agente responder: busca memórias relevantes no
  Mem0 para o `user_id` da run e injeta em `run_context.dependencies["mem0_memories"]`
  e nas dependencies do prompt de sistema (`agents/system_context.py`), onde o modelo
  as lê no bloco `<additional context>`.
- `mem0_post_hook` roda depois do agente responder: grava a mensagem do
  usuário no Mem0 para consultas futuras.

Ver `agent_service.agents.base.build_agent(memory_backend="mem0")` para como
ligar isso a um agente.
"""

from agno.run import RunContext
from agno.run.agent import RunInput, RunOutput

from agent_service.agents.system_context import add_system_dependencies
from agent_service.memory.mem0_backend import Mem0MemoryBackend

_backend: Mem0MemoryBackend | None = None


def _get_backend() -> Mem0MemoryBackend:
    global _backend
    if _backend is None:
        _backend = Mem0MemoryBackend()
    return _backend


def mem0_pre_hook(run_input: RunInput, run_context: RunContext, user_id: str | None = None) -> None:
    if not user_id:
        return
    query = run_input.input_content if isinstance(run_input.input_content, str) else str(run_input.input_content)
    memories = _get_backend().search(user_id=user_id, query=query, limit=5)
    run_context.dependencies = {**(run_context.dependencies or {}), "mem0_memories": memories}
    add_system_dependencies({"mem0_memories": memories})


def mem0_post_hook(run_output: RunOutput, run_context: RunContext, user_id: str | None = None) -> None:
    if not user_id or not run_output.input:
        return
    content = run_output.input if isinstance(run_output.input, str) else str(run_output.input)
    _get_backend().add(user_id=user_id, session_id=run_context.session_id or "", content=content)
