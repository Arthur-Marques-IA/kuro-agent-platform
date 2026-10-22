/**
 * A saída de um agente analista (`/analyze`) como árvore de campos para a tela do
 * run. A forma vem do valor que saiu — o `response_schema` atual pode já ter mudado
 * desde a versão que rodou — e o schema só dá rótulos e a ordem dos campos.
 */
import type { SchemaField } from "./types";

export interface OutputNode {
  /** Caminho no formato do `kuro eval` (`cliente.nome`, `parcelas[0].valor`). */
  path: string;
  key: string;
  label: string;
  kind: "object" | "array" | "value";
  value?: unknown;
  children?: OutputNode[];
}

/** O texto do run como objeto/array JSON; `null` se não for JSON estruturado. */
export function parseStructured(output: string | null | undefined): Record<string, unknown> | unknown[] | null {
  const text = output?.trim();
  if (!text || !(text.startsWith("{") || text.startsWith("["))) return null;
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed !== null && typeof parsed === "object" ? (parsed as Record<string, unknown> | unknown[]) : null;
  } catch {
    return null;
  }
}

function ordered(value: Record<string, unknown>, fields: SchemaField[] | null | undefined): string[] {
  const keys = Object.keys(value);
  if (!fields?.length) return keys;
  const known = fields.map((f) => f.name).filter((name) => keys.includes(name));
  return [...known, ...keys.filter((k) => !known.includes(k))];
}

function node(key: string, path: string, value: unknown, field: SchemaField | undefined, fields?: SchemaField[] | null): OutputNode {
  const label = field?.label || key;
  if (Array.isArray(value)) {
    const itemFields = field?.items?.fields ?? fields;
    return {
      path,
      key,
      label,
      kind: "array",
      children: value.map((item, i) => node(`[${i}]`, `${path}[${i}]`, item, undefined, itemFields)),
    };
  }
  if (value !== null && typeof value === "object") {
    const subFields = field?.fields ?? fields;
    const record = value as Record<string, unknown>;
    return {
      path,
      key,
      label,
      kind: "object",
      children: ordered(record, subFields).map((k) =>
        node(k, path ? `${path}.${k}` : k, record[k], subFields?.find((f) => f.name === k))
      ),
    };
  }
  return { path, key, label, kind: "value", value };
}

/** Os campos de primeiro nível, cada um com a sua subárvore. */
export function outputTree(value: Record<string, unknown> | unknown[], schema?: SchemaField[] | null): OutputNode[] {
  return node("", "", value, undefined, schema).children ?? [];
}

/** O valor de uma folha como texto curto (o `null` explícito aparece como "—"). */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "sim" : "não";
  return typeof value === "string" ? value : JSON.stringify(value);
}
