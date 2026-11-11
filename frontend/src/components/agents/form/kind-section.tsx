import { cn } from "@/lib/cn";
import type { AgentKind } from "@/lib/types";
import { FormSection, type SectionProps } from "./form-section";

const AGENT_KINDS: { kind: AgentKind; label: string; description: string }[] = [
  {
    kind: "conversational",
    label: "Conversacional",
    description: "Mantém a conversa por session_id e responde texto. Aceita nota de feedback.",
  },
  {
    kind: "analysis",
    label: "Analista",
    description: "One-shot no /analyze: recebe um documento e devolve o objeto do response_schema.",
  },
  {
    kind: "procedural",
    label: "Procedural",
    description: "Fluxo em etapas no /chat: coleta dados, confirma e executa uma ação. O servidor conduz as etapas.",
  },
];

export function KindSection({
  id,
  variant,
  mode,
  values,
  anchor,
  onKindChange,
}: SectionProps & { anchor?: string; onKindChange: (kind: AgentKind) => void }) {
  return (
    <FormSection
      anchor={anchor}
      variant={variant}
      title="Tipo"
      description="Conversacional mantém sessão e histórico e responde texto. Analista é one-shot: recebe um documento e devolve um objeto estruturado. Procedural conduz a conversa por etapas definidas aqui."
    >
      <div className="grid gap-2 sm:grid-cols-3">
        {AGENT_KINDS.map(({ kind, label, description }) => {
          const selected = values.kind === kind;
          return (
            <label
              key={kind}
              className={cn(
                "flex cursor-pointer gap-3 rounded-lg border p-3 transition-colors",
                selected ? "border-primary bg-primary-soft/50 ring-1 ring-primary" : "border-input hover:bg-accent/50",
                mode === "edit" && !selected && "cursor-not-allowed opacity-50"
              )}
            >
              <input
                type="radio"
                name={`${id}-kind`}
                value={kind}
                checked={selected}
                disabled={mode === "edit"}
                onChange={() => onKindChange(kind)}
                className="mt-0.5 accent-primary"
              />
              <span className="flex flex-col gap-0.5">
                <span className="text-[13px] font-medium">{label}</span>
                <span className="text-xs text-muted-foreground">{description}</span>
              </span>
            </label>
          );
        })}
      </div>
      {mode === "edit" && (
        <p className="mt-2 text-xs text-muted-foreground">
          O tipo não muda depois de criado: ele define o endpoint e o corpo que quem integra usa.
        </p>
      )}
    </FormSection>
  );
}
