import { cn } from "@/lib/cn";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/ui/primitives";
import { CopyButton } from "@/components/ui/copy-button";
import { FormSection, type SectionProps } from "./form-section";

export function IdentitySection({
  id,
  variant,
  mode,
  values,
  fieldError,
  anchor,
  onNameChange,
  onSlugChange,
}: SectionProps & { anchor?: string; onNameChange: (name: string) => void; onSlugChange: (slug: string) => void }) {
  return (
    <FormSection
      anchor={anchor}
      variant={variant}
      title="Identidade"
      description="Como o agente aparece no console e como outros módulos o chamam na API."
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Nome" htmlFor={`${id}-name`} error={fieldError("name")}>
          <Input
            id={`${id}-name`}
            value={values.name}
            onChange={(e) => onNameChange(e.target.value)}
            placeholder="ex.: Agente de Suporte"
            aria-invalid={Boolean(fieldError("name"))}
            autoFocus={mode === "create"}
          />
        </Field>
        <Field
          label="Slug (agent_type)"
          htmlFor={`${id}-slug`}
          error={fieldError("agentType")}
          hint={
            mode === "edit"
              ? "Identificador estável — não pode ser alterado."
              : "Gerado a partir do nome; usado no campo agent_type da API."
          }
        >
          <div className="relative">
            <Input
              id={`${id}-slug`}
              value={values.agentType}
              onChange={(e) => onSlugChange(e.target.value)}
              placeholder="ex.: suporte-nivel-1"
              disabled={mode === "edit"}
              aria-invalid={Boolean(fieldError("agentType"))}
              className={cn("font-mono text-[13px]", mode === "edit" && "pr-10")}
              spellCheck={false}
            />
            {mode === "edit" && (
              <CopyButton value={values.agentType} label="Copiar slug" className="absolute right-1 top-1" />
            )}
          </div>
        </Field>
      </div>
    </FormSection>
  );
}
