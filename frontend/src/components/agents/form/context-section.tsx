import { Plus, Trash } from "lucide-react";
import type { DependencyField, DependencyFieldType } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { FormSection, type SectionProps } from "./form-section";

const DEPENDENCY_FIELD_TYPES: DependencyFieldType[] = ["string", "integer", "number", "boolean"];

export function ContextSection({ variant, values, update, anchor }: SectionProps & { anchor?: string }) {
  return (
    <FormSection
      anchor={anchor}
      variant={variant}
      title="Campos de contexto (dependencies)"
      description="Quais chaves o agente espera no dependencies do /chat (ex.: cpf) — o que não for declarado aqui continua passando livre, sem validação."
    >
      <DependencyFieldsEditor
        fields={values.dependencyFields}
        onChange={(dependencyFields) => update("dependencyFields", dependencyFields)}
      />
    </FormSection>
  );
}

function emptyDependencyField(): DependencyField {
  return { name: "", type: "string", label: "", description: "", required: false, default: null };
}

function DependencyFieldsEditor({
  fields,
  onChange,
}: {
  fields: DependencyField[];
  onChange: (fields: DependencyField[]) => void;
}) {
  function updateField(index: number, patch: Partial<DependencyField>) {
    onChange(fields.map((f, i) => (i === index ? { ...f, ...patch } : f)));
  }

  return (
    <div className="flex flex-col gap-3">
      {fields.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          Nenhum campo declarado — qualquer <code className="font-mono">dependencies</code> enviado passa sem validação.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          {fields.map((f, i) => (
            <div key={i} className="grid grid-cols-[1fr_1fr_110px_auto_auto] items-center gap-1.5">
              <Input
                placeholder="nome (ex.: cpf)"
                value={f.name}
                onChange={(e) => updateField(i, { name: e.target.value })}
                className="font-mono text-xs"
              />
              <Input
                placeholder="rótulo (opcional)"
                value={f.label ?? ""}
                onChange={(e) => updateField(i, { label: e.target.value })}
                className="text-xs"
              />
              <Select value={f.type} onChange={(e) => updateField(i, { type: e.target.value as DependencyFieldType })}>
                {DEPENDENCY_FIELD_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </Select>
              <label className="flex items-center gap-1 text-xs text-muted-foreground" title="Obrigatório">
                <input
                  type="checkbox"
                  checked={f.required}
                  onChange={(e) => updateField(i, { required: e.target.checked })}
                  className="accent-primary"
                />
                obrig.
              </label>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Remover campo"
                onClick={() => onChange(fields.filter((_, idx) => idx !== i))}
              >
                <Trash />
              </Button>
            </div>
          ))}
        </div>
      )}
      <Button variant="outline" size="sm" className="w-fit" onClick={() => onChange([...fields, emptyDependencyField()])}>
        <Plus /> Campo
      </Button>
    </div>
  );
}
