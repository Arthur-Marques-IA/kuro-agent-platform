import { proxyJson } from "@/lib/api";

export async function GET(_request: Request, { params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  return proxyJson(`/collections/${encodeURIComponent(name)}/versions`);
}
