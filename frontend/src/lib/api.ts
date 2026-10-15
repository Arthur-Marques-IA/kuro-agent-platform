/**
 * Cliente do agent-service, usado SÓ server-side (Route Handlers em
 * `app/api/**` e Server Components). O browser nunca fala direto com o
 * backend — isso é o que torna o Next.js um BFF aqui: um único hostname/porta
 * pro cliente, sem CORS, e o endereço interno do agent-service nunca vaza pro
 * bundle do browser (por isso a env var não tem prefixo NEXT_PUBLIC_).
 */

const AGENT_SERVICE_URL = process.env.AGENT_SERVICE_URL ?? "http://localhost:8000";

export function backendUrl(path: string): string {
  return `${AGENT_SERVICE_URL}${path}`;
}

/**
 * Chave de API do agent-service, injetada server-side. O console usa o escopo
 * `admin` (edita agente, lê trace e sessão), então esta variável NÃO pode ter
 * prefixo NEXT_PUBLIC_: ela nunca vai para o bundle do browser. É o mesmo
 * motivo de o BFF existir — quem fala com o backend é o servidor do Next.
 */
const AGENT_SERVICE_API_KEY = backendApiKey();

/**
 * A chave que o console manda. No compose ela é a `ADMIN_API_KEY`, que numa rotação
 * traz duas separadas por vírgula (`nova,antiga`): vai a primeira, a atual.
 */
export function backendApiKey(): string | undefined {
  return (process.env.AGENT_SERVICE_API_KEY ?? "").split(",").map((k) => k.trim()).find(Boolean);
}

/** Acrescenta o header de autenticação, preservando os headers da chamada. */
function withAuth(init?: RequestInit): RequestInit {
  if (!AGENT_SERVICE_API_KEY) return { ...init, cache: "no-store" };
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${AGENT_SERVICE_API_KEY}`);
  return { ...init, headers, cache: "no-store" };
}

/**
 * URL do agent-service *como outros módulos a enxergam* — usada só para
 * montar exemplos de integração (cURL etc.), nunca para requisições.
 */
export function publicApiUrl(): string {
  return configuredPublicApiUrl() ?? "http://localhost:58000";
}

/**
 * A URL pública só quando alguém a configurou. Sem ela, o contrato de integração
 * deixa o agent-service decidir (KURO_PUBLIC_URL, KURO_API_DOMAIN, profile `ip`).
 */
export function configuredPublicApiUrl(): string | undefined {
  return process.env.AGENT_SERVICE_PUBLIC_URL?.trim() || undefined;
}

const NO_BODY_STATUSES = new Set([204, 205, 304]);

function unavailable(error: unknown): Response {
  console.error("[bff] agent-service indisponível:", error);
  return Response.json({ detail: "agent-service indisponível no momento." }, { status: 502 });
}

/** Proxy simples: repassa a resposta do backend como está (status, corpo, content-type). */
export async function proxyJson(path: string, init?: RequestInit): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await fetch(backendUrl(path), withAuth(init));
  } catch (error) {
    return unavailable(error);
  }
  // Response não aceita corpo (nem "") quando o status é 204/205/304 — ex.
  // o DELETE /agents/{type} do backend responde 204 sem corpo.
  if (NO_BODY_STATUSES.has(upstream.status)) {
    return new Response(null, { status: upstream.status });
  }
  const body = await upstream.text();
  return new Response(body, {
    status: upstream.status,
    headers: { "Content-Type": upstream.headers.get("Content-Type") ?? "application/json" },
  });
}

/** Proxy de streaming: repassa o corpo (ReadableStream) sem bufferizar — essencial pro SSE do chat. */
export async function proxyStream(path: string, init?: RequestInit): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await fetch(backendUrl(path), withAuth(init));
  } catch (error) {
    return unavailable(error);
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("Content-Type") ?? "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}

/** GET JSON para Server Components: `null` se o backend falhar ou responder erro. */
export async function fetchBackendJson<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(backendUrl(path), withAuth());
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}
