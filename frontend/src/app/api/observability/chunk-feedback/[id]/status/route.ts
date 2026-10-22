import { proxyJson } from "@/lib/api";

/** Dispensa, resolve sem editar ou reabre uma marcação de trecho. */
export async function POST(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyJson(`/observability/chunk-feedback/${encodeURIComponent(id)}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: await request.text(),
  });
}
