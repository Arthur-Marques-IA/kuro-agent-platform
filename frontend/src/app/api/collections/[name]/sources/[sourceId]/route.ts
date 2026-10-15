import { proxyJson } from "@/lib/api";

type Params = { params: Promise<{ name: string; sourceId: string }> };

function path(name: string, sourceId: string) {
  return `/collections/${encodeURIComponent(name)}/sources/${encodeURIComponent(sourceId)}`;
}

/** O site e as páginas dele, com o status de cada uma. */
export async function GET(_request: Request, { params }: Params) {
  const { name, sourceId } = await params;
  return proxyJson(path(name, sourceId));
}

export async function PATCH(request: Request, { params }: Params) {
  const { name, sourceId } = await params;
  return proxyJson(path(name, sourceId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: await request.text(),
  });
}

/** Remove o site — e as páginas dele da coleção, a menos de `?keep_documents=true`. */
export async function DELETE(request: Request, { params }: Params) {
  const { name, sourceId } = await params;
  return proxyJson(`${path(name, sourceId)}${new URL(request.url).search}`, { method: "DELETE" });
}
