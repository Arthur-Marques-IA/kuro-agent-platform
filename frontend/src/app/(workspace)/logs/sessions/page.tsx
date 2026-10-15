import type { Metadata } from "next";
import { parseLogsQuery } from "@/lib/logs-query";
import { LogsExplorerView } from "@/components/observability/logs-view";

export const metadata: Metadata = { title: "Sessões · Logs" };

export default async function LogsSessionsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  return <LogsExplorerView view="sessions" initial={parseLogsQuery(await searchParams)} />;
}
