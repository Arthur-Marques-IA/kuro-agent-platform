"use client";

import { useEffect, useState } from "react";
import { PERIODS, logsRange, type LogsQuery, type Period } from "@/lib/logs-query";

export { PERIODS, type Period };

/** `Date.now()` é impuro: calculado num efeito (não durante a renderização) e guardado em estado. */
export function usePeriodSince(period: Period): string | null {
  const [since, setSince] = useState<string | null>(null);
  useEffect(() => {
    const days = PERIODS.find((candidate) => candidate.value === period)?.days;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSince(days ? new Date(Date.now() - days * 86_400_000).toISOString() : null);
  }, [period]);
  return since;
}

/**
 * `since`/`until` do recorte dos Logs: o atalho do período ou o intervalo livre (`from`/`to`). `ready` fica
 * falso até o primeiro cálculo, para ninguém buscar "todo o período" antes de saber o recorte.
 */
export function useLogsRange(query: Pick<LogsQuery, "period" | "from" | "to">) {
  const [range, setRange] = useState<{ since: string | null; until: string | null; ready: boolean }>({
    since: null,
    until: null,
    ready: false,
  });
  const { period, from, to } = query;
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setRange({ ...logsRange({ period, from, to }, Date.now()), ready: true });
  }, [period, from, to]);
  return range;
}
