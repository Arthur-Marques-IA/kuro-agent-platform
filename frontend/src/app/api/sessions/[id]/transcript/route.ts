import { proxyJson } from "@/lib/api";

/** A conversa compacta + o que o agente lembra (resumo da sessão e memórias do contato). */
export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxyJson(`/observability/sessions/${encodeURIComponent(id)}/transcript`);
}
