"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Activity, ChevronRight } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import { formatCost, formatMs, formatNumber } from "@/lib/format";
import type { RunPage, RunSummary } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, RelativeTime, Skeleton, Spinner } from "@/components/ui/primitives";
import { FeedbackCount, RunStatusBadge } from "@/components/observability/run-status";

const PAGE_SIZE = 25;

/**
 * Busca e pagina `/api/observability/runs` com os filtros dados — reaproveitado
 * pela aba Execuções de um agente e pela página `/observability` (todos os agentes).
 */
export function useRunsPager(params: Record<string, string | string[]>) {
  const [items, setItems] = useState<RunSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const paramsKey = JSON.stringify(params);

  const fetchPage = useCallback(
    async (pageCursor: string | null) => {
      const query = new URLSearchParams({ limit: String(PAGE_SIZE) });
      // Lista vira parâmetro repetido (`complexity=2&complexity=3`), como a API espera.
      for (const [key, value] of Object.entries(params)) {
        for (const item of Array.isArray(value) ? value : [value]) query.append(key, item);
      }
      if (pageCursor) query.set("cursor", pageCursor);
      return requestJson<RunPage>(`/api/observability/runs?${query}`, { fallbackError: "Falha ao carregar execuções" });
    },
    // `params` é um objeto novo a cada render do chamador; `paramsKey` (seu JSON)
    // é a identidade estável que de fato importa aqui.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [paramsKey]
  );

  const reload = useCallback(async () => {
    setState("loading");
    setError(null);
    try {
      const page = await fetchPage(null);
      setItems(page.items);
      setCursor(page.next_cursor ?? null);
      setState("ready");
    } catch (err) {
      setError(errorMessage(err));
      setState("error");
    }
  }, [fetchPage]);

  useEffect(() => {
    // Carga inicial e recarga ao mudar os filtros.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
  }, [reload]);

  async function loadMore() {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const page = await fetchPage(cursor);
      setItems((prev) => [...prev, ...page.items]);
      setCursor(page.next_cursor ?? null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }

  return { items, cursor, state, loadingMore, error, reload, loadMore };
}

export function RunsTable({
  items,
  state,
  error,
  cursor,
  loadingMore,
  onLoadMore,
  onRetry,
  emptyTitle,
  showAgentColumn = false,
}: {
  items: RunSummary[];
  state: "loading" | "ready" | "error";
  error: string | null;
  cursor: string | null;
  loadingMore: boolean;
  onLoadMore: () => void;
  onRetry: () => void;
  emptyTitle: string;
  /** Mostra a coluna do agente — usado na página `/observability`, que lista vários agentes. */
  showAgentColumn?: boolean;
}) {
  if (state === "loading") {
    return (
      <Card className="flex flex-col gap-3 p-4" aria-busy="true" aria-label="Carregando execuções">
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
          icon={Activity}
          title="Não foi possível carregar as execuções"
          description={error}
          action={
            <Button variant="outline" size="sm" onClick={onRetry}>
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
        <EmptyState
          icon={Activity}
          title={emptyTitle}
          description="Execuções recém-terminadas levam alguns segundos para aparecer."
          action={
            // Uma página pode vir vazia (traces de fora do serviço) e ainda haver mais.
            cursor && (
              <Button variant="outline" size="sm" onClick={onLoadMore} disabled={loadingMore}>
                {loadingMore && <Spinner />}
                Carregar mais
              </Button>
            )
          }
        />
      </Card>
    );
  }

  return (
    <>
      <Card className="overflow-hidden">
        <div className="scrollbar-thin overflow-x-auto">
          <table className="w-full min-w-[1040px] text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="w-[30%] min-w-64 px-4 py-2.5 font-medium">Mensagem</th>
                {showAgentColumn && <th className="px-3 py-2.5 font-medium">Agente</th>}
                <th className="px-3 py-2.5 font-medium">Status</th>
                <th className="px-3 py-2.5 font-medium" title="Complexidade pelas tools de negócio distintas (1 a 3)">Tools</th>
                <th className="px-3 py-2.5 font-medium">Versão</th>
                <th className="px-3 py-2.5 text-right font-medium">Latência</th>
                <th className="px-3 py-2.5 text-right font-medium">Tokens</th>
                <th className="px-3 py-2.5 text-right font-medium">Custo</th>
                <th className="px-3 py-2.5 font-medium">Feedback</th>
                <th className="px-3 py-2.5 font-medium">Quando</th>
                <th className="w-8" aria-hidden />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {items.map((run) => (
                <RunRow key={run.run_id} run={run} showAgentColumn={showAgentColumn} />
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {cursor && (
        <Button variant="outline" size="sm" className="self-center" onClick={onLoadMore} disabled={loadingMore}>
          {loadingMore && <Spinner />}
          Carregar mais
        </Button>
      )}
    </>
  );
}

function RunRow({ run, showAgentColumn }: { run: RunSummary; showAgentColumn: boolean }) {
  const href = `/runs/${encodeURIComponent(run.run_id)}`;
  // A fala pura (entrada estruturada); em run legado, o texto inteiro que chegou.
  const message = run.input_message || run.message;
  return (
    <tr className="group relative transition-colors hover:bg-accent/50">
      <td className="max-w-0 px-4 py-2.5">
        {/* O link cobre a linha inteira; as células ficam como texto normal para leitores de tela. */}
        <Link href={href} className="absolute inset-0" aria-label={`Abrir trace: ${message ?? run.run_id}`} />
        <p className="truncate font-medium">{message || <span className="text-muted-foreground">(sem mensagem)</span>}</p>
        <p className="truncate text-xs text-muted-foreground">
          {run.user_id ?? "—"}
          {run.endpoint && <> · {run.endpoint}</>}
        </p>
      </td>
      {showAgentColumn && (
        <td className="px-3 py-2.5">
          <span className="truncate">{run.agent_name ?? run.agent_type}</span>
        </td>
      )}
      <td className="px-3 py-2.5">
        <RunStatusBadge status={run.status} title={run.status_message} />
      </td>
      <td className="whitespace-nowrap px-3 py-2.5">
        <span className="inline-flex items-center gap-1.5">
          {run.complexity != null && (
            <Badge variant="outline" title={`Complexidade ${run.complexity} · ${run.tool_calls ?? 0} chamadas de tool`}>
              C{run.complexity}
            </Badge>
          )}
          {!!run.tool_failures && (
            <Badge variant="warning" title="O agente respondeu, mas estas chamadas de tool falharam">
              {run.tool_failures} {run.tool_failures === 1 ? "falhou" : "falharam"}
            </Badge>
          )}
          {run.complexity == null && !run.tool_failures && <span className="text-muted-foreground">—</span>}
        </span>
      </td>
      <td className="px-3 py-2.5">
        {run.agent_version != null ? (
          <Badge variant="outline" title={`configuração v${run.agent_version} · prompt v${run.prompt_version ?? "—"}`}>
            v{run.agent_version}
          </Badge>
        ) : run.prompt_version != null ? (
          <Badge variant="outline" title="prompt (run anterior ao versionamento da configuração)">
            p{run.prompt_version}
          </Badge>
        ) : (
          "—"
        )}
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right tabular-nums">{formatMs(run.latency_ms)}</td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right tabular-nums">{run.total_tokens ? formatNumber(run.total_tokens) : "—"}</td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right tabular-nums">{formatCost(run.cost_usd)}</td>
      <td className="px-3 py-2.5 text-xs">
        <FeedbackCount up={run.feedback_up} down={run.feedback_down} />
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-xs text-muted-foreground">
        <RelativeTime date={run.started_at} />
      </td>
      <td className="pr-3 text-muted-foreground">
        <ChevronRight className="size-4 opacity-0 transition-opacity group-hover:opacity-100" aria-hidden />
      </td>
    </tr>
  );
}
