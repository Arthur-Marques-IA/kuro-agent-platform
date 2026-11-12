"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ChevronRight, FileText, Layers, Wrench } from "lucide-react";
import { cn } from "@/lib/cn";
import { formatDateTime, formatMs } from "@/lib/format";
import { requestJson } from "@/lib/http";
import {
  type ShownEvent,
  blockDiff,
  previousOutputs,
  roleLabel,
  shorten,
  speechOutsideEvents,
  stepBadges,
  stepLine,
  visibleEvents,
} from "@/lib/timeline";
import type {
  AgentKind,
  ChatMessage,
  RunSummary,
  RunTrace,
  TimelineContextBlock,
  TimelineStep,
  TimelineTurn,
} from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { CodeBlock } from "@/components/ui/code-block";
import { Spinner } from "@/components/ui/primitives";
import { AttachmentChips, MessageBubble } from "@/components/chat/message-bubble";
import { MessageContent } from "@/components/chat/message-content";
import { AnalysisIO } from "@/components/observability/run-insights";
import { RunStatusBadge } from "@/components/observability/run-status";
import { useWorkspace } from "@/components/workspace/workspace-provider";

const timeFormatter = new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" });

export function formatTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : timeFormatter.format(date);
}

export function canTeach(kind: AgentKind | undefined): boolean {
  return kind === "conversational" || kind === "procedural";
}

/** Saída de analista é JSON: vai como bloco ```json para o markdown formatar. */
function asMarkdown(output: string): string {
  const trimmed = output.trim();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      return "```json\n" + JSON.stringify(JSON.parse(trimmed), null, 2) + "\n```";
    } catch {
      // não era JSON de verdade — mostra como veio
    }
  }
  return output;
}

/**
 * A conversa da sessão, um turno por run: os eventos (contato, atendente humano, o que
 * foi enviado em nome do agente, sistema), a fala, uma linha por tool e a resposta do
 * agente. Clicar num turno (ou rolar até ele) o coloca em foco — é o turno cujo contexto
 * o painel lateral mostra.
 */
export function SessionConversation({
  turns,
  sessionId,
  focusId,
  onFocus,
  onShowContext,
}: {
  turns: TimelineTurn[];
  sessionId: string;
  focusId: string | null;
  onFocus: (runId: string) => void;
  /** Abre o contexto do turno (a gaveta, em tela estreita). */
  onShowContext: (runId: string) => void;
}) {
  const { getAgent } = useWorkspace();
  const isAnalysis = (turn: TimelineTurn) => getAgent(turn.agent_type)?.kind === "analysis";
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const previous = useMemo(() => previousOutputs(turns, isAnalysis), [turns, getAgent]);
  const refs = useRef(new Map<string, HTMLElement>());

  // O painel acompanha a leitura: o turno que cruza o meio da tela fica em foco.
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        const hit = entries.find((e) => e.isIntersecting);
        const id = hit?.target.getAttribute("data-run-id");
        if (id) onFocus(id);
      },
      { rootMargin: "-45% 0px -45% 0px" }
    );
    refs.current.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [turns, onFocus]);

  return (
    <ol className="flex flex-col gap-2">
      {turns.map((turn, index) => (
        <li
          key={turn.run_id}
          data-run-id={turn.run_id}
          ref={(el) => {
            if (el) refs.current.set(turn.run_id, el);
            else refs.current.delete(turn.run_id);
          }}
          onClick={() => onFocus(turn.run_id)}
          className={cn(
            "rounded-xl border-l-2 px-3 py-3 transition-colors sm:px-4",
            focusId === turn.run_id ? "border-primary bg-accent/30" : "border-transparent"
          )}
        >
          <TurnHeader
            turn={turn}
            number={index + 1}
            onShowContext={() => {
              onFocus(turn.run_id);
              onShowContext(turn.run_id);
            }}
          />
          {isAnalysis(turn) ? (
            <AnalysisTurn turn={turn} />
          ) : (
            <Turn turn={turn} previousOutput={previous[index]} sessionId={sessionId} />
          )}
        </li>
      ))}
    </ol>
  );
}

function TurnHeader({ turn, number, onShowContext }: { turn: TimelineTurn; number: number; onShowContext: () => void }) {
  const changed = turn.context.filter((b) => b.changed).length;
  return (
    <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <span className="font-medium text-foreground/80">Turno {number}</span>
      <span title={formatDateTime(turn.at)}>{formatTime(turn.at)}</span>
      {turn.dry_run && <Badge variant="outline">teste</Badge>}
      {turn.status !== "success" && <RunStatusBadge status={turn.status} title={turn.status_message} />}
      {turn.context.length > 0 && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onShowContext();
          }}
          className="ml-auto inline-flex min-h-10 items-center gap-1.5 rounded-md px-2 hover:bg-muted hover:text-foreground"
        >
          <Layers className="size-3.5" />
          Contexto
          {changed > 0 && (
            <Badge variant="default">
              {changed} {changed === 1 ? "mudou" : "mudaram"}
            </Badge>
          )}
        </button>
      )}
    </div>
  );
}

function Turn({ turn, previousOutput, sessionId }: { turn: TimelineTurn; previousOutput: string | null; sessionId: string }) {
  const { getAgent } = useWorkspace();
  const agent = getAgent(turn.agent_type);
  const shown = turn.structured ? visibleEvents(turn, previousOutput) : [];
  const speech = speechOutsideEvents(turn);
  const message: ChatMessage = {
    id: `${turn.run_id}:a`,
    role: "assistant",
    content: turn.output ? asMarkdown(turn.output) : "",
    error: turn.status === "success" ? undefined : turn.status_message || turn.status,
    runId: turn.run_id,
    sources: turn.sources,
    usage: {
      input_tokens: turn.usage.input_tokens,
      output_tokens: turn.usage.output_tokens,
      total_tokens: turn.usage.total_tokens,
      duration: turn.usage.latency_ms != null ? turn.usage.latency_ms / 1000 : undefined,
    },
  };

  return (
    <div className="flex flex-col gap-3">
      {!turn.structured && <UnstructuredInput text={turn.raw_input ?? ""} />}
      {shown.map((item) => (
        <EventBubble key={item.index} item={item} fallbackAt={turn.at} agentName={agent?.name ?? turn.agent_type} />
      ))}
      {speech && (
        <EventBubble
          item={{ event: { role: "contact", content: speech, at: turn.at, kind: "text" }, index: -1 }}
          fallbackAt={turn.at}
          agentName={agent?.name ?? turn.agent_type}
        />
      )}
      {turn.steps.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Tools chamadas neste turno">
          {turn.steps.map((step, i) => (
            <StepLine key={i} step={step} index={i} runId={turn.run_id} />
          ))}
        </ul>
      )}
      <div className="flex flex-col gap-0.5">
        <p className="pl-11 text-[11px] text-muted-foreground">{formatTime(turn.at)}</p>
        <MessageBubble
          message={message}
          agentType={turn.agent_type}
          agentName={agent?.name ?? turn.agent_type}
          sessionId={sessionId}
          canTeach={canTeach(agent?.kind)}
        />
      </div>
    </div>
  );
}

/** Run anterior à entrada estruturada (ou chamada só com `message`): o texto inteiro, recolhido. */
function UnstructuredInput({ text }: { text: string }) {
  return (
    <details className="group rounded-lg border border-dashed border-border text-xs" onClick={(e) => e.stopPropagation()}>
      <summary className="flex min-h-10 cursor-pointer list-none items-center gap-2 px-3 py-1.5 text-muted-foreground">
        <ChevronRight className="size-3.5 shrink-0 transition-transform group-open:rotate-90" aria-hidden />
        <span className="font-medium text-foreground/80">Entrada não estruturada</span>
        <span className="ml-auto">{text.length.toLocaleString("pt-BR")} caracteres</span>
      </summary>
      <div className="border-t border-border p-3">
        <p className="mb-2 text-muted-foreground">
          A integração ainda manda tudo em <code className="font-mono">message</code>: fala, histórico e contexto chegam num
          texto só, e o console não os separa. Com <code className="font-mono">events</code> e{" "}
          <code className="font-mono">context</code> (veja docs/integracao.md) cada um aparece no seu lugar.
        </p>
        <pre className="scrollbar-thin max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed">
          {text}
        </pre>
      </div>
    </details>
  );
}

const SIDE: Record<string, "right" | "left" | "center"> = {
  contact: "right",
  human_agent: "left",
  agent_sent: "left",
  system: "center",
};

function EventBubble({ item, fallbackAt, agentName }: { item: ShownEvent; fallbackAt: string; agentName: string }) {
  const { event, sent } = item;
  const time = formatTime(event.at ?? fallbackAt);
  const side = SIDE[event.role] ?? "left";
  const content = event.content ?? "";

  if (side === "center") {
    return (
      <p className="mx-auto max-w-[85%] text-center text-xs text-muted-foreground">
        <span className="font-medium">{roleLabel(event.role)}</span>
        {time && <> · {time}</>} — {content}
      </p>
    );
  }

  const author =
    event.role === "agent_sent" ? `${roleLabel(event.role)} (${agentName})` : roleLabel(event.role);
  const body =
    event.kind === "document" ? (
      <DocumentEvent content={content} filename={event.attachments?.[0]?.filename} />
    ) : (
      content && <div className="whitespace-pre-wrap break-words">{content}</div>
    );

  return (
    <div className={cn("flex flex-col gap-1", side === "right" ? "items-end" : "items-start")}>
      <p className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
        <span className="font-medium">{author}</span>
        {time && <span>{time}</span>}
        {sent === "different" && (
          <Badge variant="warning" title="O que a instituição enviou não é o que o agente respondeu no turno anterior">
            enviado diferente
          </Badge>
        )}
      </p>
      {event.attachments && event.attachments.length > 0 && event.kind !== "document" && (
        <AttachmentChips attachments={event.attachments} />
      )}
      {body && (
        <div
          className={cn(
            "max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
            side === "right" && "rounded-br-md bg-muted",
            event.role === "human_agent" && "rounded-bl-md border border-violet-500/30 bg-violet-500/10",
            event.role === "agent_sent" && "rounded-bl-md border border-dashed border-border bg-surface"
          )}
        >
          {body}
        </div>
      )}
    </div>
  );
}

/** Evento `kind: document`: recolhido com o nome do arquivo; aberto, o conteúdo em markdown. */
function DocumentEvent({ content, filename }: { content: string; filename?: string | null }) {
  return (
    <details className="group" onClick={(e) => e.stopPropagation()}>
      <summary className="flex min-h-10 cursor-pointer list-none items-center gap-2">
        <ChevronRight className="size-4 shrink-0 transition-transform group-open:rotate-90" aria-hidden />
        <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className="truncate font-medium">{filename || "documento"}</span>
      </summary>
      {content && (
        <div className="mt-2 border-t border-border pt-2">
          <MessageContent content={content} />
        </div>
      )}
    </details>
  );
}

// -- tools ------------------------------------------------------------------------

const traceCache = new Map<string, Promise<RunTrace>>();

function loadTrace(runId: string): Promise<RunTrace> {
  let pending = traceCache.get(runId);
  if (!pending) {
    pending = requestJson<RunTrace>(`/api/observability/runs/${encodeURIComponent(runId)}/trace`, {
      fallbackError: "Falha ao carregar o trace",
    });
    pending.catch(() => traceCache.delete(runId));
    traceCache.set(runId, pending);
  }
  return pending;
}

function pretty(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

/**
 * Uma linha por tool: o nome legível, os argumentos principais e o começo do resultado.
 * Aberta, mostra os argumentos e o resultado inteiros — o resultado vem do trace do run
 * (a linha do tempo só traz a prévia).
 */
function StepLine({ step, index, runId }: { step: TimelineStep; index: number; runId: string }) {
  const { availableTools } = useWorkspace();
  const labels = useMemo(() => Object.fromEntries(availableTools.map((t) => [t.tool_name, t.label])), [availableTools]);
  const [open, setOpen] = useState(false);
  const [full, setFull] = useState<{ state: "idle" | "loading" | "ready" | "error"; output?: unknown }>({ state: "idle" });
  const badges = stepBadges(step);
  const result = step.error || step.result_preview;

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && full.state === "idle") {
      setFull({ state: "loading" });
      loadTrace(runId)
        .then((trace) => {
          const tools = trace.spans.filter((s) => s.type === "TOOL");
          const span = tools[index]?.name === step.tool ? tools[index] : tools.find((s) => s.name === step.tool);
          setFull({ state: "ready", output: span?.output });
        })
        .catch(() => setFull({ state: "error" }));
    }
  }

  return (
    <li className="ml-4 border-l-2 border-border pl-3 sm:ml-11">
      <button
        type="button"
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
        className="flex min-h-10 w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-md px-2 py-1 text-left text-xs hover:bg-muted"
        title={step.tool}
      >
        <ChevronRight className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-90")} aria-hidden />
        <Wrench className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <span className="font-medium">{stepLine(step, labels)}</span>
        {badges.includes("failed") && (
          <Badge variant="destructive" title={step.error ?? undefined}>
            <AlertTriangle className="size-3" /> falhou{step.http_status ? ` (${step.http_status})` : ""}
          </Badge>
        )}
        {badges.includes("not_found") && <Badge variant="outline">não encontrado</Badge>}
        {badges.includes("side_effect") && (
          <Badge variant="warning" title="Esta tool muda algo no sistema chamado">
            efeito colateral
          </Badge>
        )}
        {result && <span className="min-w-0 truncate text-muted-foreground">→ {shorten(result, 70)}</span>}
        {step.latency_ms != null && <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">{formatMs(step.latency_ms)}</span>}
      </button>
      {open && (
        <div className="mt-1 flex flex-col gap-2 pb-2" onClick={(e) => e.stopPropagation()}>
          <p className="font-mono text-[11px] text-muted-foreground">{step.tool}</p>
          <CodeBlock title="argumentos" code={pretty(step.args) || "(sem argumentos)"} />
          {step.error && <CodeBlock title="erro" code={step.error} />}
          {full.state === "loading" && (
            <p className="flex items-center gap-2 text-xs text-muted-foreground">
              <Spinner /> Carregando o resultado completo…
            </p>
          )}
          <CodeBlock
            title={full.state === "ready" ? "resultado" : "resultado (prévia)"}
            code={(full.state === "ready" ? pretty(full.output) : "") || step.result_preview || "(sem resultado)"}
          />
          <Link href={`/runs/${encodeURIComponent(runId)}`} className="text-xs text-muted-foreground hover:text-foreground hover:underline">
            Ver no trace
          </Link>
        </div>
      )}
    </li>
  );
}

/** Run de analista na sessão: recolhido atrás do cabeçalho com o agente, o status e o link. */
function AnalysisTurn({ turn }: { turn: TimelineTurn }) {
  const { getAgent } = useWorkspace();
  const agent = getAgent(turn.agent_type);
  // O AnalysisIO lê o documento (`message`) e a saída de um RunSummary.
  const run = {
    run_id: turn.run_id,
    trace_id: turn.run_id,
    agent_type: turn.agent_type,
    started_at: turn.at,
    status: turn.status,
    status_message: turn.status_message,
    message: turn.raw_input ?? turn.message,
    output: turn.output,
    input_tokens: turn.usage.input_tokens,
    output_tokens: turn.usage.output_tokens,
    total_tokens: turn.usage.total_tokens,
    feedback_up: null,
    feedback_down: null,
    endpoint: "analyze",
  } satisfies RunSummary;
  return (
    <details className="group rounded-xl border border-border bg-background" onClick={(e) => e.stopPropagation()}>
      <summary className="flex min-h-10 cursor-pointer list-none flex-wrap items-center gap-2 px-4 py-2 text-[13px]">
        <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" aria-hidden />
        <span className="font-medium">{agent?.name ?? turn.agent_type}</span>
        <Badge variant="outline">analista</Badge>
        <RunStatusBadge status={turn.status} title={turn.status_message} />
        <Link
          href={`/runs/${encodeURIComponent(turn.run_id)}`}
          className="ml-auto text-xs text-muted-foreground hover:text-foreground hover:underline"
          onClick={(e) => e.stopPropagation()}
        >
          Ver execução
        </Link>
      </summary>
      <div className="border-t border-border p-3">
        <AnalysisIO run={run} schema={agent?.response_schema} />
      </div>
    </details>
  );
}

// -- contexto ---------------------------------------------------------------------

/**
 * Os blocos de contexto de um turno, um cartão por bloco com o `title`. Os que mudaram
 * desde o turno estruturado anterior ficam destacados e mostram a diferença.
 */
export function ContextPanel({
  turn,
  number,
  first = false,
}: {
  turn: TimelineTurn | null;
  number: number | null;
  /** O primeiro turno estruturado da sessão: não há anterior para comparar. */
  first?: boolean;
}) {
  if (!turn) {
    return <p className="p-4 text-sm text-muted-foreground">Nenhum turno desta sessão trouxe contexto.</p>;
  }
  const changed = turn.context.filter((b) => b.changed).length;
  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="px-1">
        <p className="text-sm font-semibold">Contexto · turno {number}</p>
        <p className="text-xs text-muted-foreground">
          {formatDateTime(turn.at)}
          {turn.context.length > 0 &&
            ` · ${turn.context.length} ${turn.context.length === 1 ? "bloco" : "blocos"}${changed ? `, ${changed} ${changed === 1 ? "mudou" : "mudaram"}` : ""}`}
        </p>
      </div>
      {!turn.structured ? (
        <p className="px-1 text-sm text-muted-foreground">Entrada não estruturada: o contexto, se havia, está no texto do turno.</p>
      ) : turn.context.length === 0 ? (
        <p className="px-1 text-sm text-muted-foreground">Este turno não trouxe blocos de contexto.</p>
      ) : (
        turn.context.map((block) => <ContextCard key={block.key} block={block} first={first} />)
      )}
    </div>
  );
}

function ContextCard({ block, first }: { block: TimelineContextBlock; first: boolean }) {
  const [showDiff, setShowDiff] = useState(false);
  const isNew = block.changed && block.previous_content == null;
  return (
    <section
      className={cn(
        "flex flex-col gap-2 rounded-lg border bg-background p-3",
        block.changed ? "border-primary/60 ring-1 ring-primary/30" : "border-border"
      )}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <h3 className="min-w-0 flex-1 truncate text-[13px] font-semibold" title={block.key}>
          {block.title || block.key}
        </h3>
        {block.changed && <Badge variant="default">{isNew ? "novo" : "mudou"}</Badge>}
        {block.volatile && (
          <Badge variant="outline" title="Muda todo turno; não conta como mudança">
            volátil
          </Badge>
        )}
      </div>
      {showDiff ? (
        <DiffView previous={block.previous_content} current={block.content} />
      ) : (
        <pre className="scrollbar-thin max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-[11.5px] leading-relaxed">
          {block.content}
        </pre>
      )}
      {block.changed ? (
        <button
          type="button"
          onClick={() => setShowDiff((v) => !v)}
          aria-pressed={showDiff}
          className="inline-flex min-h-10 items-center self-start rounded-md px-2 text-xs font-medium text-primary hover:bg-muted"
        >
          {showDiff ? "ver o texto" : "ver diferença"}
        </button>
      ) : (
        !block.volatile &&
        !first && <p className="text-[11px] text-muted-foreground">Igual ao turno anterior.</p>
      )}
    </section>
  );
}

function DiffView({ previous, current }: { previous: string | null | undefined; current: string }) {
  const lines = blockDiff(previous, current);
  return (
    <div className="scrollbar-thin max-h-72 overflow-auto rounded-md border border-border font-mono text-[11.5px] leading-relaxed">
      {previous == null && <p className="border-b border-border px-2 py-1 text-muted-foreground">Bloco novo neste turno.</p>}
      {lines.map((line, i) => (
        <div
          key={i}
          className={cn(
            "whitespace-pre-wrap break-words px-2",
            line.type === "added" && "bg-success/10 text-success",
            line.type === "removed" && "bg-destructive/10 text-destructive line-through decoration-destructive/40"
          )}
        >
          <span aria-hidden className="mr-1 select-none">
            {line.type === "added" ? "+" : line.type === "removed" ? "−" : " "}
          </span>
          <span className="sr-only">{line.type === "added" ? "adicionado: " : line.type === "removed" ? "removido: " : ""}</span>
          {line.text || " "}
        </div>
      ))}
    </div>
  );
}
