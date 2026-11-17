import Link from "next/link";
import { Wrench } from "lucide-react";
import { cn } from "@/lib/cn";
import { TOOL_KIND_META } from "@/lib/agent-meta";
import type { ToolSummary } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { FormSection, type SectionProps } from "./form-section";

export function ToolsSection({
  variant,
  values,
  update,
  anchor,
  availableTools,
}: SectionProps & { anchor?: string; availableTools: ToolSummary[] }) {
  function toggleTool(tool: string) {
    update("tools", values.tools.includes(tool) ? values.tools.filter((t) => t !== tool) : [...values.tools, tool]);
  }

  return (
    <FormSection
      anchor={anchor}
      variant={variant}
      title="Tools"
      description={
        values.kind === "procedural"
          ? "Consultas que o modelo pode fazer ao redigir a resposta. Tool com efeito colateral entra como etapa de ação, não aqui."
          : "Funções que o agente pode chamar durante a execução."
      }
      action={
        <Link href="/tools" className="inline-flex items-center gap-1 text-xs text-primary hover:underline">
          <Wrench className="size-3" /> Gerenciar tools
        </Link>
      }
    >
      {availableTools.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-4 py-3 text-[13px] text-muted-foreground">
          Nenhuma tool criada ainda.{" "}
          <Link href="/tools" className="text-primary hover:underline">
            Crie uma
          </Link>{" "}
          (toolkit padrão do Agno, API ou Python) e ela aparece aqui.
        </p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {availableTools.map((tool) => {
            // Num procedural, efeito só por etapa de ação — depois da confirmação.
            const blocked =
              values.kind === "procedural" && tool.side_effect === true && !values.tools.includes(tool.tool_name);
            return (
              <label
                key={tool.tool_name}
                title={blocked ? "Tem efeito colateral: use como etapa de ação" : (tool.description ?? undefined)}
                className={cn(
                  "flex cursor-pointer items-start gap-2.5 rounded-lg border border-input px-3 py-2 text-[13px] hover:bg-accent/50",
                  (!tool.enabled || blocked) && "opacity-60",
                  blocked && "cursor-not-allowed"
                )}
              >
                <input
                  type="checkbox"
                  checked={values.tools.includes(tool.tool_name)}
                  disabled={blocked}
                  onChange={() => toggleTool(tool.tool_name)}
                  className="mt-0.5 accent-primary"
                />
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="flex items-center gap-1.5">
                    <span className="truncate font-medium">{tool.label}</span>
                    <Badge variant="outline" className="shrink-0">
                      {TOOL_KIND_META[tool.kind].label}
                    </Badge>
                    {!tool.enabled && (
                      <Badge variant="secondary" className="shrink-0">
                        desativada
                      </Badge>
                    )}
                  </span>
                  <span className="truncate font-mono text-xs text-muted-foreground">{tool.tool_name}</span>
                </span>
              </label>
            );
          })}
        </div>
      )}
    </FormSection>
  );
}
