import { Plus } from "lucide-react";
import type { ToolSummary } from "@/lib/types";
import { SchemaFieldsEditor, emptyField } from "@/components/schema/schema-fields-editor";
import { StagesEditor } from "@/components/agents/stages-editor";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/primitives";
import { FormSection, type SectionProps } from "./form-section";

/** Etapas de um agente procedural. */
export function StagesSection({
  variant,
  values,
  update,
  fieldError,
  anchor,
  availableTools,
}: SectionProps & { anchor?: string; availableTools: ToolSummary[] }) {
  return (
    <FormSection
      anchor={anchor}
      variant={variant}
      title="Etapas"
      description="O fluxo que o servidor conduz, na ordem. A etapa atual é sempre a primeira que ainda não está pronta: corrigir um dado volta para a etapa dele. Uma ação só roda depois de uma confirmação."
    >
      <Field label="Etapas" error={fieldError("stages")}>
        <StagesEditor stages={values.stages} onChange={(stages) => update("stages", stages)} tools={availableTools} />
      </Field>
    </FormSection>
  );
}

/** A saída estruturada (`response_schema`) de um agente analista. */
export function OutputSection({ variant, values, update, fieldError, anchor }: SectionProps & { anchor?: string }) {
  return (
    <FormSection
      anchor={anchor}
      variant={variant}
      title="Saída (response_schema)"
      description="A forma do objeto que o /analyze devolve. O modelo é obrigado a preencher exatamente estes campos."
    >
      <Field label="Campos" error={fieldError("responseSchema")}>
        <SchemaFieldsEditor
          value={values.responseSchema}
          onChange={(responseSchema) => update("responseSchema", responseSchema)}
        />
      </Field>
      {values.responseSchema.length === 0 && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="mt-2"
          onClick={() => update("responseSchema", [emptyField()])}
        >
          <Plus className="size-4" /> Começar com um campo
        </Button>
      )}
    </FormSection>
  );
}
