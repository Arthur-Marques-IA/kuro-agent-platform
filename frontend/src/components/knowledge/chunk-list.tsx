"use client";

import { type ReactNode, useState } from "react";
import { ExternalLink, FileText } from "lucide-react";
import { cn } from "@/lib/cn";
import type { RetrievedChunk, ScoreKind } from "@/lib/types";
import { CopyButton } from "@/components/ui/copy-button";

/** Trechos recuperados da base, em ordem de relevância — o mesmo desenho na busca
 *  da collection e no trace de um run (o que o agente de fato leu). */
export function ChunkList({
  chunks,
  label = "Trechos recuperados",
  scoreKind,
  actions,
}: {
  chunks: RetrievedChunk[];
  label?: string;
  /** O que o score mede, quando a busca inteira diz (no trace); senão, o de cada trecho. */
  scoreKind?: ScoreKind | null;
  /** Ações por trecho (no trace: as marcações do operador). */
  actions?: (chunk: RetrievedChunk) => ReactNode;
}) {
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());

  function toggle(index: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  return (
    <ol className="flex flex-col gap-2" aria-label={label}>
      {chunks.map((chunk, index) => {
        const isOpen = expanded.has(index);
        const title = chunk.page_title || chunk.document;
        const position = [
          chunk.chunk != null ? `trecho ${chunk.chunk}` : null,
          chunk.page != null ? `pág. ${chunk.page}` : null,
        ].filter(Boolean);
        return (
          <li key={chunk.chunk_id ?? index} className="rounded-lg border border-border bg-surface p-3">
            <div className="mb-1.5 flex min-w-0 items-center gap-2 text-[11px] text-muted-foreground">
              <span className="font-medium tabular-nums">#{chunk.rank ?? index + 1}</span>
              {title && (
                <span className="flex min-w-0 items-center gap-1 text-foreground" title={chunk.content_id ?? undefined}>
                  <FileText className="size-3 shrink-0" aria-hidden />
                  <span className="truncate font-medium">{title}</span>
                </span>
              )}
              {position.length > 0 && <span className="shrink-0">{position.join(" · ")}</span>}
              <span className="ml-auto flex shrink-0 items-center gap-2">
                {chunk.score != null && <ScoreMeter score={chunk.score} kind={chunk.score_kind ?? scoreKind} />}
                <CopyButton value={chunk.content} label="Copiar trecho" className="size-6" />
              </span>
            </div>
            {actions && <div className="-mt-1 mb-1 flex justify-end">{actions(chunk)}</div>}
            {chunk.section && (
              <p className="mb-1 truncate text-[11px] text-muted-foreground" title={chunk.section}>
                {chunk.section}
              </p>
            )}
            {chunk.source_url && (
              <a
                href={chunk.source_url}
                target="_blank"
                rel="noreferrer noopener"
                className="mb-1.5 flex min-w-0 items-center gap-1 text-[11px] text-primary hover:underline"
              >
                <ExternalLink className="size-3 shrink-0" aria-hidden />
                <span className="truncate">{chunk.source_url}</span>
              </a>
            )}
            <p className={cn("whitespace-pre-wrap break-words text-[13px] leading-relaxed", !isOpen && "line-clamp-5")}>
              {chunk.content}
            </p>
            {chunk.content.length > 280 && (
              <button
                type="button"
                onClick={() => toggle(index)}
                className="mt-1 text-[11px] text-muted-foreground hover:text-foreground"
              >
                {isOpen ? "Mostrar menos" : "Mostrar tudo"}
              </button>
            )}
          </li>
        );
      })}
    </ol>
  );
}

export const SCORE_KIND_LABEL: Record<ScoreKind, { short: string; long: string }> = {
  similarity: { short: "similaridade", long: "Similaridade do vetor (1 − distância cosseno)" },
  hybrid: {
    short: "híbrido",
    long: "Score híbrido: peso × similaridade do vetor + (1 − peso) × ranking do full-text, normalizados em 0–1",
  },
  rerank: { short: "reranker", long: "Score do reranker" },
};

function ScoreMeter({ score, kind }: { score: number; kind?: ScoreKind | null }) {
  const pct = Math.round(Math.min(Math.max(score, 0), 1) * 100);
  const label = kind ? SCORE_KIND_LABEL[kind] : null;
  return (
    <span
      className="flex items-center gap-1.5"
      title={`${label?.long ?? "Relevância"}: ${score.toFixed(3)}`}
    >
      {label && <span>{label.short}</span>}
      <span className="relative block h-1.5 w-10 rounded-full bg-muted" aria-hidden>
        <span className="absolute inset-y-0 left-0 rounded-full bg-primary" style={{ width: `${pct}%` }} />
      </span>
      <span className="tabular-nums">{score.toFixed(2)}</span>
    </span>
  );
}
