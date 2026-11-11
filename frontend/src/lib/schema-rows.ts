/**
 * Um `response_schema` (ou os campos compostos de uma tool) como linhas de tabela,
 * para ler sem abrir o editor: cada campo vira uma linha, e os de dentro de um
 * `object` ou dos itens `object` de um `array` vêm logo abaixo, um nível mais fundo.
 */
import type { SchemaField } from "./types";

export interface SchemaRow {
  /** Caminho do campo: `cliente.nome`, `parcelas[].valor` (o `[]` é "cada item"). */
  path: string;
  name: string;
  label?: string;
  /** 0 no primeiro nível; serve para indentar. */
  depth: number;
  /** Tipo legível: `string`, `object`, `array de number`. */
  type: string;
  required: boolean;
  description?: string;
  /** Valores permitidos do campo — num array, os de cada item. */
  enum?: (string | number)[];
}

export function schemaRows(fields: SchemaField[] | null | undefined, depth = 0, prefix = ""): SchemaRow[] {
  const rows: SchemaRow[] = [];
  for (const field of fields ?? []) {
    const path = prefix ? `${prefix}.${field.name}` : field.name;
    const items = field.type === "array" ? field.items : undefined;
    const allowed = items ? items.enum : field.enum;
    rows.push({
      path,
      name: field.name,
      label: field.label || undefined,
      depth,
      type: items ? `array de ${items.type}` : field.type,
      required: Boolean(field.required),
      description: field.description || undefined,
      enum: allowed?.length ? allowed : undefined,
    });
    if (field.type === "object") rows.push(...schemaRows(field.fields, depth + 1, path));
    if (items?.type === "object") rows.push(...schemaRows(items.fields, depth + 1, `${path}[]`));
  }
  return rows;
}
