/**
 * A conta dos gráficos de tendência dos Logs (`components/observability/overview-panel.tsx`),
 * separada do desenho para ser testada sozinha (`trend.test.ts`, `npm test`).
 *
 * O gráfico de volume responde duas perguntas — quanto rodou e quanto falhou — e com o volume
 * variando 10× de um dia para outro a segunda não aparecia numa pilha absoluta. Por isso:
 * - as barras empilham as falhas primeiro, junto ao eixo (`STACK_KEYS`);
 * - a taxa de falha tem o próprio gráfico, em % (`bucketRate`, `rateScale`);
 * - período com poucas execuções (`FEW_RUNS`) não define a escala: 1 falha em 4 não pode achatar
 *   os dias de volume de verdade perto do zero.
 */
import type { OverviewBucket, OverviewTotals } from "./types";

/** Abaixo disto, uma taxa (1 de 4 = 25%) varia demais para ler como tendência. */
export const FEW_RUNS = 5;

/** De baixo para cima: falhas na base, onde poucas ainda se veem; sucesso no topo. */
export const STACK_KEYS = ["errors", "tool_failure_runs", "success"] as const;

export type RateKey = "failed" | "tool";

/** Fração das execuções do período que falharam (erro ou interrompida) ou que responderam com
 * alguma tool falhando. `null` num período sem execução: lá não há taxa, a linha quebra. */
export function bucketRate(bucket: OverviewBucket, key: RateKey): number | null {
  if (!bucket.runs) return null;
  return (key === "failed" ? bucket.errors : bucket.tool_failure_runs) / bucket.runs;
}

/** A taxa de falha do período inteiro — a linha de referência do gráfico. */
export function periodFailureRate(totals: OverviewTotals): number | null {
  return totals.runs > 0 ? (totals.errors + totals.interrupted) / totals.runs : null;
}

/** Teto "redondo" para o eixo (1, 1,5, 2, 3, 4, 5, 6, 8 × 10ⁿ): perto do dado, sem sobrar meio gráfico vazio. */
export function niceMax(value: number): number {
  if (value <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(value)));
  const step = [1, 1.5, 2, 3, 4, 5, 6, 8, 10].find((m) => m * exp >= value) ?? 10;
  // `toPrecision`: 3 × 0,1 dá 0,30000000000000004 em ponto flutuante, e o meio do eixo, 0,15000000000000002.
  return Number((step * exp).toPrecision(12));
}

/** A escala do gráfico de taxas: o teto do eixo e se algum ponto ficou acima dele.
 *
 * O teto vem só dos períodos com `FEW_RUNS` execuções ou mais (e da média do período); se nenhum
 * tem, de todos. Fica entre 5% (uma taxa baixa não vira um gráfico de ruído) e 100%. O ponto de
 * amostra pequena acima do teto é desenhado na borda — `clipped` diz se há algum. */
export function rateScale(buckets: OverviewBucket[], average: number | null): { max: number; clipped: boolean } {
  const keys: RateKey[] = ["failed", "tool"];
  const points = buckets.flatMap((bucket) =>
    keys.map((key) => ({ value: bucketRate(bucket, key), runs: bucket.runs }))
  );
  const present = points.filter((p): p is { value: number; runs: number } => p.value != null);
  const reliable = present.filter((p) => p.runs >= FEW_RUNS);
  const pool = (reliable.length ? reliable : present).map((p) => p.value);
  const highest = Math.max(0, average ?? 0, ...pool);
  // Sem falha nenhuma, 0–5%: um eixo até 100% deixaria as linhas coladas no zero sem motivo.
  const max = highest > 0 ? Math.min(1, Math.max(0.05, niceMax(highest))) : 0.05;
  return { max, clipped: present.some((p) => p.value > max) };
}
