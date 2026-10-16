import { proxyJson } from "@/lib/api";

/** Documentos com mais marcações de trecho (padrão: errado/desatualizado), com os runs. */
export async function GET(request: Request) {
  const { search } = new URL(request.url);
  return proxyJson(`/observability/chunk-feedback/flagged${search}`);
}
