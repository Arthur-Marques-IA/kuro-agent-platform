import { backendApiKey, backendUrl, proxyJson } from "@/lib/api";

const LIST_PARAMS = ["limit", "page", "sort_by", "sort_order", "knowledge_id"];

/** Conteúdo já ingerido, com status de processamento — alimenta a tabela da base de conhecimento. */
export async function GET(request: Request) {
  const incoming = new URL(request.url).searchParams;
  const params = new URLSearchParams();
  for (const key of LIST_PARAMS) {
    const value = incoming.get(key);
    if (value) params.set(key, value);
  }
  return proxyJson(`/knowledge/content?${params}`);
}

/**
 * Proxy pro upload do AgentOS (`POST /knowledge/content`, multipart: arquivo
 * ou URL). Diferente dos outros proxies, aqui a gente lê o FormData
 * (bufferiza em memória) e remonta — repassar o body cru de um multipart sem
 * reescrever o boundary/Content-Length é mais frágil do que vale a pena para
 * upload avulso pelo console. Para arquivos grandes, prefira ingestão por
 * texto/URL ou aumente esse limite com cuidado.
 */
export async function POST(request: Request) {
  const formData = await request.formData();
  // O header vai montado à mão: `withAuth` não serve aqui porque o corpo é
  // multipart e a chamada não passa por `proxyJson`. Sem isto, o upload era o
  // único caminho do console que dava 401 com as chaves ligadas.
  const headers = new Headers();
  const apiKey = backendApiKey();
  if (apiKey) headers.set("Authorization", `Bearer ${apiKey}`);
  let upstream: Response;
  try {
    upstream = await fetch(backendUrl("/knowledge/content"), {
      method: "POST",
      body: formData,
      headers,
      cache: "no-store",
    });
  } catch {
    return Response.json({ detail: "agent-service indisponível no momento." }, { status: 502 });
  }
  const body = await upstream.text();
  return new Response(body, {
    status: upstream.status,
    headers: { "Content-Type": upstream.headers.get("Content-Type") ?? "application/json" },
  });
}
