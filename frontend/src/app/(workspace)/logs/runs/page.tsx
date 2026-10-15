import type { Metadata } from "next";
import { parseLogsQuery } from "@/lib/logs-query";
import { LogsExplorerView } from "@/components/observability/logs-view";

export const metadata: Metadata = { title: "Execuções · Logs" };

export default async function LogsRunsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  return <LogsExplorerView view="runs" initial={parseLogsQuery(await searchParams)} />;
}
