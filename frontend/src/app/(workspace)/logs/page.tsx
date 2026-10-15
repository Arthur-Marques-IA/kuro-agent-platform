import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { legacyLogsRedirect, parseLogsQuery } from "@/lib/logs-query";
import { LogsOverviewView } from "@/components/observability/logs-view";

export const metadata: Metadata = { title: "Panorama · Logs" };

export default async function LogsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  // `/logs?user_id=` e `/logs?session_id=` eram as listas filtradas; hoje elas têm rota própria.
  const legacy = legacyLogsRedirect(params);
  if (legacy) redirect(legacy);
  return <LogsOverviewView initial={parseLogsQuery(params)} />;
}
