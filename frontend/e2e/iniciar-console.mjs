// Sobe o console como a imagem de produção sobe (frontend/Dockerfile): o
// `server.js` do output "standalone", com `public/` e `.next/static/` copiados ao
// lado dele. `next start` avisa que não funciona com `output: "standalone"`.
//
//   node e2e/iniciar-console.mjs --port 3100        (depois de `npm run build`)
import { cpSync, existsSync } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const raiz = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const standalone = path.join(raiz, ".next", "standalone");
if (!existsSync(path.join(standalone, "server.js"))) {
  console.error("Sem .next/standalone/server.js: rode `npm run build` antes.");
  process.exit(2);
}
cpSync(path.join(raiz, "public"), path.join(standalone, "public"), { recursive: true });
cpSync(path.join(raiz, ".next", "static"), path.join(standalone, ".next", "static"), { recursive: true });

const i = process.argv.indexOf("--port");
const port = i > 0 ? process.argv[i + 1] : "3100";
const server = spawn(process.execPath, ["server.js"], {
  cwd: standalone,
  stdio: "inherit",
  env: { ...process.env, NODE_ENV: "production", PORT: port, HOSTNAME: "127.0.0.1" },
});
for (const sinal of ["SIGINT", "SIGTERM"]) process.on(sinal, () => server.kill(sinal));
server.on("exit", (code) => process.exit(code ?? 0));
