"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, MessageSquare, TriangleAlert, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { errorMessage, requestJson } from "@/lib/http";
import type { ChunkFeedback, ChunkLabel } from "@/lib/types";
import { useToast } from "@/components/ui/toast";

export const CHUNK_LABELS: Record<ChunkLabel, { label: string; icon: typeof Check; active: string }> = {
  relevant: { label: "Relevante — era o trecho certo", icon: Check, active: "bg-success/15 text-success" },
  irrelevant: { label: "Irrelevante — não respondia à pergunta", icon: X, active: "bg-muted text-foreground" },
  wrong: {
    label: "Errado/desatualizado — o conteúdo do documento está incorreto",
    icon: TriangleAlert,
    active: "bg-warning/15 text-warning",
  },
};

const keyOf = (spanId: string, chunkId: string) => `${spanId}|${chunkId}`;

/** Marcações do operador nos trechos de um run. Só registra: nada é escondido ou despublicado. */
export function useChunkFeedback(runId: string, operator?: string | null) {
  const toast = useToast();
  const [marks, setMarks] = useState<Map<string, ChunkFeedback>>(() => new Map());

  useEffect(() => {
    let cancelled = false;
    requestJson<ChunkFeedback[]>(`/api/observability/chunk-feedback?run_id=${encodeURIComponent(runId)}`, {
      fallbackError: "Falha ao carregar as marcações",
    })
      .then((rows) => {
        if (!cancelled) setMarks(new Map(rows.map((r) => [keyOf(r.span_id, r.chunk_id), r])));
      })
      .catch(() => {
        // Sem as marcações os trechos continuam legíveis; os botões só começam vazios.
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const mark = useCallback(
    async (spanId: string, chunkId: string, label: ChunkLabel, comment?: string) => {
      try {
        const row = await requestJson<ChunkFeedback>("/api/observability/chunk-feedback", {
          method: "POST",
          json: { run_id: runId, span_id: spanId, chunk_id: chunkId, label, comment: comment || null, created_by: operator || "console" },
          fallbackError: "Falha ao marcar o trecho",
        });
        setMarks((prev) => new Map(prev).set(keyOf(spanId, chunkId), row));
      } catch (err) {
        toast({ title: "Não foi possível marcar o trecho", description: errorMessage(err), variant: "error" });
      }
    },
    [runId, operator, toast]
  );

  const clear = useCallback(
    async (row: ChunkFeedback) => {
      try {
        await requestJson(`/api/observability/chunk-feedback/${encodeURIComponent(row.id)}`, {
          method: "DELETE",
          fallbackError: "Falha ao desmarcar o trecho",
        });
        setMarks((prev) => {
          const next = new Map(prev);
          next.delete(keyOf(row.span_id, row.chunk_id));
          return next;
        });
      } catch (err) {
        toast({ title: "Não foi possível desmarcar", description: errorMessage(err), variant: "error" });
      }
    },
    [toast]
  );

  return { marks, mark, clear, keyOf };
}

/** Os três botões discretos no cartão do trecho, mais o comentário opcional. */
export function ChunkFeedbackButtons({
  current,
  onMark,
  onClear,
}: {
  current?: ChunkFeedback;
  onMark: (label: ChunkLabel, comment?: string) => Promise<void>;
  onClear: (row: ChunkFeedback) => Promise<void>;
}) {
  const [commenting, setCommenting] = useState(false);
  const [comment, setComment] = useState(current?.comment ?? "");
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    try {
      await action();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-0.5" role="group" aria-label="Avaliar trecho">
        {(Object.keys(CHUNK_LABELS) as ChunkLabel[]).map((label) => {
          const { label: title, icon: Icon, active } = CHUNK_LABELS[label];
          const isActive = current?.label === label;
          return (
            <button
              key={label}
              type="button"
              disabled={busy}
              title={isActive ? `${title} (clique para desmarcar)` : title}
              aria-label={title}
              aria-pressed={isActive}
              onClick={() => run(() => (isActive && current ? onClear(current) : onMark(label, current?.comment ?? undefined)))}
              className={cn(
                "flex size-6 items-center justify-center rounded-md text-muted-foreground/70 transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50",
                isActive && active
              )}
            >
              <Icon className="size-3.5" aria-hidden />
            </button>
          );
        })}
        {current && (
          <button
            type="button"
            title={current.comment ? `Comentário: ${current.comment}` : "Comentar a marcação"}
            aria-label="Comentar a marcação"
            onClick={() => setCommenting((v) => !v)}
            className={cn(
              "flex size-6 items-center justify-center rounded-md text-muted-foreground/70 hover:bg-accent hover:text-foreground",
              current.comment && "text-foreground"
            )}
          >
            <MessageSquare className="size-3.5" aria-hidden />
          </button>
        )}
      </div>
      {current && commenting && (
        <form
          className="flex w-64 max-w-full items-center gap-1"
          onSubmit={(event) => {
            event.preventDefault();
            run(async () => {
              await onMark(current.label, comment.trim());
              setCommenting(false);
            });
          }}
        >
          <input
            autoFocus
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            maxLength={2000}
            placeholder="Comentário (opcional)"
            className="h-7 min-w-0 flex-1 rounded-md border border-border bg-background px-2 text-[12px] outline-none focus:border-primary"
          />
          <button type="submit" disabled={busy} className="h-7 rounded-md px-2 text-[11px] font-medium text-primary hover:bg-accent">
            Salvar
          </button>
        </form>
      )}
    </div>
  );
}
