# syntax=docker/dockerfile:1

FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# AGENT_SERVICE_URL é lido em runtime (process.env), não precisa existir no build.
# NEXT_BASE_PATH é diferente: vira basePath do Next (next.config.ts), que é
# assado no bundle — por isso ARG de build, não ENV de runtime. Vazio por
# padrão (dev local, serve na raiz "/").
ARG NEXT_BASE_PATH=""
ENV NEXT_BASE_PATH=$NEXT_BASE_PATH
# Hash do commit, só para exibir no rodapé do console (ao lado da versão do serviço). O contexto
# do build é ./frontend, sem .git, então ele chega como ARG (o compose passa KURO_BUILD_SHA).
ARG NEXT_PUBLIC_BUILD_SHA=dev
ENV NEXT_PUBLIC_BUILD_SHA=$NEXT_PUBLIC_BUILD_SHA
RUN npm run build

# Runner: só o output "standalone" do Next.js, sem node_modules completo nem
# código-fonte — imagem final bem menor.
FROM node:22-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN groupadd --system nextjs && useradd --system --gid nextjs nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nextjs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0

CMD ["node", "server.js"]
