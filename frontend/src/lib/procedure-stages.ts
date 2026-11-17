/** Limpeza das etapas de um agente procedural antes de ir à API. Sem imports de `@/` (`npm test`). */
import type { ProcedureStage } from "./types";

/** Só o que o backend aceita: o editor guarda campos vazios enquanto se digita. */
export function cleanStages(stages: ProcedureStage[]): ProcedureStage[] {
  return stages.map((stage) => {
    const base = { id: stage.id, type: stage.type, goal: stage.goal.trim() };
    if (stage.type === "collect") {
      return {
        ...base,
        fields: (stage.fields ?? []).map((f) => ({
          ...f,
          label: f.label || f.name,
          enum: f.enum && f.enum.length > 0 ? f.enum : undefined,
          pattern: f.pattern ? f.pattern : undefined,
        })),
      };
    }
    if (stage.type === "action") return { ...base, tool: stage.tool, ...(stage.function ? { function: stage.function } : {}) };
    return base;
  });
}
