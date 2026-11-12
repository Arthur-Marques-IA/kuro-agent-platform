import { cn } from "@/lib/cn";
import { diffLines } from "@/lib/diff";

/** Contagem de linhas incluídas e removidas entre duas listas de instruções. */
export function diffStats(from: string[], to: string[]) {
  let added = 0;
  let removed = 0;
  for (const line of diffLines(from, to)) {
    if (line.type === "added") added++;
    else if (line.type === "removed") removed++;
  }
  return { added, removed };
}

/**
 * Diff linha a linha de duas listas de instruções (o prompt), com + e − na margem.
 * Usado na comparação draft × produção e na revisão antes de salvar o prompt.
 */
export function PromptDiff({
  from,
  to,
  addedLabel = "incluída",
  removedLabel = "removida",
  className,
}: {
  from: string[];
  to: string[];
  /** Rótulo acessível das linhas novas e das que saíram. */
  addedLabel?: string;
  removedLabel?: string;
  className?: string;
}) {
  const diff = diffLines(from, to);
  return (
    <div
      className={cn(
        "scrollbar-thin overflow-auto rounded-lg border border-border py-1.5 font-mono text-[12px] leading-5",
        className
      )}
    >
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
            aria-label={line.type === "added" ? addedLabel : line.type === "removed" ? removedLabel : undefined}
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
  );
}
