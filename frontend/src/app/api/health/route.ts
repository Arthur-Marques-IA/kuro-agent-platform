import { backendUrl } from "@/lib/api";

/**
 * Status e versão do agent-service para o rodapé da sidebar. Nunca lança: indisponível vira 503.
 * `version` é o campo do `GET /health` do serviço (`null` quando ele não devolve — anterior à 0.2.0).
 */
export async function GET() {
  try {
    const res = await fetch(backendUrl("/health"), { cache: "no-store", signal: AbortSignal.timeout(4000) });
    const body = (await res.json().catch(() => null)) as { version?: unknown } | null;
    const version = typeof body?.version === "string" ? body.version : null;
    return Response.json({ status: res.ok ? "ok" : "degraded", version }, { status: res.ok ? 200 : 503 });
  } catch {
    return Response.json({ status: "unavailable", version: null }, { status: 503 });
  }
}
