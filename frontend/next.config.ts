import type { NextConfig } from "next";

// Vazio por padrão (dev local, raiz "/"). Em produção o console é servido sob
// um path do mesmo domínio do Regente (ex.: /r8-novo-ui), atrás do Traefik —
// só o build precisa saber disso (basePath vira parte do bundle), por isso
// ARG no Dockerfile em vez de env de runtime.
const basePath = process.env.NEXT_BASE_PATH || "";

const nextConfig: NextConfig = {
  // Gera .next/standalone com só o necessário pra rodar em produção —
  // usado pelo Dockerfile pra não precisar copiar node_modules inteiro.
  output: "standalone",
  ...(basePath ? { basePath } : {}),
  // <Link>/router.push já são basePath-aware sozinhos, mas fetch() de rota
  // absoluta ("/api/...") no client não é — o Next não reescreve isso. Os
  // client components que chamam a API do próprio BFF (sidebar, chat) usam
  // esta env pra montar a URL certa (ver frontend/src/lib/http.ts).
  // NEXT_PUBLIC_BUILD_SHA: hash do commit do build (ARG do Dockerfile), mostrado no rodapé da
  // sidebar ao lado da versão do serviço. Fora da imagem fica "dev".
  env: { NEXT_PUBLIC_BASE_PATH: basePath, NEXT_PUBLIC_BUILD_SHA: process.env.NEXT_PUBLIC_BUILD_SHA || "dev" },
  // Rotas antigas (abas separadas) → áreas equivalentes do console unificado.
  async redirects() {
    return [
      { source: "/admin", destination: "/knowledge", permanent: false },
      { source: "/docs", destination: "/agents", permanent: false },
      { source: "/observability", destination: "/logs", permanent: false },
      { source: "/observability/:path*", destination: "/logs/:path*", permanent: false },
    ];
  },
};

export default nextConfig;
