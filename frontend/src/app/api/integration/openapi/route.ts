import { proxyJson } from "@/lib/api";

/**
 * O OpenAPI de quem integra: só as rotas que a chave `runtime` alcança (`/chat`,
 * `/chat/stream`, `/analyze`, scores...). O `/docs` do agent-service exige a chave
 * admin, então o link do console para ele dava 403 no navegador; por aqui o BFF
 * busca com a chave do console e o navegador nunca a vê.
 */
export async function GET() {
  return proxyJson("/integration/openapi.json");
}
