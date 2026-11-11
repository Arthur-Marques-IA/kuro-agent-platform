import { MEMORY_BACKENDS } from "@/lib/agent-meta";
import type { AgentDefinition } from "@/lib/types";

/** "Memória: desligada" — o selo diz do que é o valor, em vez de um "Desligada" solto. */
export function memoryLabel(agent: Pick<AgentDefinition, "memory_backend">): string {
  const label = MEMORY_BACKENDS[agent.memory_backend]?.label ?? agent.memory_backend;
  return `Memória: ${label === "Mem0" ? label : label.toLowerCase()}`;
}
