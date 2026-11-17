/**
 * As checagens de um agente no formulário (`components/agents/form/checks-section.tsx`): a linha
 * editável, a conversão de e para o formato da API e a validação que o servidor também faz
 * (`observability/checks.py`) — para o erro aparecer antes do salvar.
 *
 * Sem imports de `@/`: roda direto no test runner do Node (`npm test`).
 */
import type { AgentCheck, CheckCondition, CheckOperator, CheckSeverity } from "./types";

/** Uma linha do editor: o valor e o `when` são texto (JSON quando possível), como o usuário digita. */
export interface CheckRow {
  name: string;
  field: string;
  op: CheckOperator;
  value: string;
  /** Vazio = sem condição: a checagem vale em todo run. */
  whenField: string;
  whenOp: CheckOperator;
  whenValue: string;
  severity: CheckSeverity;
}

export const CHECK_OPERATORS: { value: CheckOperator; label: string; needsValue: boolean; hint: string }[] = [
  { value: "eq", label: "é igual a", needsValue: true, hint: "valor exato (texto, número ou true/false)" },
  { value: "ne", label: "é diferente de", needsValue: true, hint: "valor exato" },
  { value: "in", label: "está em", needsValue: true, hint: 'lista JSON, ex.: ["a", "b"]' },
  { value: "not_in", label: "não está em", needsValue: true, hint: 'lista JSON, ex.: ["a", "b"]' },
  { value: "lt", label: "é menor que", needsValue: true, hint: "número" },
  { value: "lte", label: "é menor ou igual a", needsValue: true, hint: "número" },
  { value: "gt", label: "é maior que", needsValue: true, hint: "número" },
  { value: "gte", label: "é maior ou igual a", needsValue: true, hint: "número" },
  { value: "exists", label: "existe", needsValue: false, hint: "" },
  { value: "not_exists", label: "não existe", needsValue: false, hint: "" },
  { value: "contains", label: "contém", needsValue: true, hint: "um trecho de texto" },
  { value: "matches", label: "casa com (regex)", needsValue: true, hint: "expressão regular, até 200 caracteres" },
];

/** Os campos do run que uma checagem pode ler — os mesmos de `docs/observabilidade.md`. */
export const CHECK_FIELDS: { value: string; label: string }[] = [
  { value: "output", label: "output — o texto da resposta" },
  { value: "output_chars", label: "output_chars — tamanho da resposta" },
  { value: "status", label: "status — success, error ou interrupted" },
  { value: "latency_ms", label: "latency_ms — latência do run" },
  { value: "input_tokens", label: "input_tokens" },
  { value: "output_tokens", label: "output_tokens" },
  { value: "tool_calls", label: "tool_calls — quantas chamadas de tool" },
  { value: "tool_failures", label: "tool_failures — quantas falharam" },
  { value: "tools.<nome>.called", label: "tools.<nome>.called — a tool foi chamada?" },
  { value: "tools.<nome>.calls", label: "tools.<nome>.calls — quantas vezes" },
  { value: "tools.<nome>.ok", label: "tools.<nome>.ok — nenhuma chamada falhou" },
  { value: "tools.<nome>.args.<campo>", label: "tools.<nome>.args.<campo> — argumento da última chamada" },
  { value: "result.<campo>", label: "result.<campo> — saída estruturada (analista)" },
  { value: "dependencies.<campo>", label: "dependencies.<campo>" },
  { value: "metadata.<campo>", label: "metadata.<campo>" },
];

const STATIC_FIELDS = new Set([
  "output",
  "output_chars",
  "status",
  "latency_ms",
  "input_tokens",
  "output_tokens",
  "tool_calls",
  "tool_failures",
]);
const TOOL_FIELD = /^tools\.[^.]+\.(called|calls|ok|args\.[^.].*)$/;
const PREFIXED = ["result.", "dependencies.", "metadata."];

export function validField(field: string): boolean {
  if (STATIC_FIELDS.has(field) || TOOL_FIELD.test(field)) return true;
  return PREFIXED.some((p) => field.startsWith(p) && field.length > p.length && !field.endsWith("."));
}

export const EMPTY_CHECK_ROW: CheckRow = {
  name: "",
  field: "output_chars",
  op: "gte",
  value: "",
  whenField: "",
  whenOp: "eq",
  whenValue: "",
  severity: "fail",
};

const needsValue = (op: CheckOperator) => CHECK_OPERATORS.find((o) => o.value === op)?.needsValue ?? true;

/** O texto digitado vira JSON quando dá (`3`, `true`, `["a"]`); senão fica texto. */
export function parseValue(raw: string): unknown {
  const text = raw.trim();
  if (text === "") return "";
  try {
    return JSON.parse(text);
  } catch {
    return raw;
  }
}

function showValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function rowFromCheck(check: AgentCheck): CheckRow {
  return {
    name: check.name,
    field: check.field,
    op: check.op,
    value: showValue(check.value),
    whenField: check.when?.field ?? "",
    whenOp: check.when?.op ?? "eq",
    whenValue: showValue(check.when?.value),
    severity: check.severity ?? "fail",
  };
}

function condition(field: string, op: CheckOperator, value: string): CheckCondition {
  return needsValue(op) ? { field: field.trim(), op, value: parseValue(value) } : { field: field.trim(), op };
}

/** As linhas no formato do `PUT`/`POST` (a linha sem nome nem campo é descartada). */
export function checksFromRows(rows: CheckRow[]): AgentCheck[] {
  return rows
    .filter((row) => row.name.trim() || row.field.trim())
    .map((row) => ({
      name: row.name.trim(),
      ...condition(row.field, row.op, row.value),
      ...(row.whenField.trim() ? { when: condition(row.whenField, row.whenOp, row.whenValue) } : {}),
      severity: row.severity,
    }));
}

/** O primeiro problema das linhas, ou `null`. O servidor confere de novo (e confere o regex). */
export function checksProblem(rows: CheckRow[]): string | null {
  const seen = new Set<string>();
  for (const [i, row] of rows.entries()) {
    if (!row.name.trim() && !row.field.trim()) continue;
    const label = row.name.trim() || `#${i + 1}`;
    if (!row.name.trim()) return `Checagem ${label}: dê um nome.`;
    if (seen.has(row.name.trim())) return `Checagem ${label}: nome repetido.`;
    seen.add(row.name.trim());
    if (!validField(row.field.trim())) return `Checagem ${label}: campo desconhecido "${row.field}".`;
    if (needsValue(row.op) && row.value.trim() === "") return `Checagem ${label}: informe o valor.`;
    if (["lt", "lte", "gt", "gte"].includes(row.op) && typeof parseValue(row.value) !== "number") {
      return `Checagem ${label}: "${CHECK_OPERATORS.find((o) => o.value === row.op)?.label}" pede um número.`;
    }
    if (["in", "not_in"].includes(row.op) && !Array.isArray(parseValue(row.value))) {
      return `Checagem ${label}: use uma lista JSON, ex.: ["a", "b"].`;
    }
    if (row.whenField.trim()) {
      if (!validField(row.whenField.trim())) return `Checagem ${label}: campo da condição desconhecido "${row.whenField}".`;
      if (needsValue(row.whenOp) && row.whenValue.trim() === "") return `Checagem ${label}: informe o valor da condição.`;
    }
  }
  return null;
}
