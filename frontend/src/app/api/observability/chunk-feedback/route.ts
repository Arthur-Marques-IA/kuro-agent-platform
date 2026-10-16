import { proxyJson } from "@/lib/api";

/** Marcações de trecho recuperado (`?run_id=`, `?collection=`, `?label=`). */
export async function GET(request: Request) {
  const { search } = new URL(request.url);
  return proxyJson(`/observability/chunk-feedback${search}`);
}

/** Marca um trecho do trace como relevante, irrelevante ou errado/desatualizado. */
export async function POST(request: Request) {
  return proxyJson("/observability/chunk-feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: await request.text(),
  });
}
