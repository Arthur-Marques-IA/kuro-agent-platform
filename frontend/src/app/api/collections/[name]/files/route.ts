import { backendApiKey, backendUrl } from "@/lib/api";

/**
 * Upload de arquivo para uma collection qualquer.
 *
 * Não passa por `proxyJson` porque o corpo é multipart: reenviar o `FormData`
 * deixa o fetch montar um boundary novo, e repassar o corpo cru exigiria
 * reescrever boundary e Content-Length. O header de autenticação é montado
 * aqui pelo mesmo motivo — e a falta dele era o furo do upload anterior, que
 * dava 401 com as chaves ligadas.
 */
export async function POST(request: Request, { params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  const formData = await request.formData();
  const headers = new Headers();
  const apiKey = backendApiKey();
  if (apiKey) headers.set("Authorization", `Bearer ${apiKey}`);

  let upstream: Response;
  try {
    upstream = await fetch(backendUrl(`/collections/${encodeURIComponent(name)}/files`), {
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
