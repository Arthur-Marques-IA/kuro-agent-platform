"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
  // A resposta de uma busca velha (filtro trocado no meio) é descartada.
  const generation = useRef(0);
  const busy = useRef(false);

  const load = useCallback(
    async (silent: boolean) => {
      if (silent && busy.current) return;
      const mine = silent ? generation.current : ++generation.current;
      busy.current = true;
      if (!silent) {
        setState("loading");
        setError(null);
      }
      try {
        const page = await requestJson<LogSessionPage>(`/api/observability/sessions?${apiSearchParams(params)}`, {
          fallbackError: "Falha ao carregar sessões",
        });
        if (mine !== generation.current) return;
        // Sem paginação, a lista nova substitui a velha: a sessão que voltou a falar sobe para o topo.
        setItems(page.items);
        setScanned(page.scanned);
        setState("ready");
        setError(null);
      } catch (err) {
        if (mine !== generation.current || silent) return; // a automática falha em silêncio
        setError(errorMessage(err));
        setState("error");
      } finally {
        if (mine === generation.current) busy.current = false;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [paramsKey]
  );

  const reload = useCallback(() => load(false), [load]);
  /** Atualização silenciosa (a automática): sem esqueleto, a rolagem fica onde estava. */
  const refresh = useCallback(() => load(true), [load]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
  }, [reload]);

  return { items, scanned, state, error, reload, refresh };
}

/** `refreshTick` muda a cada volta da atualização automática. */
export function SessionsTable({ params, refreshTick = 0 }: { params: Record<string, string | string[]>; refreshTick?: number }) {
  const { items, state, error, reload, refresh } = useSessions(params);
  const lastTick = useRef(refreshTick);
  useEffect(() => {
    if (lastTick.current === refreshTick) return;
    lastTick.current = refreshTick;
    void refresh();
  }, [refreshTick, refresh]);

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
    <>
      {/* No celular, cartões: o contato, a última fala, os agentes e a hora, sem rolagem lateral. */}
      <ul className="flex flex-col gap-2 md:hidden" aria-label="Sessões">
        {items.map((session) => (
          <SessionCard key={session.session_id} session={session} />
        ))}
      </ul>
      <Card className="hidden overflow-hidden md:block">
        <div className="scrollbar-thin overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="w-[34%] px-4 py-2.5 font-medium">Contato e última fala</th>
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
    </>
  );
}

function sessionHref(session: LogSessionSummary) {
  return `/logs/sessions/${encodeURIComponent(session.session_id)}`;
}

function SessionCard({ session }: { session: LogSessionSummary }) {
  return (
    <li>
      <Link
        href={sessionHref(session)}
        className="flex min-h-10 flex-col gap-1 rounded-xl border border-border bg-card px-3.5 py-3 outline-none transition-colors hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring"
      >
        <div className="flex min-w-0 items-baseline gap-2">
          {/* Sem o nome do contato, o usuário: legível, ao contrário do session_id. */}
          <p className="min-w-0 flex-1 truncate text-sm font-medium">
            {session.contact_name || session.user_id || <span className="text-muted-foreground">Sem nome de contato</span>}
          </p>
          <span className="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
            <RelativeTime date={session.last_activity} />
          </span>
        </div>
        <p className="line-clamp-2 break-words text-[13px]">
          {session.last_message || <span className="text-muted-foreground">(sem mensagem)</span>}
        </p>
        <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
          {session.agent_types.map((agentType) => (
            <Badge key={agentType} variant="outline" className="max-w-full truncate">
              {agentType}
            </Badge>
          ))}
          <span>
            {formatNumber(session.run_count)} {session.run_count === 1 ? "execução" : "execuções"}
          </span>
          {session.channel && <span>· {session.channel}</span>}
          {session.error_count > 0 && (
            <span className="text-destructive">
              · {session.error_count} {session.error_count === 1 ? "erro" : "erros"}
            </span>
          )}
        </div>
      </Link>
    </li>
  );
}

function SessionRow({ session }: { session: LogSessionSummary }) {
  const href = sessionHref(session);
  return (
    <tr className="group relative transition-colors hover:bg-accent/50">
      <td className="max-w-0 px-4 py-2.5">
        <Link href={href} className="absolute inset-0" aria-label={`Ver sessão: ${session.contact_name ?? session.session_id}`} />
        {session.contact_name && <p className="truncate font-medium">{session.contact_name}</p>}
        <p className="truncate text-[13px]" title={session.last_message ?? undefined}>
          {session.last_message || <span className="text-muted-foreground">(sem mensagem)</span>}
        </p>
        <p className="truncate font-mono text-[11px] text-muted-foreground">
          {session.session_id}
          {session.channel && <span className="font-sans"> · {session.channel}</span>}
        </p>
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
