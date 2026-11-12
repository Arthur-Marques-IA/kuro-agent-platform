"use client";

import { Fragment } from "react";
import { Braces, ListTree } from "lucide-react";
import { cn } from "@/lib/cn";
import { displayValue, type OutputNode } from "@/lib/structured-output";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/**
 * A saída de um analista campo a campo (rótulo → valor), com objetos e listas
 * indentados. Usada na tela de Análise e na tela do run. Folha com `enum` vira
 * selo; valor fora dos permitidos fica em destaque de alerta.
 */
export function OutputFields({ nodes, depth = 0 }: { nodes: OutputNode[]; depth?: number }) {
  if (nodes.length === 0) {
    return depth === 0 ? <p className="px-4 py-3 text-xs text-muted-foreground">(vazio)</p> : null;
  }
  return (
    <dl className={cn("flex flex-col text-[13px]", depth === 0 && "scrollbar-thin max-h-[32rem] overflow-auto py-1")}>
      {nodes.map((node) => (
        <Fragment key={node.path}>
          {/* A indentação fica no rótulo: os valores de todos os níveis seguem alinhados numa coluna só. */}
          <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-3 border-b border-border/60 px-4 py-1.5 last:border-b-0">
            <dt className="truncate text-muted-foreground" title={node.path} style={{ paddingLeft: depth * 16 }}>
              {node.label}
              {node.label !== node.key && <span className="ml-1 font-mono text-[11px] opacity-70">{node.key}</span>}
            </dt>
            <dd className="min-w-0 whitespace-pre-wrap break-words">
              <OutputValue node={node} />
            </dd>
          </div>
          {node.children && node.children.length > 0 && <OutputFields nodes={node.children} depth={depth + 1} />}
        </Fragment>
      ))}
    </dl>
  );
}

function OutputValue({ node }: { node: OutputNode }) {
  if (node.kind !== "value") {
    const count = node.children?.length ?? 0;
    return (
      <span className="text-xs text-muted-foreground">
        {node.kind === "array" ? (count === 1 ? "1 item" : `${count} itens`) : "objeto"}
      </span>
    );
  }
  if (node.value == null) return <span className="text-muted-foreground">{displayValue(node.value)}</span>;
  if (node.enum) {
    const allowed = node.enum.map(String).join(", ");
    return node.enumMatch ? (
      <Badge className="font-mono" title={`Valores permitidos: ${allowed}`}>
        {displayValue(node.value)}
      </Badge>
    ) : (
      <Badge variant="warning" className="font-mono" title={`Fora dos valores permitidos pelo response_schema atual: ${allowed}`}>
        {displayValue(node.value)} · fora do enum
      </Badge>
    );
  }
  return <span>{displayValue(node.value)}</span>;
}

/** Alterna entre os campos e o JSON cru da mesma saída. */
export function RawJsonToggle({ raw, onChange }: { raw: boolean; onChange: (raw: boolean) => void }) {
  return (
    <Button
      variant="ghost"
      size="sm"
      className="h-7 px-2 text-xs"
      aria-pressed={raw}
      onClick={() => onChange(!raw)}
      title={raw ? "Ver campo a campo" : "Ver o JSON cru"}
    >
      {raw ? <ListTree /> : <Braces />} {raw ? "Campos" : "JSON"}
    </Button>
  );
}
