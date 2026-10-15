import { proxyJson } from "@/lib/api";

export async function DELETE(_request: Request, { params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  return proxyJson(`/collections/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export async function PUT(request: Request, { params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  return proxyJson(`/collections/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: await request.text(),
  });
}
