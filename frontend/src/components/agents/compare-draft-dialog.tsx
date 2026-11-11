"use client";

import { ArrowUpRight } from "lucide-react";
import { draftChanges } from "@/lib/agent-list";
import { cn } from "@/lib/cn";
import { diffLines } from "@/lib/diff";
import type { AgentDefinition } from "@/lib/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

/**
 * Produção × draft lado a lado: o diff do prompt e, nos outros campos que o promote copia,
 * o valor de cada um. Daqui o botão leva direto ao promote.
 */
export function CompareDraftDialog({
  prod,
  draft,
  open,
  onOpenChange,
  onPromote,
}: {
  prod: AgentDefinition;
  draft: AgentDefinition;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPromote: () => void;
}) {
  const changes = draftChanges(prod, draft);
  const promptChanged = changes.some((c) => c.field === "instructions");
  const others = changes.filter((c) => c.field !== "instructions");
  const diff = promptChanged ? diffLines(prod.instructions, draft.instructions) : [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="lg">
      <DialogHeader>
        <DialogTitle>Comparar draft com produção</DialogTitle>
        <DialogDescription>
          <b className="font-mono">{prod.agent_type}</b> → <b className="font-mono">{draft.agent_type}</b>. Rode o{" "}
          <code>kuro eval</code> no draft antes de promover.
        </DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-4">
        {changes.length === 0 && (
          <p className="text-[13px] text-muted-foreground">
            Prompt, modelo, tools, memória, base, dependências, schema e etapas são iguais nos dois.
          </p>
        )}
        {promptChanged && (
          <section className="flex flex-col gap-1.5">
            <h3 className="text-[13px] font-medium">Prompt</h3>
            <div className="scrollbar-thin max-h-72 overflow-auto rounded-lg border border-border py-1.5 font-mono text-[12px] leading-5">
              {diff.map((line, i) => (
                <div
                  key={i}
                  className={cn(
                    "grid grid-cols-[1.75rem_minmax(0,1fr)] px-1",
                    line.type === "added" && "bg-success/10",
                    line.type === "removed" && "bg-destructive/10"
                  )}
                >
                  <span
                    aria-label={line.type === "added" ? "no draft" : line.type === "removed" ? "só na produção" : undefined}
                    className={cn(
                      "select-none text-center",
                      line.type === "added" && "text-success",
                      line.type === "removed" && "text-destructive",
                      line.type === "same" && "text-muted-foreground/50"
                    )}
                  >
                    {line.type === "added" ? "+" : line.type === "removed" ? "−" : " "}
                  </span>
                  <span className="whitespace-pre-wrap break-words pr-2">{line.text}</span>
                </div>
              ))}
            </div>
          </section>
        )}
        {others.length > 0 && (
          <section className="flex flex-col gap-1.5">
            <h3 className="text-[13px] font-medium">Configuração</h3>
            <dl className="divide-y divide-border rounded-lg border border-border text-[12px]">
              <div
                aria-hidden
                className="hidden gap-3 px-3 py-1.5 text-[11px] uppercase tracking-wide text-muted-foreground sm:grid sm:grid-cols-[10rem_minmax(0,1fr)_minmax(0,1fr)]"
              >
                <span>Campo</span>
                <span>Produção</span>
                <span>Draft</span>
              </div>
              {others.map((change) => (
                <div key={change.field} className="grid gap-1 px-3 py-2 sm:grid-cols-[10rem_minmax(0,1fr)_minmax(0,1fr)] sm:gap-3">
                  <dt className="font-mono text-muted-foreground">{change.field}</dt>
                  <dd className="min-w-0 break-words">
                    <span className="text-muted-foreground sm:hidden">produção: </span>
                    <span className="font-mono text-destructive">{show(change.prod)}</span>
                  </dd>
                  <dd className="min-w-0 break-words">
                    <span className="text-muted-foreground sm:hidden">draft: </span>
                    <span className="font-mono text-success">{show(change.draft)}</span>
                  </dd>
                </div>
              ))}
            </dl>
          </section>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)}>
          Fechar
        </Button>
        <Button
          onClick={() => {
            onOpenChange(false);
            onPromote();
          }}
        >
          <ArrowUpRight /> Promover para {prod.agent_type}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
