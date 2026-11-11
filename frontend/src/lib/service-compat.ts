/**
 * Console × serviço: as duas imagens são construídas separadas, e um console novo contra um
 * serviço antigo quebra em telas que leem campos que o serviço ainda não devolve.
 *
 * Regra (simples de propósito): o console declara a versão MÍNIMA do agent-service que ele
 * exige (`MIN_SERVICE_VERSION`). O serviço é compatível quando o `version` do `GET /health` é
 * igual ou maior que ela, comparando os números segmento a segmento (`0.10` > `0.9`; sufixos
 * como `.dev0` ou `+abc` são ignorados). Serviço inacessível, sem `version` (anterior à 0.2.0)
 * ou com uma versão que não dá para ler (`"desconhecida"`) → aviso.
 *
 * Suba esta constante quando o console passar a depender de algo de uma versão nova do
 * serviço (o número sai de `docs/notas-de-versao.md`). Roda com `npm test`.
 */
export const MIN_SERVICE_VERSION = "0.2.0";

/** Hash do build do console (`NEXT_PUBLIC_BUILD_SHA`, assado no build); `dev` fora da imagem. */
export const CONSOLE_BUILD = process.env.NEXT_PUBLIC_BUILD_SHA || "dev";

/** `"0.2.0"` → `[0, 2, 0]`; `null` se não começar por números separados por ponto. */
export function parseVersion(raw: string | null | undefined): number[] | null {
  const match = /^\s*v?(\d+(?:\.\d+)*)/.exec(raw ?? "");
  if (!match) return null;
  return match[1].split(".").map(Number);
}

/** Negativo se `a < b`, zero se iguais, positivo se `a > b` (segmentos faltando valem 0). */
export function compareVersions(a: number[], b: number[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const diff = (a[i] ?? 0) - (b[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

export type ServiceCompat =
  | { ok: true }
  | { ok: false; reason: "unreachable" | "unknown" | "too_old"; message: string };

/**
 * O serviço atende este console? `reachable=false` quando o `/health` não respondeu;
 * `version` é o campo do `/health` (ausente em serviços anteriores à 0.2.0).
 */
export function serviceCompat(
  reachable: boolean,
  version: string | null | undefined,
  minimum: string = MIN_SERVICE_VERSION
): ServiceCompat {
  if (!reachable) {
    return { ok: false, reason: "unreachable", message: "Não deu para ler a versão do serviço: ele não respondeu." };
  }
  const parsed = parseVersion(version);
  if (!parsed) {
    return {
      ok: false,
      reason: "unknown",
      message: `O serviço não informa uma versão legível (${version ? `"${version}"` : "sem versão"}); este console exige ${minimum} ou mais nova.`,
    };
  }
  const min = parseVersion(minimum) ?? [0];
  if (compareVersions(parsed, min) < 0) {
    return {
      ok: false,
      reason: "too_old",
      message: `O serviço está na ${version} e este console exige ${minimum} ou mais nova. Reconstrua as duas imagens juntas.`,
    };
  }
  return { ok: true };
}
