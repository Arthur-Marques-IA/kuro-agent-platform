/**
 * CSV para abrir em planilha (RFC 4180): vírgula como separador, CRLF entre linhas e BOM no começo
 * para o Excel ler o UTF-8 (sem ele, "ação" vira "aÃ§Ã£o"). Sem dependência de React: testado por
 * `csv.test.ts`.
 *
 * Injeção de fórmula: um texto que começa com `=`, `+`, `-`, `@`, tab ou CR é lido como fórmula pela
 * planilha (`=HYPERLINK(...)` numa mensagem de cliente vira link clicável). Esse texto ganha um `'` na
 * frente, que a planilha mostra como texto puro. Números entram crus: `-3` continua sendo número.
 */

export type CsvValue = string | number | boolean | null | undefined;

export const CSV_BOM = "﻿";

const FORMULA_START = /^[=+\-@\t\r]/;
const NEEDS_QUOTES = /[",;\r\n]/;

export function csvCell(value: CsvValue): string {
  if (value == null) return "";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
  if (typeof value === "boolean") return value ? "true" : "false";
  let text = value;
  if (FORMULA_START.test(text)) text = `'${text}`;
  return NEEDS_QUOTES.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export interface CsvColumn<T> {
  header: string;
  value: (row: T) => CsvValue;
}

/** O arquivo inteiro: BOM, cabeçalho e uma linha por item, tudo com CRLF. */
export function toCsv<T>(columns: CsvColumn<T>[], rows: T[]): string {
  const lines = [columns.map((c) => csvCell(c.header)).join(",")];
  for (const row of rows) lines.push(columns.map((c) => csvCell(c.value(row))).join(","));
  return `${CSV_BOM}${lines.join("\r\n")}\r\n`;
}
