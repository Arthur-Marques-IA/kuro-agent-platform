import { proxyJson } from "@/lib/api";

/** A linha do tempo da sessão (`GET /observability/sessions/{id}/timeline`): um turno por run, já separado em campos. */
export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const incoming = new URL(request.url).searchParams;
  const query = new URLSearchParams();
  for (const key of ["user_id", "limit", "cursor"]) {
    const value = incoming.get(key);
    if (value) query.set(key, value);
  }
  const suffix = query.size ? `?${query}` : "";
  return proxyJson(`/observability/sessions/${encodeURIComponent(id)}/timeline${suffix}`);
}
