import { proxyJson } from "@/lib/api";

/** Apaga uma marcação de trecho. */
export async function DELETE(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyJson(`/observability/chunk-feedback/${encodeURIComponent(id)}`, { method: "DELETE" });
}
