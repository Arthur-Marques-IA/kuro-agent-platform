import { proxyJson } from "@/lib/api";

/** Coleta o site de novo agora. */
export async function POST(_request: Request, { params }: { params: Promise<{ name: string; sourceId: string }> }) {
  const { name, sourceId } = await params;
  return proxyJson(`/collections/${encodeURIComponent(name)}/sources/${encodeURIComponent(sourceId)}/sync`, {
    method: "POST",
  });
}
