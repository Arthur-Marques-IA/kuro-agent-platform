import { proxyJson } from "@/lib/api";

/** Sites da coleção (`documents/web_sync.py` no backend). */
export async function GET(_request: Request, { params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  return proxyJson(`/collections/${encodeURIComponent(name)}/sources`);
}

/** Cadastra um site e dispara a primeira sincronização em background (202). */
export async function POST(request: Request, { params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  return proxyJson(`/collections/${encodeURIComponent(name)}/sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: await request.text(),
  });
}
