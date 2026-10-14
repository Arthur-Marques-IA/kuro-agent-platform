"use client";

import { TOOL_FAILURE_META, toolFailureLabel } from "@/lib/tool-failures";
import type { ToolFailureKind } from "@/lib/types";
import { type ReactNode, useCallback, useEffect, useMemo, useState, Fragment } from "react";
import Link from "next/link";
import {
  Activity,
  Bot,
  Box,
  Braces,
  Database,
  MessageSquare,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { ApiError, errorMessage, requestJson } from "@/lib/http";
import { formatCost, formatDateTime, formatMs, formatNumber } from "@/lib/format";
import type { RetrievalReferences, RunTrace, TraceScore, TraceSpan } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { CodeBlock } from "@/components/ui/code-block";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { EmptyState, RelativeTime, SectionHeading, Skeleton, Spinner } from "@/components/ui/primitives";
import { AgentAvatar } from "@/components/agents/agent-avatar";
import { ChunkList } from "@/components/knowledge/chunk-list";
import { FeedbackCount, RunStatusBadge } from "@/components/observability/run-status";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";

/** A gravação é assíncrona: um run recém-terminado pode levar um instante para aparecer. */
const INDEXING_RETRIES = 10;
const INDEXING_INTERVAL_MS = 3000;

type LoadState =
  | { kind: "loading" }
  | { kind: "indexing"; attempt: number }
  | { kind: "not-found" }
  | { kind: "error"; message: string }
  | { kind: "ready"; trace: RunTrace };

export function RunTraceView({ runId }: { runId: string }) {
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  const load = useCallback(
    async (attempt: number) => {
      try {
        const trace = await requestJson<RunTrace>(`/api/observability/runs/${encodeURIComponent(runId)}/trace`, {
          fallbackError: "Falha ao carregar o trace",
        });
        setState({ kind: "ready", trace });
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          setState(attempt < INDEXING_RETRIES ? { kind: "indexing", attempt } : { kind: "not-found" });
        } else {
          setState({ kind: "error", message: errorMessage(err) });
        }
      }
    },
    [runId]
  );

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(0);
  }, [load]);

  useEffect(() => {
    if (state.kind !== "indexing") return;
    const timer = setTimeout(() => void load(state.attempt + 1), INDEXING_INTERVAL_MS);
    return () => clearTimeout(timer);
  }, [state, load]);

  if (state.kind === "ready") return <TraceDetail trace={state.trace} onReload={() => void load(INDEXING_RETRIES)} />;

  return (
    <>
      <PageHeader breadcrumbs={[{ label: "Agentes", href: "/agents" }, { label: "Execução" }]} title="Execução" />
      <PageBody>
        {state.kind === "loading" ? (
          <div className="flex flex-col gap-4" aria-busy="true" aria-label="Carregando trace">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              {[1, 2, 3, 4].map((i) => (
                <Skeleton key={i} className="h-16 rounded-xl" />
              ))}
            </div>
            <Skeleton className="h-72 rounded-xl" />
          </div>
        ) : (
          <Card>
            {state.kind === "indexing" ? (
              <EmptyState
                icon={Activity}
                title="Indexando o trace…"
                description="A execução acabou de terminar; os spans aparecem em alguns segundos."
                action={<Spinner className="text-muted-foreground" />}
              />
            ) : (
              <EmptyState
                icon={Activity}
                title={state.kind === "not-found" ? "Trace não encontrado" : "Não foi possível carregar o trace"}
                description={
                  state.kind === "not-found"
                    ? "O run não existe, não foi rastreado (observabilidade desligada na época) ou ainda está sendo indexado."
                    : state.message
                }
                action={
                  <Button variant="outline" size="sm" onClick={() => void load(INDEXING_RETRIES)}>
                    <RefreshCw /> Tentar de novo
                  </Button>
                }
              />
            )}
          </Card>
        )}
      </PageBody>
    </>
  );
}

function TraceDetail({ trace, onReload }: { trace: RunTrace; onReload: () => void }) {
  const { userId, getAgent } = useWorkspace();
  const { run, spans, scores } = trace;
  const rows = useMemo(() => flattenTree(spans), [spans]);
  const [selectedId, setSelectedId] = useState<string | null>(rows[0]?.span.id ?? null);
  const selected = spans.find((s) => s.id === selectedId) ?? rows[0]?.span;
  const agentHref = `/agents/${encodeURIComponent(run.agent_type)}`;
  const agentLabel = run.agent_name ?? run.agent_type;

  return (
    <>
      <PageHeader
        breadcrumbs={[
          { label: "Agentes", href: "/agents" },
          { label: agentLabel, href: `${agentHref}?tab=runs` },
          { label: "Execução" },
        ]}
        title={
          <span className="flex min-w-0 items-center gap-3">
            <AgentAvatar agentType={run.agent_type} name={agentLabel} size="sm" />
            <span className="truncate">{run.message || "Execução"}</span>
          </span>
        }
        description={
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <RunStatusBadge status={run.status} title={run.status_message} />
            {!!run.tool_failures && (
              <Badge variant="warning" title="O agente seguiu, mas estas chamadas de tool falharam — veja as spans abaixo.">
                {run.tool_failures} {run.tool_failures === 1 ? "tool falhou" : "tools falharam"}
              </Badge>
            )}
            {run.agent_version != null && (
              <Badge variant="outline" title={`config_hash ${run.config_hash ?? "—"}`}>
                Config v{run.agent_version}
              </Badge>
            )}
            {run.prompt_version != null && <Badge variant="outline">Prompt v{run.prompt_version}</Badge>}
            {run.model && <Badge variant="outline">{run.model}</Badge>}
            {run.endpoint && <Badge variant="outline">{run.endpoint}</Badge>}
            <span className="inline-flex items-center rounded-md bg-muted pl-2 font-mono text-xs text-foreground/80">
              {run.run_id}
              <CopyButton value={run.run_id} label="Copiar run_id" className="size-6" />
            </span>
            <span className="ml-1 text-xs" title={formatDateTime(run.started_at)}>
              <RelativeTime date={run.started_at} />
            </span>
          </div>
        }
        actions={
          <>
            {run.session_id && (
              <Link
                href={`/logs/sessions/${encodeURIComponent(run.session_id)}`}
                className={buttonVariants({ variant: "outline" })}
                title="Todas as execuções desta sessão, com gráficos"
              >
                <Activity />
                <span className="hidden sm:inline">Ver sessão</span>
              </Link>
            )}
            {run.session_id && run.user_id === userId && getAgent(run.agent_type)?.kind === "conversational" && (
              <Link
                href={`/chat/${encodeURIComponent(run.session_id)}`}
                className={buttonVariants({ variant: "ghost", size: "icon" })}
                title="Abrir conversa (só existe se a sessão ainda estiver no console)"
                aria-label="Abrir conversa"
              >
                <MessageSquare />
              </Link>
            )}
            <Button variant="outline" size="icon" onClick={onReload} aria-label="Atualizar trace" title="Atualizar">
              <RefreshCw />
            </Button>
          </>
        }
      />

      <PageBody className="flex flex-col gap-6">
        {run.status !== "success" && run.status_message && (
          <div
            role="alert"
            className={cn(
              "rounded-xl border px-4 py-3 text-sm",
              run.status === "error"
                ? "border-destructive/30 bg-destructive/5 text-destructive"
                : "border-warning/30 bg-warning/5 text-warning"
            )}
          >
            {run.status_message}
          </div>
        )}

        <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat label="Latência" value={formatMs(run.latency_ms)} />
          <Stat
            label="Tokens"
            value={run.total_tokens ? formatNumber(run.total_tokens) : "—"}
            hint={run.total_tokens ? `${formatNumber(run.input_tokens)} entrada · ${formatNumber(run.output_tokens)} saída` : undefined}
          />
          <Stat label="Custo" value={formatCost(run.cost_usd)} />
          <Stat
            label="Feedback"
            value={<FeedbackCount up={run.feedback_up} down={run.feedback_down} />}
            hint={run.user_id ? `usuário ${run.user_id}` : undefined}
          />
        </dl>

        <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
          <Card className="overflow-hidden">
            <div className="flex h-10 items-center justify-between border-b border-border px-4">
              <h2 className="text-sm font-semibold">Spans</h2>
              <span className="text-xs text-muted-foreground">{spans.length}</span>
            </div>
            <Waterfall rows={rows} run={trace.run} selectedId={selected?.id ?? null} onSelect={setSelectedId} />
          </Card>
          {selected && <SpanDetail key={selected.id} span={selected} />}
        </div>

        {(Object.keys(run.metadata ?? {}).length > 0 || trace.reference) && (
          <div className="grid items-start gap-4 lg:grid-cols-2">
            {Object.keys(run.metadata ?? {}).length > 0 && (
              <Card className="flex flex-col gap-2 p-4">
                <h2 className="text-sm font-semibold">Metadata de quem chamou</h2>
                <dl className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-x-3 gap-y-1 font-mono text-[12px]">
                  {Object.entries(run.metadata ?? {}).map(([key, value]) => (
                    <Fragment key={key}>
                      <dt className="truncate text-muted-foreground">{key}</dt>
                      <dd className="truncate">{value}</dd>
                    </Fragment>
                  ))}
                </dl>
              </Card>
            )}
            {trace.reference && (
              <CodeBlock title="referência (shadow) — a decisão que deveria ter saído" code={JSON.stringify(trace.reference, null, 2)} />
            )}
          </div>
        )}

        {scores.length > 0 && <Scores scores={scores} />}
      </PageBody>
    </>
  );
}

function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <Card className="flex flex-col gap-0.5 px-4 py-3">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums">{value}</dd>
      {hint && <dd className="truncate text-xs text-muted-foreground">{hint}</dd>}
    </Card>
  );
}

// -- árvore de spans -------------------------------------------------------

interface SpanRow {
  span: TraceSpan;
  depth: number;
}

/** Ordem de leitura: cada span seguido dos filhos, por horário de início. */
function flattenTree(spans: TraceSpan[]): SpanRow[] {
  const ids = new Set(spans.map((s) => s.id));
  const children = new Map<string | null, TraceSpan[]>();
  for (const span of spans) {
    const parent = span.parent_id && ids.has(span.parent_id) ? span.parent_id : null;
    children.set(parent, [...(children.get(parent) ?? []), span]);
  }
  const rows: SpanRow[] = [];
  const visit = (parent: string | null, depth: number) => {
    const items = [...(children.get(parent) ?? [])].sort((a, b) => a.started_at.localeCompare(b.started_at));
    for (const span of items) {
      rows.push({ span, depth });
      visit(span.id, depth + 1);
    }
  };
  visit(null, 0);
  return rows;
}

const SPAN_TYPES: Record<string, { icon: LucideIcon; label: string; bar: string }> = {
  AGENT: { icon: Bot, label: "Agente", bar: "bg-primary" },
  GENERATION: { icon: Sparkles, label: "Modelo", bar: "bg-success" },
  TOOL: { icon: Wrench, label: "Tool", bar: "bg-warning" },
  RETRIEVER: { icon: Search, label: "Busca", bar: "bg-warning" },
  EMBEDDING: { icon: Database, label: "Embedding", bar: "bg-muted-foreground" },
  GUARDRAIL: { icon: ShieldCheck, label: "Guardrail", bar: "bg-muted-foreground" },
  CHAIN: { icon: Braces, label: "Cadeia", bar: "bg-muted-foreground" },
};
const DEFAULT_SPAN_TYPE = { icon: Box, label: "Span", bar: "bg-muted-foreground" };

function spanType(type: string) {
  return SPAN_TYPES[type] ?? { ...DEFAULT_SPAN_TYPE, label: type.charAt(0) + type.slice(1).toLowerCase() };
}

function Waterfall({
  rows,
  run,
  selectedId,
  onSelect,
}: {
  rows: SpanRow[];
  run: RunTrace["run"];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const start = new Date(run.started_at).getTime();
  const total = Math.max(
    run.latency_ms ?? 0,
    ...rows.map(({ span }) => new Date(span.started_at).getTime() - start + (span.latency_ms ?? 0)),
    1
  );

  return (
    <ul className="flex flex-col py-1" aria-label="Spans da execução">
      {rows.map(({ span, depth }) => {
        const { icon: Icon, label, bar } = spanType(span.type);
        const offset = Math.min(Math.max(((new Date(span.started_at).getTime() - start) / total) * 100, 0), 100);
        const width = Math.max(((span.latency_ms ?? 0) / total) * 100, 0.5);
        const active = span.id === selectedId;
        const failed = span.level === "ERROR";
        return (
          <li key={span.id}>
            <button
              type="button"
              onClick={() => onSelect(span.id)}
              aria-current={active || undefined}
              className={cn(
                "flex w-full flex-col gap-1.5 px-4 py-2 text-left transition-colors hover:bg-accent/50",
                active && "bg-accent"
              )}
            >
              <span className="flex w-full min-w-0 items-center gap-2" style={{ paddingLeft: depth * 16 }}>
                <Icon className={cn("size-3.5 shrink-0", failed ? "text-destructive" : "text-muted-foreground")} aria-label={label} />
                <span className={cn("min-w-0 flex-1 truncate text-[13px]", failed && "text-destructive")}>{span.name}</span>
                {span.total_tokens > 0 && (
                  <span className="hidden shrink-0 text-[11px] tabular-nums text-muted-foreground sm:inline">
                    {formatNumber(span.total_tokens)} tok
                  </span>
                )}
                <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{formatMs(span.latency_ms)}</span>
              </span>
              <span className="relative block h-1.5 w-full rounded-full bg-muted" aria-hidden>
                <span
                  className={cn("absolute inset-y-0 rounded-full", failed ? "bg-destructive" : bar)}
                  style={{ left: `${offset}%`, width: `${Math.min(width, 100 - offset)}%` }}
                />
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

// -- detalhe do span -------------------------------------------------------

function SpanDetail({ span }: { span: TraceSpan }) {
  const references = asReferences(span.metadata?.references);
  const { icon: Icon, label } = references ? { icon: Search, label: "Busca" } : spanType(span.type);
  // As referências têm seção própria; nos metadados elas seriam um JSON ilegível.
  const metadata = Object.entries(span.metadata).filter(([key]) => !(references && key === "references"));

  return (
    <Card className="flex min-w-0 flex-col">
      <div className="flex flex-col gap-2 border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          <h2 className="min-w-0 flex-1 truncate text-sm font-semibold">{span.name}</h2>
          <Badge variant="secondary">{label}</Badge>
          {span.level === "ERROR" && (
            <Badge variant="destructive" title={typeof span.metadata?.failure === "string" ? TOOL_FAILURE_META[span.metadata.failure as ToolFailureKind]?.action : undefined}>
              {typeof span.metadata?.failure === "string"
                ? `${toolFailureLabel(span.metadata.failure)}${span.metadata.http_status ? ` · HTTP ${span.metadata.http_status}` : ""}`
                : "Erro"}
            </Badge>
          )}
          {span.level === "WARNING" && (
            <Badge variant="warning" title={typeof span.metadata?.failure === "string" ? TOOL_FAILURE_META[span.metadata.failure as ToolFailureKind]?.action : undefined}>
              {typeof span.metadata?.failure === "string"
                ? `${toolFailureLabel(span.metadata.failure)}${span.metadata.http_status ? ` · HTTP ${span.metadata.http_status}` : ""}`
                : "Aviso"}
            </Badge>
          )}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>{formatMs(span.latency_ms)}</span>
          {span.model && <span>{span.model}</span>}
          {span.total_tokens > 0 && (
            <span>
              {formatNumber(span.input_tokens)} → {formatNumber(span.output_tokens)} tokens
            </span>
          )}
          {span.cost_usd != null && <span>{formatCost(span.cost_usd)}</span>}
        </div>
        {span.status_message && <p className="text-xs text-destructive">{span.status_message}</p>}
      </div>
      <div className="flex flex-col gap-4 p-4">
        <IoSection title="Entrada" value={span.input} />
        {references ? <ReferencesSection references={references} output={span.output} /> : <IoSection title="Saída" value={span.output} />}
        {metadata.length > 0 && (
          <section className="flex flex-col gap-2">
            <h3 className="text-xs font-medium text-muted-foreground">Metadados</h3>
            <dl className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-x-3 gap-y-1 rounded-lg border border-border p-3 font-mono text-[11.5px]">
              {metadata.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="truncate text-muted-foreground" title={key}>
                    {key}
                  </dt>
                  <dd className="break-all">{typeof value === "string" ? value : JSON.stringify(value)}</dd>
                </div>
              ))}
            </dl>
          </section>
        )}
      </div>
    </Card>
  );
}

function asReferences(value: unknown): RetrievalReferences | null {
  const refs = value as RetrievalReferences | null | undefined;
  return refs && typeof refs === "object" && Array.isArray(refs.results) ? refs : null;
}

function ReferencesSection({ references, output }: { references: RetrievalReferences; output: unknown }) {
  const [raw, setRaw] = useState(false);
  const { results } = references;
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-medium text-muted-foreground">
          Trechos consultados · {results.length} {results.length === 1 ? "trecho" : "trechos"} de{" "}
          <Link href={`/knowledge?collection=${encodeURIComponent(references.collection)}`} className="font-mono hover:underline">
            {references.collection}
          </Link>
        </h3>
        <button type="button" onClick={() => setRaw((v) => !v)} className="text-[11px] text-muted-foreground hover:text-foreground">
          {raw ? "Ver trechos" : "Ver texto enviado ao modelo"}
        </button>
      </div>
      {raw ? (
        <IoSection title="Saída" value={output} />
      ) : results.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-3 py-4 text-center text-[13px] text-muted-foreground">
          A busca não encontrou nada — o agente respondeu sem trechos da base.
        </p>
      ) : (
        <ChunkList chunks={results} label="Trechos consultados" />
      )}
    </section>
  );
}

interface ChatLikeMessage {
  role: string;
  content: unknown;
}

function asMessages(value: unknown): ChatLikeMessage[] | null {
  const messages = (value as { messages?: unknown } | null)?.messages;
  if (!Array.isArray(messages) || messages.length === 0) return null;
  return messages.every((m) => m && typeof m === "object" && typeof (m as ChatLikeMessage).role === "string")
    ? (messages as ChatLikeMessage[])
    : null;
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function IoSection({ title, value }: { title: string; value: unknown }) {
  if (value == null || value === "") return null;
  const messages = asMessages(value);
  const text = asText(value);

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
        <CopyButton value={text} label={`Copiar ${title.toLowerCase()}`} className="size-6" />
      </div>
      {messages ? (
        <ol className="flex flex-col gap-2">
          {messages.map((message, i) => (
            <li key={i} className="rounded-lg border border-border">
              <p className="border-b border-border px-3 py-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                {message.role}
              </p>
              <pre className="scrollbar-thin max-h-72 overflow-auto whitespace-pre-wrap break-words px-3 py-2 font-mono text-[12px] leading-relaxed">
                {asText(message.content ?? "")}
              </pre>
            </li>
          ))}
        </ol>
      ) : (
        <pre className="scrollbar-thin max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-surface px-3 py-2 font-mono text-[12px] leading-relaxed">
          {text}
        </pre>
      )}
    </section>
  );
}

function Scores({ scores }: { scores: TraceScore[] }) {
  return (
    <section className="flex flex-col gap-3">
      <SectionHeading title="Avaliações" description="Feedback de usuários e notas registradas via POST /observability/scores." />
      <Card className="divide-y divide-border overflow-hidden">
        {scores.map((score) => (
          <div key={score.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-sm">
            <span className="font-medium">{score.name}</span>
            <ScoreValue score={score} />
            {score.comment && <span className="min-w-0 flex-1 truncate text-muted-foreground">“{score.comment}”</span>}
            <span className="ml-auto text-xs text-muted-foreground">
              {score.user_id ?? score.source}
              {" · "}
              <RelativeTime date={score.timestamp} />
            </span>
          </div>
        ))}
      </Card>
    </section>
  );
}

function ScoreValue({ score }: { score: TraceScore }) {
  if (score.data_type === "BOOLEAN") {
    return score.value ? <Badge variant="success">positivo</Badge> : <Badge variant="destructive">negativo</Badge>;
  }
  return <Badge variant="outline">{String(score.value)}</Badge>;
}
