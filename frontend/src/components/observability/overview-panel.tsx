"use client";

import { Fragment, useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ArrowDownRight, ArrowRight, ArrowUpRight, ChevronDown, ChevronRight, Table2, Wrench } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import { formatCost, formatMs, formatNumber, formatRelativeTime } from "@/lib/format";
import { TOOL_FAILURE_META } from "@/lib/tool-failures";
import { FEW_RUNS, STACK_KEYS, bucketRate, niceMax, periodFailureRate, rateScale } from "@/lib/trend";
import { apiSearchParams, type LogsQuery } from "@/lib/logs-query";
import type { Overview, OverviewAgent, OverviewBucket, OverviewTotals } from "@/lib/types";
import { AgentAvatar } from "@/components/agents/agent-avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, Skeleton } from "@/components/ui/primitives";
import { cn } from "@/lib/cn";

/**
 * O dashboard dos Logs, organizado pelas perguntas que ele responde:
 * tem algo quebrado? (KPIs com o período anterior, tools falhando), para onde vai
 * o dinheiro? (custo por agente e por conversa), a última versão melhorou?
 * (versões de cada agente), e como isso evolui (série no tempo, sem buracos).
 * Os dados vêm prontos de `GET /observability/overview`. Investigar um caso é nas listas de
 * Sessões e Execuções: `runsHref` monta o link de cada número para as execuções que ele conta.
 */

const TIMEZONE = "America/Sao_Paulo";

/** `enabled` falso segura a busca (o recorte ainda não foi calculado). */
export function useOverview(params: Record<string, string | string[]>, enabled = true) {
  const [data, setData] = useState<Overview | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const key = JSON.stringify(params);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    // Recarregar mantém o desenho anterior (com opacidade menor): sem esqueleto piscando.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setState("loading");
    requestJson<Overview>(`/api/observability/overview?${apiSearchParams({ tz: TIMEZONE, ...params })}`, {
      fallbackError: "Falha ao carregar o panorama",
    })
      .then((overview) => {
        if (cancelled) return;
        setData(overview);
        setError(null);
        setState("ready");
      })
      .catch((err) => {
        if (cancelled) return;
        setError(errorMessage(err));
        setState("error");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce, enabled]);

  return { data, state, error, reload: () => setNonce((n) => n + 1) };
}

/** O link para a lista de execuções com um recorte a mais (agente, versão, tool falhando). */
type RunsHref = (drill: Partial<LogsQuery>) => string;

export function OverviewPanel({ overview, runsHref }: { overview: ReturnType<typeof useOverview>; runsHref: RunsHref }) {
  const { data, state, error } = overview;

  if (!data) {
    if (state === "error") {
      return (
        <Card>
          <EmptyState icon={AlertTriangle} title="Não foi possível carregar o panorama" description={error ?? undefined} />
        </Card>
      );
    }
    return <Skeleton className="h-72 w-full rounded-xl" />;
  }

  return (
    <div className={cn("flex flex-col gap-4 transition-opacity", state === "loading" && "opacity-60")}>
      {state === "error" && error && <p className="text-[13px] text-destructive">{error}</p>}
      <KpiRow totals={data.totals} previous={data.previous} runsHref={runsHref} />
      {data.totals.runs === 0 ? (
        <Card>
          <EmptyState
            icon={Table2}
            title="Nenhuma execução no período"
            description={data.include_dry_run ? undefined : "Os testes do Playground (dry_run) estão de fora — marque “Incluir testes” para vê-los."}
          />
        </Card>
      ) : (
        <>
          <TrendCard overview={data} />
          {data.tool_failures.length > 0 && <ToolFailuresCard overview={data} runsHref={runsHref} />}
          <AgentsCard overview={data} runsHref={runsHref} />
        </>
      )}
    </div>
  );
}

// -- KPIs ------------------------------------------------------------------------

function rate(part: number, total: number): number | null {
  return total > 0 ? part / total : null;
}

function formatRate(value: number | null): string {
  if (value == null) return "—";
  return `${(value * 100).toLocaleString("pt-BR", { maximumFractionDigits: value < 0.1 ? 1 : 0 })}%`;
}

/** `bad`: subir é ruim (erro, latência). `neutral`: subir não é bom nem ruim (volume, custo). */
type Polarity = "bad" | "neutral";

function Delta({ current, previous, polarity, kind }: { current: number | null; previous: number | null; polarity: Polarity; kind: "ratio" | "rate" }) {
  if (current == null || previous == null) return null;
  let text: string;
  let direction: number;
  if (kind === "rate") {
    const diff = (current - previous) * 100;
    direction = Math.sign(Math.round(diff * 10));
    text = `${Math.abs(diff).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} p.p.`;
  } else {
    if (previous === 0) return null;
    const change = (current - previous) / previous;
    direction = Math.sign(Math.round(change * 1000));
    text = `${Math.abs(change * 100).toLocaleString("pt-BR", { maximumFractionDigits: 0 })}%`;
  }
  if (direction === 0) return <span className="text-xs text-muted-foreground">igual ao período anterior</span>;
  const Icon = direction > 0 ? ArrowUpRight : ArrowDownRight;
  const tone = polarity === "neutral" ? "text-muted-foreground" : direction > 0 ? "text-destructive" : "text-success";
  return (
    <span className={cn("inline-flex items-center gap-0.5 text-xs font-medium", tone)}>
      <Icon className="size-3.5" aria-hidden />
      {direction > 0 ? "+" : "−"}
      {text}
      <span className="font-normal text-muted-foreground">&nbsp;vs anterior</span>
    </span>
  );
}

/** Cada KPI leva às execuções que explicam o número, no mesmo recorte (período, agentes, testes). */
function KpiRow({ totals, previous, runsHref }: { totals: OverviewTotals; previous: OverviewTotals | null; runsHref: RunsHref }) {
  const failed = totals.errors + totals.interrupted;
  const prevFailed = previous ? previous.errors + previous.interrupted : null;
  const tiles = [
    {
      label: "Execuções",
      value: formatNumber(totals.runs),
      href: runsHref({}),
      goesTo: "ver as execuções do período",
      detail: `${formatNumber(totals.sessions)} conversas`,
      delta: <Delta current={totals.runs} previous={previous?.runs ?? null} polarity="neutral" kind="ratio" />,
    },
    {
      label: "Falharam",
      value: formatRate(rate(failed, totals.runs)),
      href: runsHref({ statuses: ["error", "interrupted"] }),
      goesTo: "ver as execuções com erro ou interrompidas",
      detail: `${formatNumber(totals.errors)} com erro · ${formatNumber(totals.interrupted)} interrompidas`,
      delta: (
        <Delta
          current={rate(failed, totals.runs)}
          previous={previous && prevFailed != null ? rate(prevFailed, previous.runs) : null}
          polarity="bad"
          kind="rate"
        />
      ),
    },
    {
      label: "Responderam com tool falhando",
      value: formatRate(rate(totals.tool_failure_runs, totals.runs)),
      // O número conta só as que terminaram em sucesso: o erro já está em "Falharam".
      href: runsHref({ statuses: ["success"], toolFailed: true }),
      goesTo: "ver as execuções que responderam com alguma tool falhando",
      detail: `${formatNumber(totals.tool_failures)} de ${formatNumber(totals.tool_calls)} chamadas de tool falharam`,
      delta: (
        <Delta
          current={rate(totals.tool_failure_runs, totals.runs)}
          previous={previous ? rate(previous.tool_failure_runs, previous.runs) : null}
          polarity="bad"
          kind="rate"
        />
      ),
    },
    {
      label: "Custo",
      value: formatCost(totals.cost_usd),
      href: runsHref({ sort: "cost" }),
      goesTo: "ver as execuções mais caras primeiro",
      // A coleta de sites entra no total, mas não é de conversa nenhuma.
      detail: [
        totals.sessions
          ? `${formatCost(((totals.cost_usd ?? 0) - (totals.ingestion_cost_usd ?? 0)) / totals.sessions)} por conversa`
          : null,
        totals.ingestion_cost_usd ? `inclui ${formatCost(totals.ingestion_cost_usd)} da coleta de sites` : null,
      ]
        .filter(Boolean)
        .join(" · ") || "—",
      delta: <Delta current={totals.cost_usd} previous={previous?.cost_usd ?? null} polarity="neutral" kind="ratio" />,
    },
    {
      label: "Latência p95",
      value: formatMs(totals.p95_latency_ms),
      href: runsHref({ sort: "latency" }),
      goesTo: "ver as execuções mais lentas primeiro",
      detail: `mediana ${formatMs(totals.p50_latency_ms)}`,
      delta: <Delta current={totals.p95_latency_ms} previous={previous?.p95_latency_ms ?? null} polarity="bad" kind="ratio" />,
    },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
      {tiles.map((tile) => (
        <Link
          key={tile.label}
          href={tile.href}
          aria-label={`${tile.label}: ${tile.value}. Abrir as Execuções para ${tile.goesTo}`}
          title={`Abrir as Execuções para ${tile.goesTo}`}
          className={cn(
            "group relative flex flex-col gap-1 rounded-xl border border-border bg-card p-4 text-card-foreground shadow-xs",
            "outline-none transition-colors hover:border-primary/50 hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring"
          )}
        >
          <span className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
            {tile.label}
            <ArrowRight
              className="size-3.5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100"
              aria-hidden
            />
          </span>
          <span className="text-2xl font-semibold tracking-tight">{tile.value}</span>
          <span className="min-h-4">{tile.delta}</span>
          <span className="text-xs text-muted-foreground">{tile.detail}</span>
        </Link>
      ))}
    </div>
  );
}

// -- Série no tempo --------------------------------------------------------------

type Metric = "runs" | "cost" | "latency";

const METRICS: { value: Metric; label: string; title: string }[] = [
  { value: "runs", label: "Volume", title: "Execuções por resultado" },
  { value: "cost", label: "Custo", title: "Custo (US$)" },
  { value: "latency", label: "Latência p95", title: "Latência p95" },
];

const STATUS_SERIES = [
  { key: "success", label: "Sucesso", color: "var(--viz-good)" },
  { key: "tool_failure_runs", label: "Respondeu com tool falhando", color: "var(--viz-warning)" },
  { key: "errors", label: "Erro ou interrompida", color: "var(--viz-critical)" },
] as const;

/** Empilhadas a partir da base (`STACK_KEYS`): as falhas ficam junto ao eixo, onde poucas ainda se
 * veem. No topo da barra, 42 erros num dia de 640 execuções viravam um fio vermelho que ninguém lia. */
const STACK_ORDER = STACK_KEYS.map((key) => STATUS_SERIES.find((s) => s.key === key)!);

/** A taxa de falha separada do volume: com o volume variando 10× entre um dia e outro, a
 * proporção de falhas não aparece numa pilha absoluta. Duas medidas, dois gráficos — nunca
 * dois eixos no mesmo. O âmbar da linha é mais escuro que o das barras (ver globals.css). */
const RATE_SERIES = [
  { key: "failed", label: "Falharam (erro ou interrompida)", color: "var(--viz-critical)" },
  { key: "tool", label: "Responderam com tool falhando", color: "var(--viz-warning-line)" },
] as const;
// Com menos de FEW_RUNS execuções o ponto sai vazado e o tooltip avisa — na taxa e na latência.

function bucketLabel(start: string, granularity: Overview["granularity"], long = false): string {
  const date = new Date(start);
  const opts: Intl.DateTimeFormatOptions = { timeZone: TIMEZONE };
  if (granularity === "hour") {
    return long
      ? new Intl.DateTimeFormat("pt-BR", { ...opts, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(date)
      : `${new Intl.DateTimeFormat("pt-BR", { ...opts, hour: "2-digit" }).format(date)}h`;
  }
  const day = new Intl.DateTimeFormat("pt-BR", { ...opts, day: "2-digit", month: "short" }).format(date).replace(".", "");
  return granularity === "week" ? `sem. ${day}` : day;
}

const GRANULARITY_LABEL = { hour: "por hora", day: "por dia", week: "por semana" } as const;

function metricValue(bucket: OverviewBucket, metric: Metric): number | null {
  if (metric === "runs") return bucket.runs;
  if (metric === "cost") return bucket.cost_usd;
  return bucket.p95_latency_ms;
}

function formatMetric(value: number | null, metric: Metric): string {
  if (value == null) return "—";
  if (metric === "runs") return formatNumber(value);
  if (metric === "cost") return formatCost(value);
  return formatMs(value);
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** Retângulo com só os cantos de cima arredondados — a ponta do dado; a base fica reta. */
function topRounded(x: number, y: number, w: number, h: number, r: number): string {
  const radius = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + radius}Q${x},${y} ${x + radius},${y}H${x + w - radius}Q${x + w},${y} ${x + w},${y + radius}V${y + h}Z`;
}

function TrendCard({ overview }: { overview: Overview }) {
  const [metric, setMetric] = useState<Metric>("runs");
  const [asTable, setAsTable] = useState(false);
  // Um período em foco para os dois gráficos do volume: passar o mouse num marca o outro.
  const [hovered, setHovered] = useState<number | null>(null);
  const current = METRICS.find((m) => m.value === metric)!;

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">{current.title}</h3>
          <p className="text-xs text-muted-foreground">
            {GRANULARITY_LABEL[overview.granularity]}, horário de Brasília
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div role="tablist" aria-label="Medida do gráfico" className="inline-flex rounded-lg border border-border p-0.5">
            {METRICS.map((m) => (
              <button
                key={m.value}
                role="tab"
                type="button"
                aria-selected={metric === m.value}
                onClick={() => {
                  setMetric(m.value);
                  setHovered(null);
                }}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs transition-colors",
                  metric === m.value ? "bg-accent font-medium text-foreground" : "text-muted-foreground hover:text-foreground"
                )}
              >
                {m.label}
              </button>
            ))}
          </div>
          <Button variant="ghost" size="sm" onClick={() => setAsTable((v) => !v)} aria-pressed={asTable}>
            <Table2 /> {asTable ? "Gráfico" : "Tabela"}
          </Button>
        </div>
      </div>
      {metric === "runs" && !asTable && (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label="Legenda">
          {STATUS_SERIES.map((s) => (
            <li key={s.key} className="inline-flex items-center gap-1.5">
              <span className="size-2.5 rounded-[2px]" style={{ background: s.color }} aria-hidden />
              {s.label}
            </li>
          ))}
        </ul>
      )}
      {asTable ? (
        <TrendTable overview={overview} metric={metric} />
      ) : (
        <>
          <TrendChart overview={overview} metric={metric} hovered={hovered} onHover={setHovered} />
          {metric === "runs" && <RateChart overview={overview} hovered={hovered} onHover={setHovered} />}
        </>
      )}
    </Card>
  );
}

const PLOT_HEIGHT = 168;
const AXIS_LEFT = 52;
const AXIS_BOTTOM = 24;

function TrendChart({
  overview,
  metric,
  hovered,
  onHover: setHovered,
}: {
  overview: Overview;
  metric: Metric;
  hovered: number | null;
  onHover: (index: number | null) => void;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const titleId = useId();
  const buckets = overview.buckets;
  const n = buckets.length;
  const plotWidth = Math.max(width - AXIS_LEFT - 8, 0);
  const band = n ? plotWidth / n : 0;
  const barWidth = Math.max(Math.min(24, band * 0.72), 1);
  const values = buckets.map((b) => metricValue(b, metric));
  const max = niceMax(Math.max(0, ...values.map((v) => v ?? 0)));
  const y = (value: number) => PLOT_HEIGHT - (value / max) * PLOT_HEIGHT;
  const ticks = [0, max / 2, max];
  // Rótulos do eixo X: no máximo ~7, espaçados por igual, sempre com o último.
  const labelEvery = Math.max(1, Math.ceil(n / 7));
  const xCenter = (i: number) => AXIS_LEFT + band * i + band / 2;

  const tooltipBucket = hovered != null ? buckets[hovered] : null;

  return (
    <div ref={ref} className="relative w-full" onPointerLeave={() => setHovered(null)}>
      {width > 0 && (
        <svg width={width} height={PLOT_HEIGHT + AXIS_BOTTOM + 8} role="img" aria-labelledby={titleId} className="overflow-visible">
          <title id={titleId}>{METRICS.find((m) => m.value === metric)?.title}</title>
          <g transform="translate(0,8)">
            {ticks.map((tick) => (
              <g key={tick}>
                <line x1={AXIS_LEFT} x2={width - 8} y1={y(tick)} y2={y(tick)} className="stroke-border" strokeWidth={1} />
                <text x={AXIS_LEFT - 8} y={y(tick)} dy="0.32em" textAnchor="end" className="fill-muted-foreground text-[11px] tabular-nums">
                  {metric === "latency" ? formatMs(tick) : metric === "cost" ? formatCost(tick) : formatNumber(tick)}
                </text>
              </g>
            ))}

            {metric === "runs" &&
              buckets.map((bucket, i) => {
                const x = xCenter(i) - barWidth / 2;
                let base = PLOT_HEIGHT;
                const segments = STACK_ORDER.map((s) => ({ ...s, value: bucket[s.key] })).filter((s) => s.value > 0);
                return (
                  <g key={bucket.start} opacity={hovered == null || hovered === i ? 1 : 0.55}>
                    {segments.map((segment, s) => {
                      const h = (segment.value / max) * PLOT_HEIGHT;
                      // 2px de fundo entre os segmentos: o espaço separa, não um contorno.
                      const top = base - h;
                      const gap = s > 0 ? 2 : 0;
                      const height = Math.max(h - gap, 1);
                      const isTop = s === segments.length - 1;
                      base = top;
                      return isTop ? (
                        <path key={segment.key} d={topRounded(x, top, barWidth, height, 4)} style={{ fill: segment.color }} />
                      ) : (
                        <rect key={segment.key} x={x} y={top} width={barWidth} height={height} style={{ fill: segment.color }} />
                      );
                    })}
                  </g>
                );
              })}

            {metric === "cost" &&
              buckets.map((bucket, i) => {
                if (!bucket.cost_usd) return null;
                const h = Math.max((bucket.cost_usd / max) * PLOT_HEIGHT, 1);
                return (
                  <path
                    key={bucket.start}
                    d={topRounded(xCenter(i) - barWidth / 2, PLOT_HEIGHT - h, barWidth, h, 4)}
                    className="fill-primary"
                    opacity={hovered == null || hovered === i ? 1 : 0.55}
                  />
                );
              })}

            {metric === "latency" && (
              <PointLine
                values={values}
                runs={buckets.map((b) => b.runs)}
                xCenter={xCenter}
                y={y}
                hovered={hovered}
                color="var(--color-primary)"
              />
            )}

            {hovered != null && metric === "latency" && (
              <line x1={xCenter(hovered)} x2={xCenter(hovered)} y1={0} y2={PLOT_HEIGHT} className="stroke-muted-foreground" strokeWidth={1} />
            )}

            <line x1={AXIS_LEFT} x2={width - 8} y1={PLOT_HEIGHT} y2={PLOT_HEIGHT} className="stroke-border" strokeWidth={1} />
            {buckets.map((bucket, i) =>
              i % labelEvery === 0 || i === n - 1 ? (
                (i === n - 1 || n - 1 - i >= labelEvery / 2 || i === 0) && (
                  <text key={bucket.start} x={xCenter(i)} y={PLOT_HEIGHT + 16} textAnchor="middle" className="fill-muted-foreground text-[11px]">
                    {bucketLabel(bucket.start, overview.granularity)}
                  </text>
                )
              ) : null
            )}

            {/* Alvos de hover/foco: a faixa inteira de cada balde, não só a barra pintada. */}
            {buckets.map((bucket, i) => (
              <rect
                key={bucket.start}
                x={AXIS_LEFT + band * i}
                y={0}
                width={band}
                height={PLOT_HEIGHT}
                fill="transparent"
                tabIndex={0}
                aria-label={`${bucketLabel(bucket.start, overview.granularity, true)}: ${formatMetric(values[i], metric)}`}
                onPointerEnter={() => setHovered(i)}
                onFocus={() => setHovered(i)}
                onBlur={() => setHovered(null)}
                className="outline-none"
              />
            ))}
          </g>
        </svg>
      )}
      {tooltipBucket && hovered != null && (
        <TrendTooltip
          bucket={tooltipBucket}
          metric={metric}
          granularity={overview.granularity}
          left={Math.min(Math.max(xCenter(hovered), 124), width - 124)}
        />
      )}
    </div>
  );
}

function PointLine({
  values,
  runs,
  xCenter,
  y,
  hovered,
  color,
  ceiling,
}: {
  values: (number | null)[];
  runs: number[];
  xCenter: (i: number) => number;
  y: (v: number) => number;
  hovered: number | null;
  color: string;
  /** Acima disto o ponto fica preso na borda de cima, como um triângulo ("passou daqui"). */
  ceiling?: number;
}) {
  const clamp = (v: number) => (ceiling != null ? Math.min(v, ceiling) : v);
  // Balde sem execução não tem latência nem taxa: a linha quebra ali, em vez de cair a zero.
  const segments: { i: number; v: number }[][] = [];
  let current: { i: number; v: number }[] = [];
  values.forEach((v, i) => {
    if (v == null) {
      if (current.length) segments.push(current);
      current = [];
    } else {
      current.push({ i, v });
    }
  });
  if (current.length) segments.push(current);
  return (
    <g>
      {segments.map((segment) => (
        <polyline
          key={segment[0].i}
          points={segment.map((p) => `${xCenter(p.i)},${y(clamp(p.v))}`).join(" ")}
          fill="none"
          style={{ stroke: color }}
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      ))}
      {values.map((v, i) =>
        v == null ? null : ceiling != null && v > ceiling ? (
          // Fora da escala (só acontece com poucas execuções, ver RateChart): na borda, vazado.
          <path
            key={i}
            d={`M${xCenter(i) - 5},${y(ceiling) + 4}L${xCenter(i)},${y(ceiling) - 4}L${xCenter(i) + 5},${y(ceiling) + 4}Z`}
            className="fill-card"
            style={{ stroke: color }}
            strokeWidth={2}
            strokeLinejoin="round"
          />
        ) : runs[i] < FEW_RUNS ? (
          // Poucas execuções: vazado, para o valor não ser lido como tendência.
          <circle key={i} cx={xCenter(i)} cy={y(v)} r={hovered === i ? 5 : 4} className="fill-card" style={{ stroke: color }} strokeWidth={2} />
        ) : (
          <circle key={i} cx={xCenter(i)} cy={y(v)} r={hovered === i ? 5 : 4} className="stroke-card" style={{ fill: color }} strokeWidth={2} />
        )
      )}
    </g>
  );
}

function FewRunsNote({ runs }: { runs: number }) {
  if (runs === 0 || runs >= FEW_RUNS) return null;
  return (
    <p className="mt-1 text-muted-foreground">
      Só {formatNumber(runs)} {runs === 1 ? "execução" : "execuções"}: um valor só, não uma tendência.
    </p>
  );
}

const RATE_PLOT_HEIGHT = 112;

/** A segunda pergunta do volume: das execuções de cada período, quantas falharam? Mesmo eixo X e
 * mesma largura do gráfico de cima (os períodos se alinham), eixo Y próprio em %. */
function RateChart({
  overview,
  hovered,
  onHover,
}: {
  overview: Overview;
  hovered: number | null;
  onHover: (index: number | null) => void;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const titleId = useId();
  const buckets = overview.buckets;
  const n = buckets.length;
  const plotWidth = Math.max(width - AXIS_LEFT - 8, 0);
  const band = n ? plotWidth / n : 0;
  const xCenter = (i: number) => AXIS_LEFT + band * i + band / 2;
  const series = RATE_SERIES.map((s) => ({ ...s, values: buckets.map((b) => bucketRate(b, s.key)) }));
  const average = periodFailureRate(overview.totals);
  // A escala ignora os períodos de amostra pequena (ver `rateScale`): o ponto deles acima do teto
  // fica na borda (triângulo vazado), e o valor real está no tooltip e na tabela.
  const { max, clipped } = rateScale(buckets, average);
  const y = (value: number) => RATE_PLOT_HEIGHT - (value / max) * RATE_PLOT_HEIGHT;
  const percent = (value: number) => `${(value * 100).toLocaleString("pt-BR", { maximumFractionDigits: max < 0.1 ? 1 : 0 })}%`;
  const runs = buckets.map((b) => b.runs);

  return (
    <div className="flex flex-col gap-2 border-t border-border pt-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h4 id={titleId} className="text-sm font-medium">
          Taxa de falha
          <span className="ml-1.5 text-xs font-normal text-muted-foreground">das execuções de cada período</span>
        </h4>
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label="Legenda da taxa de falha">
          {RATE_SERIES.map((s) => (
            <li key={s.key} className="inline-flex items-center gap-1.5">
              <span className="h-0.5 w-3 rounded-full" style={{ background: s.color }} aria-hidden />
              {s.label}
            </li>
          ))}
          {average != null && average > 0 && (
            <li className="inline-flex items-center gap-1.5">
              <span className="h-px w-3 bg-muted-foreground/60" aria-hidden />
              média do período: {formatRate(average)} falharam
            </li>
          )}
          <li className="inline-flex items-center gap-1.5">
            <span className="size-2 rounded-full border-2 border-muted-foreground" aria-hidden />
            menos de {FEW_RUNS} execuções
          </li>
          {clipped && (
            <li className="inline-flex items-center gap-1.5">
              <svg width="10" height="9" aria-hidden className="overflow-visible">
                <path d="M0,8L5,0L10,8Z" className="fill-none stroke-muted-foreground" strokeWidth={1.5} strokeLinejoin="round" />
              </svg>
              acima da escala (valor no tooltip)
            </li>
          )}
        </ul>
      </div>
      <div ref={ref} className="relative w-full" onPointerLeave={() => onHover(null)}>
        {width > 0 && (
          <svg width={width} height={RATE_PLOT_HEIGHT + 16} role="img" aria-labelledby={titleId} className="overflow-visible">
            <g transform="translate(0,8)">
              {[0, max / 2, max].map((tick) => (
                <g key={tick}>
                  <line x1={AXIS_LEFT} x2={width - 8} y1={y(tick)} y2={y(tick)} className="stroke-border" strokeWidth={1} />
                  <text x={AXIS_LEFT - 8} y={y(tick)} dy="0.32em" textAnchor="end" className="fill-muted-foreground text-[11px] tabular-nums">
                    {percent(tick)}
                  </text>
                </g>
              ))}
              {average != null && average > 0 && (
                // A média do período: o que um dia "ruim" está acima. Linha cheia e fina; o rótulo fica
                // na legenda — dentro do gráfico ele cobria o ponto que caísse perto dele.
                <line x1={AXIS_LEFT} x2={width - 8} y1={y(average)} y2={y(average)} className="stroke-muted-foreground/60" strokeWidth={1} />
              )}
              {hovered != null && (
                <line x1={xCenter(hovered)} x2={xCenter(hovered)} y1={0} y2={RATE_PLOT_HEIGHT} className="stroke-muted-foreground" strokeWidth={1} />
              )}
              {series.map((s) => (
                <PointLine key={s.key} values={s.values} runs={runs} xCenter={xCenter} y={y} hovered={hovered} color={s.color} ceiling={max} />
              ))}
              {buckets.map((bucket, i) => (
                <rect
                  key={bucket.start}
                  x={AXIS_LEFT + band * i}
                  y={0}
                  width={band}
                  height={RATE_PLOT_HEIGHT}
                  fill="transparent"
                  tabIndex={0}
                  aria-label={
                    bucket.runs
                      ? `${bucketLabel(bucket.start, overview.granularity, true)}: ${formatRate(bucketRate(bucket, "failed"))} falharam, ` +
                        `${formatRate(bucketRate(bucket, "tool"))} com tool falhando, em ${formatNumber(bucket.runs)} execuções`
                      : `${bucketLabel(bucket.start, overview.granularity, true)}: sem execuções`
                  }
                  onPointerEnter={() => onHover(i)}
                  onFocus={() => onHover(i)}
                  onBlur={() => onHover(null)}
                  className="outline-none"
                />
              ))}
            </g>
          </svg>
        )}
      </div>
    </div>
  );
}

function TrendTooltip({
  bucket,
  metric,
  granularity,
  left,
}: {
  bucket: OverviewBucket;
  metric: Metric;
  granularity: Overview["granularity"];
  left: number;
}) {
  return (
    <div
      role="status"
      className="pointer-events-none absolute top-0 z-10 w-60 -translate-x-1/2 rounded-lg border border-border bg-popover px-3 py-2 text-xs shadow-md"
      style={{ left }}
    >
      <p className="mb-1 text-muted-foreground">{bucketLabel(bucket.start, granularity, true)}</p>
      {metric === "runs" ? (
        <>
          <p className="mb-1 text-sm font-semibold">{formatNumber(bucket.runs)} execuções</p>
          {STATUS_SERIES.map((s) => (
            <p key={s.key} className="flex items-center gap-1.5">
              <span className="h-0.5 w-3 rounded-full" style={{ background: s.color }} aria-hidden />
              <span className="font-medium tabular-nums">{formatNumber(bucket[s.key])}</span>
              <span className="truncate text-muted-foreground">{s.label}</span>
            </p>
          ))}
          {bucket.runs > 0 && (
            <p className="mt-1 border-t border-border pt-1 text-muted-foreground">
              <span className="font-medium text-foreground tabular-nums">{formatRate(bucketRate(bucket, "failed"))}</span> falharam ·{" "}
              <span className="font-medium text-foreground tabular-nums">{formatRate(bucketRate(bucket, "tool"))}</span> com tool falhando
            </p>
          )}
          <FewRunsNote runs={bucket.runs} />
        </>
      ) : (
        <>
          <p className="text-sm font-semibold">{formatMetric(metricValue(bucket, metric), metric)}</p>
          <p className="text-muted-foreground">{formatNumber(bucket.runs)} execuções</p>
          {metric === "latency" && <FewRunsNote runs={bucket.runs} />}
        </>
      )}
    </div>
  );
}

function TrendTable({ overview, metric }: { overview: Overview; metric: Metric }) {
  const rows = overview.buckets.filter((b) => b.runs > 0);
  return (
    <div className="max-h-72 overflow-auto rounded-lg border border-border">
      <table className="w-full text-[13px]">
        <thead className="sticky top-0 bg-card text-xs text-muted-foreground">
          <tr className="border-b border-border text-left">
            <th className="px-3 py-2 font-medium">Período</th>
            <th className="px-3 py-2 text-right font-medium">Execuções</th>
            <th className="px-3 py-2 text-right font-medium">Sucesso</th>
            <th className="px-3 py-2 text-right font-medium">Tool falhando</th>
            <th className="px-3 py-2 text-right font-medium">Erro / interrompida</th>
            <th className="px-3 py-2 text-right font-medium">Falharam</th>
            <th className="px-3 py-2 text-right font-medium">Com tool falhando</th>
            <th className="px-3 py-2 text-right font-medium">Custo</th>
            <th className="px-3 py-2 text-right font-medium">p95</th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((b) => (
            <tr key={b.start} className={cn("border-b border-border last:border-0", metric && "")}>
              <td className="px-3 py-1.5">{bucketLabel(b.start, overview.granularity, true)}</td>
              <td className="px-3 py-1.5 text-right">{formatNumber(b.runs)}</td>
              <td className="px-3 py-1.5 text-right">{formatNumber(b.success)}</td>
              <td className="px-3 py-1.5 text-right">{formatNumber(b.tool_failure_runs)}</td>
              <td className="px-3 py-1.5 text-right">{formatNumber(b.errors)}</td>
              <td className="px-3 py-1.5 text-right">{formatRate(bucketRate(b, "failed"))}</td>
              <td className="px-3 py-1.5 text-right">{formatRate(bucketRate(b, "tool"))}</td>
              <td
                className="px-3 py-1.5 text-right"
                title={b.ingestion_cost_usd ? `inclui ${formatCost(b.ingestion_cost_usd)} da coleta de sites` : undefined}
              >
                {formatCost(b.cost_usd)}
              </td>
              <td className="px-3 py-1.5 text-right">{formatMs(b.p95_latency_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="px-3 py-2 text-xs text-muted-foreground">
        Períodos sem execução não aparecem na tabela. As taxas são sobre as execuções de cada período.
      </p>
    </div>
  );
}

// -- Tools falhando --------------------------------------------------------------

function ToolFailuresCard({ overview, runsHref }: { overview: Overview; runsHref: RunsHref }) {
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div>
        <h3 className="flex items-center gap-1.5 text-sm font-medium">
          <Wrench className="size-4 text-muted-foreground" aria-hidden /> Tools falhando
        </h3>
        <p className="text-xs text-muted-foreground">
          O agente seguiu e respondeu, mas a chamada falhou. Cada tipo pede uma ação diferente.
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[13px]">
          <thead className="text-xs text-muted-foreground">
            <tr className="border-b border-border text-left">
              <th className="py-2 pr-3 font-medium">Tool</th>
              <th className="py-2 pr-3 font-medium">Tipo</th>
              <th className="py-2 pr-3 text-right font-medium">Falhas</th>
              <th className="py-2 pr-3 font-medium">Agentes</th>
              <th className="py-2 pr-3 font-medium">Última</th>
              <th className="py-2 font-medium">
                <span className="sr-only">Execuções</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {overview.tool_failures.map((f) => (
              <tr key={`${f.tool_name}:${f.failure}`} className="border-b border-border align-top last:border-0">
                <td className="py-2 pr-3 font-mono text-xs">{f.tool_name}</td>
                <td className="py-2 pr-3">
                  <span className="font-medium">{TOOL_FAILURE_META[f.failure]?.label ?? f.failure}</span>
                  {f.failure === "not_found" && <span className="text-muted-foreground"> · não conta como falha</span>}
                  {f.http_status.length > 0 && <span className="text-muted-foreground"> · HTTP {f.http_status.join(", ")}</span>}
                  <p className="text-xs text-muted-foreground">{TOOL_FAILURE_META[f.failure]?.action}</p>
                </td>
                <td className="whitespace-nowrap py-2 pr-3 text-right tabular-nums">{formatNumber(f.count)}</td>
                <td className="py-2 pr-3 text-xs text-muted-foreground">{f.agent_types.join(", ")}</td>
                <td className="whitespace-nowrap py-2 pr-3 text-xs text-muted-foreground">{formatRelativeTime(f.last_at)}</td>
                <td className="whitespace-nowrap py-2 text-right">
                  {/* A lista não filtra por tool: o recorte é "alguma tool falhou", nos agentes em que esta falhou.
                      `not_found` não conta como falha, então não há lista que o mostre. */}
                  {f.failure !== "not_found" && (
                    <RunsLink
                      href={runsHref({ toolFailed: true, agents: f.agent_types })}
                      title="Execuções em que alguma tool falhou (a lista não recorta por tool)"
                    />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// -- Agentes ---------------------------------------------------------------------

function Sparkline({ values, label }: { values: number[]; label: string }) {
  const width = 88;
  const height = 24;
  if (values.length < 2) return null;
  const max = Math.max(1, ...values);
  const step = width / (values.length - 1);
  const points = values.map((v, i) => `${(i * step).toFixed(1)},${(height - 3 - (v / max) * (height - 6)).toFixed(1)}`);
  const last = points[points.length - 1].split(",");
  return (
    <svg width={width} height={height} role="img" aria-label={label} className="shrink-0 overflow-visible">
      <polyline points={points.join(" ")} fill="none" className="stroke-muted-foreground/60" strokeWidth={1.5} strokeLinejoin="round" />
      <circle cx={last[0]} cy={last[1]} r={2.5} className="fill-primary" />
    </svg>
  );
}

const MIN_RUNS_TO_COMPARE = 10;

function VersionVerdict({ agent }: { agent: OverviewAgent }) {
  const [latest, previous] = agent.versions;
  if (!latest || latest.agent_version == null) return <span className="text-muted-foreground">—</span>;
  if (!previous || previous.agent_version == null) return <Badge variant="outline">v{latest.agent_version}</Badge>;
  if (latest.runs < MIN_RUNS_TO_COMPARE || previous.runs < MIN_RUNS_TO_COMPARE) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <Badge variant="outline">v{latest.agent_version}</Badge>
        <span className="text-xs text-muted-foreground" title={`É preciso ${MIN_RUNS_TO_COMPARE} execuções de cada versão para comparar.`}>
          poucas execuções
        </span>
      </span>
    );
  }
  const worse =
    latest.error_rate + latest.tool_failure_rate - (previous.error_rate + previous.tool_failure_rate);
  const tone = Math.abs(worse) < 0.02 ? "neutral" : worse > 0 ? "worse" : "better";
  return (
    <span className="inline-flex items-center gap-1.5">
      <Badge variant="outline">v{latest.agent_version}</Badge>
      <span
        className={cn(
          "text-xs",
          tone === "worse" ? "text-destructive" : tone === "better" ? "text-success" : "text-muted-foreground"
        )}
      >
        {tone === "worse" ? "▲ mais falhas que a" : tone === "better" ? "▼ menos falhas que a" : "parecida com a"} v
        {previous.agent_version}
      </span>
    </span>
  );
}

function AgentsCard({ overview, runsHref }: { overview: Overview; runsHref: RunsHref }) {
  const [open, setOpen] = useState<string | null>(overview.agents.length === 1 ? overview.agents[0].agent_type : null);
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div>
        <h3 className="text-sm font-medium">Por agente</h3>
        <p className="text-xs text-muted-foreground">
          Abra um agente para comparar as versões da configuração que rodaram no período; a seta leva às execuções.
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1000px] text-[13px]">
          <thead className="text-xs text-muted-foreground">
            <tr className="border-b border-border text-left">
              <th className="py-2 pr-3 font-medium">Agente</th>
              <th className="py-2 pr-3 font-medium">Execuções</th>
              <th className="py-2 pr-3 text-right font-medium">Falharam</th>
              <th className="py-2 pr-3 text-right font-medium">Tool falhando</th>
              <th className="py-2 pr-3 text-right font-medium">Custo</th>
              <th className="py-2 pr-3 text-right font-medium">Por conversa</th>
              <th className="py-2 pr-3 text-right font-medium">p95</th>
              <th className="py-2 pr-3 text-right font-medium">Feedback</th>
              <th className="py-2 pr-3 font-medium">Versão atual</th>
              <th className="py-2 font-medium">
                <span className="sr-only">Execuções</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {overview.agents.map((agent) => {
              const t = agent.totals;
              const expanded = open === agent.agent_type;
              return (
                <Fragment key={agent.agent_type}>
                  <tr className="border-b border-border last:border-0 hover:bg-accent/40">
                    <td className="py-2 pr-3">
                      <button
                        type="button"
                        onClick={() => setOpen(expanded ? null : agent.agent_type)}
                        aria-expanded={expanded}
                        className="flex items-center gap-2 text-left"
                      >
                        {expanded ? <ChevronDown className="size-4 text-muted-foreground" /> : <ChevronRight className="size-4 text-muted-foreground" />}
                        <AgentAvatar agentType={agent.agent_type} name={agent.agent_name ?? agent.agent_type} size="sm" />
                        <span className="min-w-0">
                          <span className="block max-w-56 truncate font-medium" title={agent.agent_name ?? undefined}>
                            {agent.agent_name ?? agent.agent_type}
                          </span>
                          <span className="block font-mono text-[11px] text-muted-foreground">{agent.agent_type}</span>
                        </span>
                      </button>
                    </td>
                    <td className="py-2 pr-3">
                      <span className="flex items-center gap-2">
                        <span className="w-10 tabular-nums">{formatNumber(t.runs)}</span>
                        <Sparkline values={agent.trend} label={`Execuções de ${agent.agent_name ?? agent.agent_type} no período`} />
                      </span>
                    </td>
                    <td className="whitespace-nowrap py-2 pr-3 text-right tabular-nums">{formatRate(rate(t.errors + t.interrupted, t.runs))}</td>
                    <td className="whitespace-nowrap py-2 pr-3 text-right tabular-nums">{formatRate(rate(t.tool_failure_runs, t.runs))}</td>
                    <td className="whitespace-nowrap py-2 pr-3 text-right tabular-nums">{formatCost(t.cost_usd)}</td>
                    <td className="whitespace-nowrap py-2 pr-3 text-right tabular-nums">{formatCost(agent.cost_per_session_usd)}</td>
                    <td className="whitespace-nowrap py-2 pr-3 text-right tabular-nums">{formatMs(t.p95_latency_ms)}</td>
                    <td className="whitespace-nowrap py-2 pr-3 text-right text-xs tabular-nums">
                      {t.feedback_up + t.feedback_down ? `👍 ${t.feedback_up} · 👎 ${t.feedback_down}` : "—"}
                    </td>
                    <td className="whitespace-nowrap py-2 pr-3">
                      <VersionVerdict agent={agent} />
                    </td>
                    <td className="whitespace-nowrap py-2 text-right">
                      <RunsLink href={runsHref({ agents: [agent.agent_type] })} title={`Execuções de ${agent.agent_name ?? agent.agent_type}`} />
                    </td>
                  </tr>
                  {expanded && (
                    <tr className="border-b border-border bg-muted/30">
                      <td colSpan={10} className="px-3 py-3">
                        <VersionsTable agent={agent} runsHref={runsHref} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function VersionsTable({ agent, runsHref }: { agent: OverviewAgent; runsHref: RunsHref }) {
  return (
    <table className="w-full text-xs">
      <thead className="text-muted-foreground">
        <tr className="text-left">
          <th className="py-1 pr-3 font-medium">Versão</th>
          <th className="py-1 pr-3 text-right font-medium">Execuções</th>
          <th className="py-1 pr-3 text-right font-medium">Falharam</th>
          <th className="py-1 pr-3 text-right font-medium">Tool falhando</th>
          <th className="py-1 pr-3 text-right font-medium">Custo / execução</th>
          <th className="py-1 pr-3 text-right font-medium">p95</th>
          <th className="py-1 pr-3 text-right font-medium">Feedback</th>
          <th className="py-1 font-medium">Rodou</th>
        </tr>
      </thead>
      <tbody className="tabular-nums">
        {agent.versions.map((v) => (
          <tr key={String(v.agent_version)}>
            <td className="py-1 pr-3">
              {v.agent_version != null ? (
                <Link
                  href={runsHref({ agents: [agent.agent_type], version: v.agent_version })}
                  className="font-medium underline-offset-2 hover:underline"
                  title={`Execuções da v${v.agent_version}`}
                >
                  v{v.agent_version}
                </Link>
              ) : (
                "sem versão"
              )}
            </td>
            <td className="py-1 pr-3 text-right">{formatNumber(v.runs)}</td>
            <td className="py-1 pr-3 text-right">{formatRate(v.error_rate)}</td>
            <td className="py-1 pr-3 text-right">{formatRate(v.tool_failure_rate)}</td>
            <td className="py-1 pr-3 text-right">{formatCost(v.cost_per_run_usd)}</td>
            <td className="py-1 pr-3 text-right">{formatMs(v.p95_latency_ms)}</td>
            <td className="py-1 pr-3 text-right">{v.feedback_up + v.feedback_down ? `👍 ${v.feedback_up} · 👎 ${v.feedback_down}` : "—"}</td>
            <td className="py-1 text-muted-foreground">
              {formatRelativeTime(v.first_seen)} → {formatRelativeTime(v.last_seen)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RunsLink({ href, title }: { href: string; title: string }) {
  return (
    <Link
      href={href}
      title={title}
      aria-label={title}
      className="inline-flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
    >
      <ArrowRight className="size-4" aria-hidden />
    </Link>
  );
}
