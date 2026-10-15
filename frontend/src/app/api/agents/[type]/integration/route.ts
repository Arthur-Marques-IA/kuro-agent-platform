import { configuredPublicApiUrl, proxyJson } from "@/lib/api";

/**
 * Contrato de integração derivado pelo backend. O console consome isto em vez
 * de montar o próprio exemplo: assim o cURL já vem com o endpoint certo para o
 * tipo do agente, as dependencies que ele declara e o header de autenticação.
 *
 * Sem `base_url` na tela nem `AGENT_SERVICE_PUBLIC_URL` configurada, quem decide a
 * URL dos exemplos é o agent-service (KURO_PUBLIC_URL, KURO_API_DOMAIN, profile `ip`):
 * um `http://localhost:58000` fixo aqui venceria a URL pública que ele conhece.
 */
export async function GET(request: Request, { params }: { params: Promise<{ type: string }> }) {
  const { type } = await params;
  const baseUrl = new URL(request.url).searchParams.get("base_url") || configuredPublicApiUrl();
  const query = baseUrl ? `?${new URLSearchParams({ base_url: baseUrl })}` : "";
  return proxyJson(`/agents/${encodeURIComponent(type)}/integration${query}`);
}
