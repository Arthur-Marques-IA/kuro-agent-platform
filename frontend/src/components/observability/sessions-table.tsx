"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ChevronRight, MessagesSquare } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import { formatCost, formatNumber } from "@/lib/format";
import type { LogSessionPage, LogSessionSummary } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, RelativeTime, Skeleton } from "@/components/ui/primitives";
import { FeedbackCount } from "@/components/observability/run-status";
import { apiSearchParams } from "@/lib/logs-query";

/** Busca `/api/observability/sessions` com os filtros dados — não pagina de verdade, ver `scanned`. */
export function useSessions(params: Record<string, string | string[]>) {
  const [items, setItems] = useState<LogSessionSummary[]>([]);
  const [scanned, setScanned] = useState(0);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const paramsKey = JSON.stringify(params);

  const reload = useCallback(async () => {
    setState("loading");
    setError(null);
    try {
      const page = await requestJson<LogSessionPage>(`/api/observability/sessions?${apiSearchParams(params)}`, {
        fallbackError: "Falha ao carregar sessões",
      });
      setItems(page.items);
      setScanned(page.scanned);
      setState("ready");
    } catch (err) {
      setError(errorMessage(err));
      setState("error");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramsKey]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
  }, [reload]);

  return { items, scanned, state, error, reload };
}

export function SessionsTable({ params }: { params: Record<string, string | string[]> }) {
  const { items, state, error, reload } = useSessions(params);

  if (state === "loading") {
    return (
      <Card className="flex flex-col gap-3 p-4" aria-busy="true" aria-label="Carregando sessões">
        {[1, 2, 3, 4].map((i) => (
          <Skeleton key={i} className="h-10" />
        ))}
      </Card>
    );
  }

  if (state === "error") {
    return (
      <Card>
        <EmptyState
          icon={MessagesSquare}
          title="Não foi possível carregar as sessões"
          description={error}
          action={
            <Button variant="outline" size="sm" onClick={() => void reload()}>
              Tentar de novo
            </Button>
          }
        />
      </Card>
    );
  }

  if (items.length === 0) {
    return (
      <Card>
        <EmptyState icon={MessagesSquare} title="Nenhuma sessão no período" description="Ajuste os filtros ou a janela de datas." />
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden">
      <div className="scrollbar-thin overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="w-[28%] px-4 py-2.5 font-medium">Sessão</th>
              <th className="px-3 py-2.5 font-medium">Usuário</th>
              <th className="px-3 py-2.5 font-medium">Agentes</th>
              <th className="px-3 py-2.5 text-right font-medium">Execuções</th>
              <th className="px-3 py-2.5 text-right font-medium">Tokens</th>
              <th className="px-3 py-2.5 text-right font-medium">Custo</th>
              <th className="px-3 py-2.5 font-medium">Feedback</th>
              <th className="px-3 py-2.5 font-medium">Última atividade</th>
              <th className="w-8" aria-hidden />
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {items.map((session) => (
              <SessionRow key={session.session_id} session={session} />
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function SessionRow({ session }: { session: LogSessionSummary }) {
  const href = `/logs/sessions/${encodeURIComponent(session.session_id)}`;
  return (
    <tr className="group relative transition-colors hover:bg-accent/50">
      <td className="max-w-0 px-4 py-2.5">
        <Link href={href} className="absolute inset-0" aria-label={`Ver sessão: ${session.session_id}`} />
        <p className="truncate font-mono text-xs">{session.session_id}</p>
        {session.error_count > 0 && (
          <p className="truncate text-xs text-destructive">
            {session.error_count} {session.error_count === 1 ? "erro" : "erros"}
          </p>
        )}
      </td>
      <td className="max-w-0 truncate px-3 py-2.5 text-xs text-muted-foreground">{session.user_id ?? "—"}</td>
      <td className="px-3 py-2.5">
        <div className="flex flex-wrap gap-1">
          {session.agent_types.map((agentType) => (
            <Badge key={agentType} variant="outline">
              {agentType}
            </Badge>
          ))}
        </div>
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right tabular-nums">{formatNumber(session.run_count)}</td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right tabular-nums">{session.total_tokens ? formatNumber(session.total_tokens) : "—"}</td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right tabular-nums">{formatCost(session.cost_usd)}</td>
      <td className="px-3 py-2.5 text-xs">
        <FeedbackCount up={session.feedback_up} down={session.feedback_down} />
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-xs text-muted-foreground">
        <RelativeTime date={session.last_activity} />
      </td>
      <td className="pr-3 text-muted-foreground">
        <ChevronRight className="size-4 opacity-0 transition-opacity group-hover:opacity-100" aria-hidden />
      </td>
    </tr>
  );
}
