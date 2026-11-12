"use client";

import type { AgentFormPayload, FormSectionInfo } from "@/lib/agent-form";
import { formatNumber } from "@/lib/format";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/primitives";
import { PromptDiff, diffStats } from "@/components/agents/prompt-diff";

/**
 * Revisão antes de salvar quando o prompt mudou: o diff contra as instruções salvas (exatamente
 * como estão, para que uma instrução de várias linhas que vire várias instruções fique visível)
 * e as outras seções que também vão no `PUT`.
 */
export function SaveReviewDialog({
  open,
  onOpenChange,
  saved,
  savedVersion,
  changes,
  otherSections,
  submitting,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  saved: string[];
  savedVersion: number;
  changes: AgentFormPayload;
  /** As outras seções alteradas, além do prompt. */
  otherSections: FormSectionInfo[];
  submitting: boolean;
  onConfirm: () => void;
}) {
  const next = changes.instructions ?? saved;
  const stats = diffStats(saved, next);
  return (
    <Dialog open={open} onOpenChange={(value) => !submitting && onOpenChange(value)} size="xl">
      <DialogHeader>
        <DialogTitle>Salvar o prompt como v{savedVersion + 1}?</DialogTitle>
        <DialogDescription>
          {formatNumber(saved.length)} → {formatNumber(next.length)} instruções ·{" "}
          <span className="text-success">+{stats.added}</span> <span className="text-destructive">−{stats.removed}</span>{" "}
          linhas em relação à v{savedVersion}. A versão anterior continua no histórico.
        </DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-3">
        <PromptDiff
          from={saved}
          to={next}
          addedLabel="nova"
          removedLabel={`só na v${savedVersion}`}
          className="max-h-[55dvh]"
        />
        {otherSections.length > 0 && (
          <p className="text-[13px] text-muted-foreground">
            Também vão nesta gravação: {otherSections.map((s) => s.label).join(", ")}.
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
          Voltar a editar
        </Button>
        <Button onClick={onConfirm} disabled={submitting} data-autofocus>
          {submitting && <Spinner />}
          Salvar como v{savedVersion + 1}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
