"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { FileText, Pencil, TriangleAlert } from "lucide-react";
import { requestJson } from "@/lib/http";
import { pluralize } from "@/lib/format";
import type { FlaggedDocument } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { RelativeTime, SectionHeading } from "@/components/ui/primitives";
import type { DocumentTarget } from "@/components/knowledge/document-fix-dialog";

const RUN_LINKS = 3;

/** Documentos com marcações abertas de errado/desatualizado no trace. O Kuro não esconde
 *  nem despublica nada: "Corrigir" grava uma revisão nova do documento e resolve as
 *  marcações nela; página de site se corrige na fonte. */
export function FlaggedDocumentsPanel({
  collection,
  reloadKey,
  onFix,
}: {
  collection: string;
  reloadKey?: number;
  onFix: (target: DocumentTarget) => void;
}) {
  const [state, setState] = useState<{ collection: string; rows: FlaggedDocument[] } | null>(null);

  useEffect(() => {
    let cancelled = false;
    requestJson<FlaggedDocument[]>(
      `/api/observability/chunk-feedback/flagged?${new URLSearchParams({ collection, label: "wrong", limit: "10" })}`,
      { fallbackError: "Falha ao carregar os documentos marcados" }
    )
      .then((rows) => {
        if (!cancelled) setState({ collection, rows });
      })
      .catch(() => {
        if (!cancelled) setState({ collection, rows: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [collection, reloadKey]);

  const rows = state?.collection === collection ? state.rows : [];
  if (rows.length === 0) return null;

  return (
    <section className="flex min-w-0 flex-col gap-3">
      <SectionHeading
        title={
          <span className="flex items-center gap-2">
            <TriangleAlert className="size-4 text-warning" aria-hidden />
            Marcados como errados ou desatualizados
          </span>
        }
        description="Marcações abertas no trace. Nada foi escondido dos agentes: corrija o documento e as marcações ficam resolvidas na revisão nova."
      />
      <Card className="divide-y divide-border overflow-hidden">
        {rows.map((doc) => (
          <div key={`${doc.content_id ?? doc.document}`} className="flex flex-col gap-1.5 px-4 py-3 text-[13px]">
            <div className="flex min-w-0 items-center gap-2">
              <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden />
              <span className="truncate font-medium" title={doc.content_id ?? undefined}>
                {doc.document || doc.content_id}
              </span>
              {doc.stale && (
                <Badge variant="secondary" title="As marcações são de uma revisão anterior à atual: talvez já esteja corrigido.">
                  revisão antiga
                </Badge>
              )}
              <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                {pluralize(doc.count, "marcação", "marcações")} · <RelativeTime date={doc.last_at} />
                {doc.current_revision != null && <> · rev. {doc.current_revision}</>}
              </span>
              {doc.content_id && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onFix({ contentId: doc.content_id as string, name: doc.document })}
                >
                  <Pencil />
                  Corrigir
                </Button>
              )}
            </div>
            {doc.comments[0] && <p className="truncate text-xs text-muted-foreground">“{doc.comments[0]}”</p>}
            <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
              {doc.run_ids.slice(0, RUN_LINKS).map((runId) => (
                <Link key={runId} href={`/runs/${encodeURIComponent(runId)}`} className="font-mono text-primary hover:underline">
                  {runId.slice(0, 12)}
                </Link>
              ))}
              {doc.run_ids.length > RUN_LINKS && (
                <span className="text-muted-foreground">+{doc.run_ids.length - RUN_LINKS} runs</span>
              )}
            </div>
          </div>
        ))}
      </Card>
    </section>
  );
}
