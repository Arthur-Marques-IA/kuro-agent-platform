import { defineConfig, devices } from "@playwright/test";

/**
 * Smoke do console (`npm run e2e`): o console de produção (`next build` +
 * `next start`) contra o agent-service desta árvore servido por
 * `scripts/servico_de_teste.py` — o app real, com SQLite temporário e um modelo
 * determinístico no lugar do provedor de LLM. Portas próprias, para não
 * esbarrar no `npm run dev` (3000) nem no compose (58000).
 */
const BACKEND_PORT = Number(process.env.E2E_BACKEND_PORT ?? 58100);
const FRONTEND_PORT = Number(process.env.E2E_FRONTEND_PORT ?? 3100);
const ADMIN_KEY = "e2e-admin-key";
const CI = Boolean(process.env.CI);

export default defineConfig({
  testDir: "./e2e",
  // Um fluxo só, que cria dados: em série, sem paralelismo.
  fullyParallel: false,
  workers: 1,
  forbidOnly: CI,
  retries: 0,
  // O fluxo leva 20–60 s (a primeira resposta do backend recém-subido é a mais lenta).
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${FRONTEND_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: [
    {
      // `uv run` acha o pyproject.toml subindo a partir de frontend/.
      command: `uv run python ../scripts/servico_de_teste.py --port ${BACKEND_PORT}`,
      url: `http://127.0.0.1:${BACKEND_PORT}/health`,
      env: { KURO_TESTE_ADMIN_KEY: ADMIN_KEY },
      // Importar o serviço (Agno, SDKs dos provedores) leva perto de um minuto no Windows.
      timeout: 240_000,
      reuseExistingServer: !CI,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      // Build de produção servido como na imagem (o `server.js` do standalone): é o que vai
      // para o ar, e a recomendação do guia de Playwright do Next.
      // `E2E_SKIP_BUILD=1` reaproveita um `.next` já construído.
      command: `${process.env.E2E_SKIP_BUILD ? "" : "npm run build && "}node e2e/iniciar-console.mjs --port ${FRONTEND_PORT}`,
      url: `http://127.0.0.1:${FRONTEND_PORT}/agents`,
      env: {
        AGENT_SERVICE_URL: `http://127.0.0.1:${BACKEND_PORT}`,
        AGENT_SERVICE_API_KEY: ADMIN_KEY,
        NEXT_BASE_PATH: "",
      },
      // Inclui o `next build`: 2–4 min numa máquina livre, bem mais com outra carga junto.
      timeout: 600_000,
      reuseExistingServer: !CI,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
