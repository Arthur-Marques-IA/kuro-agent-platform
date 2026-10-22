import { proxyJson } from "@/lib/api";

/** Revisões do documento, mais recente primeiro (sem o texto). */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ name: string; contentId: string }> },
) {
  const { name, contentId } = await params;
  return proxyJson(`/collections/${encodeURIComponent(name)}/documents/${encodeURIComponent(contentId)}/revisions`);
}
