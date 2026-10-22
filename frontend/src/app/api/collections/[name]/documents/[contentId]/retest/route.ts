import { proxyJson } from "@/lib/api";

/** Roda de novo a pergunta de cada marcação do documento na busca atual (sem modelo). */
export async function POST(
  request: Request,
  { params }: { params: Promise<{ name: string; contentId: string }> },
) {
  const { name, contentId } = await params;
  const { search } = new URL(request.url);
  return proxyJson(
    `/collections/${encodeURIComponent(name)}/documents/${encodeURIComponent(contentId)}/retest${search}`,
    { method: "POST" },
  );
}
