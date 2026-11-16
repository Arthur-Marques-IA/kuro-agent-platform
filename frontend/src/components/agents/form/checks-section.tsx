import { Plus, Trash } from "lucide-react";
import { CHECK_FIELDS, CHECK_OPERATORS, EMPTY_CHECK_ROW, type CheckRow } from "@/lib/agent-checks";
import type { CheckOperator, CheckSeverity } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Field } from "@/components/ui/primitives";
import { FormSection, type SectionProps } from "./form-section";

const needsValue = (op: CheckOperator) => CHECK_OPERATORS.find((o) => o.value === op)?.needsValue ?? true;

/** Checagens determinísticas avaliadas em toda execução real (sem modelo). */
export function ChecksSection({ id, variant, values, update, fieldError, anchor }: SectionProps & { anchor?: string }) {
  const rows = values.checks;
  const set = (index: number, patch: Partial<CheckRow>) =>
    update(
      "checks",
      rows.map((row, i) => (i === index ? { ...row, ...patch } : row))
    );

  return (
    <FormSection
      anchor={anchor}
      variant={variant}
      title="Checagens"
      description="Regras simples, sem modelo, avaliadas em toda execução real. O resultado fica no run e filtra a fila de revisão. Não julgam a qualidade do texto: dizem se a resposta veio vazia, se uma tool obrigatória não foi chamada, se uma tool com efeito colateral rodou duas vezes."
    >
      <Field label="Checagens" error={fieldError("checks")}>
        <div className="flex flex-col gap-3">
          <datalist id={`${id}-check-fields`}>
            {CHECK_FIELDS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </datalist>
          {rows.map((row, i) => (
            <div key={i} className="flex flex-col gap-2 rounded-lg border border-border p-3">
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  aria-label="Nome da checagem"
                  placeholder="Nome (ex.: resposta curta)"
                  className="min-w-40 flex-1"
                  value={row.name}
                  onChange={(e) => set(i, { name: e.target.value })}
                />
                <Select
                  aria-label="Severidade"
                  className="w-28"
                  value={row.severity}
                  onChange={(e) => set(i, { severity: e.target.value as CheckSeverity })}
                >
                  <option value="fail">falha</option>
                  <option value="warn">aviso</option>
                </Select>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={`Remover a checagem ${row.name || i + 1}`}
                  onClick={() =>
                    update(
                      "checks",
                      rows.filter((_, j) => j !== i)
                    )
                  }
                >
                  <Trash className="size-4" />
                </Button>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  aria-label="Campo do run"
                  list={`${id}-check-fields`}
                  placeholder="campo (ex.: output_chars)"
                  className="min-w-48 flex-1 font-mono text-xs"
                  value={row.field}
                  onChange={(e) => set(i, { field: e.target.value })}
                />
                <Select
                  aria-label="Operador"
                  className="w-48"
                  value={row.op}
                  onChange={(e) => set(i, { op: e.target.value as CheckOperator })}
                >
                  {CHECK_OPERATORS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </Select>
                {needsValue(row.op) && (
                  <Input
                    aria-label="Valor"
                    placeholder={CHECK_OPERATORS.find((o) => o.value === row.op)?.hint}
                    className="min-w-40 flex-1"
                    value={row.value}
                    onChange={(e) => set(i, { value: e.target.value })}
                  />
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted-foreground">Só quando</span>
                <Input
                  aria-label="Campo da condição"
                  list={`${id}-check-fields`}
                  placeholder="(sempre)"
                  className="min-w-40 flex-1 font-mono text-xs"
                  value={row.whenField}
                  onChange={(e) => set(i, { whenField: e.target.value })}
                />
                {row.whenField.trim() && (
                  <>
                    <Select
                      aria-label="Operador da condição"
                      className="w-48"
                      value={row.whenOp}
                      onChange={(e) => set(i, { whenOp: e.target.value as CheckOperator })}
                    >
                      {CHECK_OPERATORS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </Select>
                    {needsValue(row.whenOp) && (
                      <Input
                        aria-label="Valor da condição"
                        className="min-w-32 flex-1"
                        value={row.whenValue}
                        onChange={(e) => set(i, { whenValue: e.target.value })}
                      />
                    )}
                  </>
                )}
              </div>
            </div>
          ))}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="self-start"
            onClick={() => update("checks", [...rows, { ...EMPTY_CHECK_ROW }])}
          >
            <Plus className="size-4" /> Adicionar checagem
          </Button>
        </div>
      </Field>
    </FormSection>
  );
}
