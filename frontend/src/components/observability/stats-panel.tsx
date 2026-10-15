"use client";

import { useEffect, useState } from "react";
import { Activity, AlertCircle, Clock, Coins, ListChecks } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import { formatCost, formatDate, formatMs, formatNumber } from "@/lib/format";
import type { RunStats } from "@/lib/types";
import { Card } from "@/components/ui/card";
import { EmptyState, Skeleton } from "@/components/ui/primitives";
import { apiSearchParams } from "@/lib/logs-query";

const STATUS_DOT: Record<string, string | string[]> = {
  success: "bg-success",
  error: "bg-destructive",
  interrupted: "bg-warning",
};

const STATUS_LABEL: Record<string, string | string[]> = {
  success: "Sucesso",
  error: "Erro",
  interrupted: "Interrompido",
};

/** Busca `/api/observability/stats` com os filtros dados — reaproveitado pelos Logs (geral) e por sessão. */
export function useStats(params: Record<string, string | string[]>) {
  const [stats, setStats] = useState<RunStats | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const paramsKey = JSON.stringify(params);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setState("loading");
    setError(null);
    requestJson<RunStats>(`/api/observability/stats?${apiSearchParams(params)}`, {
      fallbackError: "Falha ao carregar estatísticas",
    })
      .then((data) => {
        if (cancelled) return;
        setStats(data);
        setState("ready");
      })
      .catch((err) => {
        if (cancelled) return;
        setError(errorMessage(err));
        setState("error");
      });
    return () => {
      cancelled = true;
    };
    // `params` é um objeto novo a cada render do chamador; `paramsKey` é a identidade estável.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramsKey]);

  return { stats, state, error };
}

/**
 * KPIs + gráfico de execuções por dia. `showTrend=false` esconde o gráfico —
 * uma sessão isolada normalmente cabe num único dia, então a série temporal
 * não mostra tendência nenhuma; só os KPIs agregados fazem sentido ali.
 */
export function StatsPanel({ params, showTrend = true }: { params: Record<string, string | string[]>; showTrend?: boolean }) {
  const { stats, state, error } = useStats(params);

  if (state === "loading") {
    return (
      <Card className="grid grid-cols-2 gap-4 p-5 sm:grid-cols-5" aria-busy="true" aria-label="Carregando estatísticas">
        {[1, 2, 3, 4, 5].map((i) => (
          <Skeleton key={i} className="h-12" />
        ))}
      </Card>
    );
  }

  if (state === "error") {
    return (
      <Card>
        <EmptyState icon={Activity} title="Não foi possível carregar as estatísticas" description={error} />
      </Card>
    );
  }

  if (!stats || stats.total_runs === 0) {
    return (
      <Card>
        <EmptyState icon={Activity} title="Sem execuções no período" description="Ajuste os filtros ou a janela de datas." />
      </Card>
    );
  }

  const errorRate = (stats.status_counts.error ?? 0) / stats.total_runs;

  return (
    <Card className="flex flex-col gap-5 p-5">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
        <Kpi icon={ListChecks} label="Execuções" value={formatNumber(stats.total_runs)} />
        <Kpi icon={AlertCircle} label="Taxa de erro" value={`${(errorRate * 100).toFixed(1)}%`} />
        <Kpi icon={Coins} label="Tokens" value={formatNumber(stats.total_tokens)} />
        <Kpi icon={Activity} label="Custo" value={formatCost(stats.total_cost_usd)} />
        <Kpi icon={Clock} label="Latência média" value={formatMs(stats.avg_latency_ms)} />
      </div>

      {showTrend && stats.buckets.length > 0 && <RunsChart buckets={stats.buckets} />}

      <div className="flex flex-wrap items-center gap-4 border-t border-border pt-4 text-xs text-muted-foreground">
        {Object.entries(stats.status_counts).map(([status, count]) => (
          <span key={status} className="inline-flex items-center gap-1.5">
            <span className={`size-2 rounded-full ${STATUS_DOT[status] ?? "bg-muted-foreground"}`} aria-hidden />
            {STATUS_LABEL[status] ?? status}: {formatNumber(count ?? 0)}
          </span>
        ))}
        <span className="ml-auto">
          Calculado sobre as {formatNumber(stats.scanned)} execuções mais recentes do período.
        </span>
      </div>
    </Card>
  );
}

function Kpi({ icon: Icon, label, value }: { icon: typeof Activity; label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        <Icon className="size-3.5" aria-hidden />
        {label}
      </span>
      <span className="text-lg font-semibold tabular-nums">{value}</span>
    </div>
  );
}

const CHART_HEIGHT = 100;

function RunsChart({ buckets }: { buckets: RunStats["buckets"] }) {
  const max = Math.max(...buckets.map((b) => b.runs), 1);
  const barWidth = 100 / buckets.length;

  return (
    <div>
      <svg viewBox={`0 0 100 ${CHART_HEIGHT}`} preserveAspectRatio="none" className="h-24 w-full" role="img" aria-label="Execuções por dia">
        {buckets.map((bucket, i) => {
          const success = bucket.runs - bucket.errors;
          const successHeight = (success / max) * (CHART_HEIGHT - 4);
          const errorHeight = (bucket.errors / max) * (CHART_HEIGHT - 4);
          const x = i * barWidth + barWidth * 0.15;
          const width = Math.max(barWidth * 0.7, 0.5);
          return (
            <g key={bucket.date}>
              <title>{`${formatDate(bucket.date)}: ${formatNumber(bucket.runs)} execuções, ${formatNumber(bucket.errors)} erro(s), ${formatNumber(bucket.total_tokens)} tokens`}</title>
              <rect x={x} y={CHART_HEIGHT - successHeight} width={width} height={Math.max(successHeight, bucket.runs > bucket.errors ? 1 : 0)} className="fill-primary" />
              {errorHeight > 0 && (
                <rect x={x} y={CHART_HEIGHT - successHeight - errorHeight} width={width} height={Math.max(errorHeight, 1)} className="fill-destructive" />
              )}
            </g>
          );
        })}
      </svg>
      <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
        <span>{formatDate(buckets[0].date)}</span>
        {buckets.length > 1 && <span>{formatDate(buckets[buckets.length - 1].date)}</span>}
      </div>
    </div>
  );
}
