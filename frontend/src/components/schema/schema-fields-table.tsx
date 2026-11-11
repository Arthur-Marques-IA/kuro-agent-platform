import { schemaRows } from "@/lib/schema-rows";
import type { SchemaField } from "@/lib/types";
import { Badge } from "@/components/ui/badge";

/**
 * O `response_schema` em modo leitura. O `SchemaFieldsEditor` é feito de inputs
 * (serve para editar, não para ler de relance), então a leitura é uma tabela à
 * parte — com as mesmas regras de forma, vindas de `schemaRows`.
 */
export function SchemaFieldsTable({ fields }: { fields: SchemaField[] }) {
  const rows = schemaRows(fields);
  if (rows.length === 0) {
    return <p className="text-[13px] text-muted-foreground">Este agente não declara campos de saída.</p>;
  }
  return (
    <div className="scrollbar-thin overflow-x-auto rounded-xl border border-border">
      <table className="w-full min-w-[36rem] text-left text-[13px]">
        <thead className="bg-muted/40 text-xs text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">Campo</th>
            <th scope="col" className="px-3 py-2 font-medium">Tipo</th>
            <th scope="col" className="px-3 py-2 font-medium">Obrigatório</th>
            <th scope="col" className="px-3 py-2 font-medium">Descrição</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.path} className="border-t border-border/60 align-top">
              <td className="px-3 py-2" style={{ paddingLeft: 12 + row.depth * 18 }}>
                <span
                  className={row.depth > 0 ? "border-l-2 border-border pl-2" : undefined}
                  title={row.path}
                >
                  <span className="font-mono text-[12.5px]">{row.name}</span>
                  {row.label && row.label !== row.name && (
                    <span className="ml-1.5 text-xs text-muted-foreground">{row.label}</span>
                  )}
                </span>
                {row.enum && (
                  <div className={`mt-1 flex flex-wrap gap-1 ${row.depth > 0 ? "pl-2.5" : ""}`} aria-label="Valores permitidos">
                    {row.enum.map((v) => (
                      <Badge key={String(v)} variant="outline" className="font-mono">
                        {String(v)}
                      </Badge>
                    ))}
                  </div>
                )}
              </td>
              <td className="whitespace-nowrap px-3 py-2 font-mono text-[12px] text-muted-foreground">
                {row.type}
                {row.enum && <span className="ml-1 font-sans">· enum</span>}
              </td>
              <td className="px-3 py-2">
                {row.required ? <Badge variant="secondary">sim</Badge> : <span className="text-muted-foreground">não</span>}
              </td>
              <td className="px-3 py-2 text-muted-foreground">{row.description ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
