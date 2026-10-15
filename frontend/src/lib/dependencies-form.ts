/**
 * Conta do formulário de `dependencies` (e de argumentos de tool) do console.
 *
 * A fonte da verdade é sempre o texto JSON — é ele que o Playground persiste por
 * agente e compara com o preset de teste. O formulário só lê esse texto, edita um
 * campo e devolve o texto de novo, preservando a ordem das chaves e tudo o que
 * não está declarado (campos extras passam livres no backend, então não podem se
 * perder no caminho).
 *
 * Sem dependência de runtime: roda com `npm test` (node --test).
 */
import type { ApiToolParam, FieldType, SchemaField, SchemaItem } from "./types";

/** Campo do formulário: o vocabulário de `field_schema.py` (DependencyField é um subconjunto). */
export interface FormField {
  name: string;
  type: FieldType;
  label?: string | null;
  description?: string | null;
  required?: boolean;
  default?: unknown;
  enum?: (string | number)[] | null;
  fields?: SchemaField[] | null;
  items?: SchemaItem | null;
}

export type JsonObject = Record<string, unknown>;

/** Reservado pelo backend: `dependencies.dry_run` é injetado pelo modo teste. */
const DRY_RUN_DEPENDENCY = "dry_run";

export function isPlainObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Texto → objeto. `""` é objeto vazio; JSON que não é objeto vira erro. */
export function parseObjectText(text: string): { value: JsonObject | null; error: string | null } {
  if (!text.trim()) return { value: {}, error: null };
  try {
    const parsed: unknown = JSON.parse(text);
    if (!isPlainObject(parsed)) return { value: null, error: 'Use um objeto JSON, ex.: {"nome": "Maria"}' };
    return { value: parsed, error: null };
  } catch (err) {
    return { value: null, error: `JSON inválido: ${(err as Error).message}` };
  }
}

/** Objeto → texto. Objeto vazio vira `""` (no Playground, "nada é enviado"). */
export function stringifyObject(value: JsonObject): string {
  return Object.keys(value).length === 0 ? "" : JSON.stringify(value, null, 2);
}

/** Troca (ou remove, com `undefined`) uma chave sem mudar a posição das outras. */
export function setKey(obj: JsonObject, key: string, value: unknown): JsonObject {
  const result: JsonObject = {};
  let found = false;
  for (const [k, v] of Object.entries(obj)) {
    if (k === key) {
      found = true;
      if (value !== undefined) result[k] = value;
    } else {
      result[k] = v;
    }
  }
  if (!found && value !== undefined) result[key] = value;
  return result;
}

/** Chaves do objeto que não são campos declarados — viram "outros campos". */
export function extraKeys(fields: FormField[], obj: JsonObject): string[] {
  const declared = new Set(fields.map((f) => f.name));
  return Object.keys(obj).filter((k) => !declared.has(k));
}

const INTEGER = /^-?\d+$/;
const NUMBER = /^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

/**
 * O que um input de texto vira no objeto. Vazio = chave ausente (`undefined`).
 * Texto que não é número ("-", "abc") fica como texto e aparece como erro. O
 * input numérico guarda o rascunho ("1.", "1.0") enquanto ele valer o mesmo
 * número, senão o ponto sumiria a cada tecla e ninguém digitaria 1.05.
 */
export function coerceText(type: string, raw: string): unknown {
  if (raw === "") return undefined;
  if (type === "integer") {
    const t = raw.trim();
    return INTEGER.test(t) ? Number(t) : raw;
  }
  if (type === "number") {
    const t = raw.trim();
    return NUMBER.test(t) ? Number(t) : raw;
  }
  return raw;
}

/** O texto que um input mostra para um valor folha. */
export function displayText(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

/**
 * Valor solto de "outros campos" (como o `chave=valor` da CLI): JSON quando dá,
 * texto quando não. `formatLoose` é o inverso: um texto que pareceria número ou
 * JSON sai entre aspas, para não mudar de tipo ao ser editado de novo.
 */
export function parseLoose(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function formatLoose(value: unknown): string {
  if (typeof value === "string") {
    try {
      JSON.parse(value);
      return JSON.stringify(value);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value) ?? "";
}

function isEmpty(value: unknown): boolean {
  return value === undefined || value === null || value === "";
}

/** Problema de tipo de um valor presente, ou `null`. */
function typeProblem(type: string, value: unknown): string | null {
  switch (type) {
    case "string":
      return typeof value === "string" ? null : "deve ser texto";
    case "integer":
      return typeof value === "number" && Number.isInteger(value) ? null : "deve ser um número inteiro";
    case "number":
      return typeof value === "number" && Number.isFinite(value) ? null : "deve ser um número";
    case "boolean":
      return typeof value === "boolean" ? null : "deve ser verdadeiro ou falso";
    case "object":
      return isPlainObject(value) ? null : "deve ser um objeto";
    case "array":
      return Array.isArray(value) ? null : "deve ser uma lista";
    default:
      return null;
  }
}

function enumProblem(values: (string | number)[] | null | undefined, value: unknown): string | null {
  if (!values?.length) return null;
  return values.some((v) => v === value) ? null : `fora dos valores permitidos (${values.join(", ")})`;
}

/**
 * Erros por caminho (`cliente.nome`, `parcelas[0].valor`): obrigatório faltando,
 * tipo errado e valor fora do `enum`. Só aponta — quem decide é o backend (422).
 * Campo opcional ausente não é erro: o backend aplica o `default`.
 */
export function validateObject(fields: FormField[], obj: JsonObject, prefix = ""): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of fields) {
    const path = prefix ? `${prefix}.${field.name}` : field.name;
    Object.assign(errors, validateValue(field, obj[field.name], path));
  }
  return errors;
}

function validateValue(field: FormField, value: unknown, path: string): Record<string, string> {
  if (isEmpty(value)) return field.required ? { [path]: "obrigatório" } : {};
  const problem = typeProblem(field.type, value) ?? enumProblem(field.enum, value);
  if (problem) return { [path]: problem };
  if (field.type === "object" && field.fields?.length) return validateObject(field.fields, value as JsonObject, path);
  if (field.type === "array" && field.items) return validateItems(field.items, value as unknown[], path);
  return {};
}

function validateItems(items: SchemaItem, list: unknown[], path: string): Record<string, string> {
  const errors: Record<string, string> = {};
  list.forEach((item, i) => {
    const itemPath = `${path}[${i}]`;
    const problem = typeProblem(items.type, item) ?? enumProblem(items.enum, item);
    if (problem) errors[itemPath] = problem;
    else if (items.type === "object" && items.fields?.length) {
      Object.assign(errors, validateObject(items.fields, item as JsonObject, itemPath));
    }
  });
  return errors;
}

/** Valor inicial de um item novo numa lista. */
export function emptyItem(items: SchemaItem): unknown {
  switch (items.type) {
    case "boolean":
      return false;
    case "integer":
    case "number":
      return items.enum?.length ? items.enum[0] : 0;
    case "object":
      return {};
    default:
      return items.enum?.length ? items.enum[0] : "";
  }
}

/**
 * Parâmetros de uma tool `kind="api"` → os dois formulários do teste:
 * `source="model"` (ou ausente) são os argumentos; `source="dependency"` vão no
 * `dependencies`, pelo nome do campo em `dependency` (não o nome do parâmetro).
 * `const` fica de fora: o valor é fixo.
 */
export function fieldsFromToolParams(params: ApiToolParam[] | undefined): { args: FormField[]; deps: FormField[] } {
  const args: FormField[] = [];
  const deps: FormField[] = [];
  const seenDeps = new Set<string>();
  for (const p of params ?? []) {
    const source = p.source ?? "model";
    if (source === "model") {
      args.push({
        name: p.name,
        type: p.type,
        description: p.description,
        required: p.required,
        fields: p.fields,
        items: p.items,
      });
    } else if (source === "dependency" && p.dependency && p.dependency !== DRY_RUN_DEPENDENCY) {
      if (seenDeps.has(p.dependency)) continue;
      seenDeps.add(p.dependency);
      deps.push({
        name: p.dependency,
        type: p.type,
        description: p.description || `Preenche o parâmetro ${p.name}.`,
        required: p.required,
      });
    }
  }
  return { args, deps };
}
