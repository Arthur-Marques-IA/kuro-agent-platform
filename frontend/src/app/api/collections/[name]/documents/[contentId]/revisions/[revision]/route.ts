import { proxyJson } from "@/lib/api";

/** Uma revisão do documento, com o texto quando ele é texto. */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ name: string; contentId: string; revision: string }> },
) {
  const { name, contentId, revision } = await params;
  return proxyJson(
    `/collections/${encodeURIComponent(name)}/documents/${encodeURIComponent(contentId)}/revisions/${encodeURIComponent(revision)}`,
  );
}
