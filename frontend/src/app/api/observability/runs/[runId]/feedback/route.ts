import { proxyJson } from "@/lib/api";

/** 👍/👎 de um run, com comentário e (analista) a saída correta, que vira a referência. */
export async function POST(request: Request, { params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  return proxyJson(`/observability/runs/${encodeURIComponent(runId)}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: await request.text(),
  });
}
