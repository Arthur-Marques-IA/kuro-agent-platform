import type { ReactNode } from "react";
import type { FormErrors, FormSectionId, FormValues } from "@/lib/agent-form";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

export type FormVariant = "page" | "sheet";

/** O que toda seção do formulário recebe do `AgentForm`. */
export interface SectionProps {
  /** Prefixo dos ids dos campos (`useId` do formulário). */
  id: string;
  variant: FormVariant;
  mode: "create" | "edit";
  values: FormValues;
  update: <K extends keyof FormValues>(key: K, value: FormValues[K]) => void;
  /** O erro do campo, só depois da primeira tentativa de salvar. */
  fieldError: (key: keyof FormErrors) => string | null;
}

/** O id do elemento da seção na página — o alvo do índice lateral. */
export const sectionAnchor = (formId: string, section: FormSectionId) => `${formId}-section-${section}`;

export function FormSection({
  anchor,
  variant,
  title,
  description,
  action,
  children,
}: {
  anchor?: string;
  variant: FormVariant;
  title: string;
  description: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  if (variant === "sheet") {
    return (
      <section id={anchor} className="flex flex-col gap-3">
        <div className="flex items-start justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold">{title}</h3>
            <p className="text-xs text-muted-foreground">{description}</p>
          </div>
          {action}
        </div>
        {children}
      </section>
    );
  }
  return (
    <section
      id={anchor}
      // `scroll-mt`: ao pular pelo índice, a barra compacta (telas estreitas) não cobre o título.
      className="grid scroll-mt-16 gap-4 py-8 first:pt-2 lg:scroll-mt-4 xl:grid-cols-[220px_minmax(0,1fr)] xl:gap-8"
    >
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-semibold">{title}</h3>
          {action}
        </div>
        <p className="text-[13px] text-muted-foreground">{description}</p>
      </div>
      <Card className="p-5">{children}</Card>
    </section>
  );
}

export function ParamInput({
  id,
  label,
  value,
  onChange,
  placeholder,
  step,
  title,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  step?: string;
  title?: string;
}) {
  return (
    <label htmlFor={id} className="flex flex-col gap-1 text-xs text-muted-foreground" title={title}>
      {label}
      <Input
        id={id}
        type="number"
        inputMode="decimal"
        step={step ?? "1"}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="h-8"
      />
    </label>
  );
}
