"use client";

import { useMemo, useState } from "react";
import { Clock, Undo2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { diffLines } from "@/lib/diff";
import { formatDateTime } from "@/lib/format";
import type { PromptVersion } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, RelativeTime } from "@/components/ui/primitives";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

export function VersionHistory({
  versions,
  currentVersion,
  currentInstructions,
  onRestore,
}: {
  versions: PromptVersion[];
  currentVersion: number;
  currentInstructions: string[];
  onRestore: (version: PromptVersion) => void;
}) {
  const sorted = useMemo(() => [...versions].sort((a, b) => b.version - a.version), [versions]);
  const [selectedNumber, setSelectedNumber] = useState<number | null>(null);
  const [view, setView] = useState<"content" | "diff">("content");

  if (sorted.length === 0) {
    return (
      <Card>
        <EmptyState
          icon={Clock}
          title="Sem histórico de versões"
          description="Cada vez que as instruções forem salvas, uma versão nova aparece aqui."
        />
      </Card>
    );
  }

  const selected = sorted.find((v) => v.version === selectedNumber) ?? sorted[0];
  const isCurrent = selected.version === currentVersion;
  const effectiveView = isCurrent ? "content" : view;
  const diff = effectiveView === "diff" ? diffLines(selected.instructions, currentInstructions) : null;
  const added = diff?.filter((line) => line.type === "added").length ?? 0;
  const removed = diff?.filter((line) => line.type === "removed").length ?? 0;

  return (
    <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
      <Card className="self-start p-1.5">
        <ol className="flex flex-col gap-0.5" aria-label="Versões do prompt">
          {sorted.map((version) => {
            const active = version.version === selected.version;
            return (
              <li key={version.version}>
                <button
                  type="button"
                  onClick={() => setSelectedNumber(version.version)}
                  aria-current={active}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left transition-colors",
                    active ? "bg-accent" : "hover:bg-accent/50"
                  )}
                >
                  <span
                    className={cn(
                      "flex size-8 shrink-0 items-center justify-center rounded-lg border font-mono text-xs",
                      version.version === currentVersion
                        ? "border-primary/40 bg-primary-soft text-primary"
                        : "border-border bg-surface text-muted-foreground"
                    )}
                  >
                    v{version.version}
                  </span>
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="flex items-center gap-2 text-[13px] font-medium">
                      Versão {version.version}
                      {version.version === currentVersion && <Badge>atual</Badge>}
                    </span>
                    <RelativeTime date={version.created_at} className="text-xs text-muted-foreground" />
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      </Card>

      <Card className="min-w-0 overflow-hidden">
        <div className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold">Versão {selected.version}</p>
            <p className="text-xs text-muted-foreground">
              {formatDateTime(selected.created_at)} · {selected.instructions.length}{" "}
              {selected.instructions.length === 1 ? "instrução" : "instruções"}
              {diff && (
                <>
                  {" · "}
                  <span className="text-success">+{added}</span> <span className="text-destructive">−{removed}</span> em
                  relação à atual
                </>
              )}
            </p>
          </div>
          {!isCurrent && (
            <Tabs value={effectiveView} onValueChange={(v) => setView(v as "content" | "diff")} variant="pill">
              <TabsList>
                <TabsTrigger value="content">Conteúdo</TabsTrigger>
                <TabsTrigger value="diff">Comparar com atual</TabsTrigger>
              </TabsList>
            </Tabs>
          )}
          <Button size="sm" variant="outline" disabled={isCurrent} onClick={() => onRestore(selected)}>
            <Undo2 /> Restaurar no editor
          </Button>
        </div>

        <div className="scrollbar-thin overflow-x-auto py-2 font-mono text-[12.5px] leading-6">
          {diff
            ? diff.map((line, i) => (
                <div
                  key={i}
                  className={cn(
                    "grid grid-cols-[2.25rem_minmax(0,1fr)] px-2",
                    line.type === "added" && "bg-success/10",
                    line.type === "removed" && "bg-destructive/10"
                  )}
                >
                  <span
                    className={cn(
                      "select-none text-center",
                      line.type === "added" && "text-success",
                      line.type === "removed" && "text-destructive",
                      line.type === "same" && "text-muted-foreground/50"
                    )}
                  >
                    {line.type === "added" ? "+" : line.type === "removed" ? "−" : " "}
                  </span>
                  <span className="whitespace-pre-wrap break-words pr-3">{line.text}</span>
                </div>
              ))
            : selected.instructions.map((line, i) => (
                <div key={i} className="grid grid-cols-[2.25rem_minmax(0,1fr)] px-2">
                  <span className="select-none text-center text-muted-foreground">{i + 1}</span>
                  <span className="whitespace-pre-wrap break-words pr-3">{line}</span>
                </div>
              ))}
        </div>
      </Card>
    </div>
  );
}
