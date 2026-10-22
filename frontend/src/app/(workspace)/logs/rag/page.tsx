import type { Metadata } from "next";
import { parseLogsQuery } from "@/lib/logs-query";
import { LogsExplorerView } from "@/components/observability/logs-view";

export const metadata: Metadata = { title: "RAG · Logs" };

export default async function LogsRagPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  return <LogsExplorerView view="rag" initial={parseLogsQuery(await searchParams)} />;
}
