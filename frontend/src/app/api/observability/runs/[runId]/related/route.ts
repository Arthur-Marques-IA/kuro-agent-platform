import { proxyJson } from "@/lib/api";

/** Runs que compartilham com este uma chave de correlação (ex.: `ciclo_id`), em ordem. */
export async function GET(_request: Request, { params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  return proxyJson(`/observability/runs/${encodeURIComponent(runId)}/related`);
}
