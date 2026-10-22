"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Check, CircleAlert, FileText, GitCommitVertical, ListTree, ThumbsDown, ThumbsUp } from "lucide-react";
import { cn } from "@/lib/cn";
import { errorMessage, requestJson } from "@/lib/http";
import { formatDateTime, formatMs } from "@/lib/format";
import { displayValue, outputTree, parseStructured, type OutputNode } from "@/lib/structured-output";
import type { ProcedureState, RelatedRuns, RunSummary, SchemaField } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/primitives";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/toast";
import { TeachAgent } from "@/components/agents/teach-agent";
import { STAGE_TYPE_META } from "@/components/chat/procedure-progress";
import { RunStatusBadge } from "@/components/observability/run-status";
import { useWorkspace } from "@/components/workspace/workspace-provider";

/** Execução do `/analyze`: o endpoint gravado no run, não o `kind` atual do agente (que pode ter mudado). */
export function isAnalysisRun(run: RunSummary): boolean {
  return run.endpoint === "analyze";
}

// -- analista: entrada e saída --------------------------------------------------------

/** O documento que entrou (quebras de linha preservadas) e a saída como árvore de campos. */
export function AnalysisIO({ run, schema }: { run: RunSummary; schema?: SchemaField[] | null }) {
  const structured = useMemo(() => parseStructured(run.output), [run.output]);
  return (
    <div className="grid items-start gap-4 lg:grid-cols-2">
      <Card className="flex min-w-0 flex-col">
        <div className="flex h-10 items-center justify-between gap-2 border-b border-border px-4">
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <FileText className="size-4 text-muted-foreground" aria-hidden /> Entrada
          </h2>
          <span className="flex items-center gap-2 text-xs text-muted-foreground">
            {(run.message ?? "").length.toLocaleString("pt-BR")} caracteres
            {run.message && <CopyButton value={run.message} label="Copiar entrada" className="size-6" />}
          </span>
        </div>
        <pre className="scrollbar-thin max-h-[32rem] overflow-auto whitespace-pre-wrap break-words px-4 py-3 font-mono text-[12px] leading-relaxed">
          {run.message || "—"}
        </pre>
      </Card>
      <Card className="flex min-w-0 flex-col">
        <div className="flex h-10 items-center justify-between gap-2 border-b border-border px-4">
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <ListTree className="size-4 text-muted-foreground" aria-hidden /> Saída
          </h2>
          {run.output && <CopyButton value={run.output} label="Copiar saída" className="size-6" />}
        </div>
        {structured ? (
          <OutputTreeView nodes={outputTree(structured, schema)} />
        ) : (
          <pre className="scrollbar-thin max-h-[32rem] overflow-auto whitespace-pre-wrap break-words px-4 py-3 font-mono text-[12px]">
            {run.output || (run.status === "success" ? "—" : "O run não produziu saída.")}
          </pre>
        )}
      </Card>
    </div>
  );
}

function OutputTreeView({ nodes, depth = 0 }: { nodes: OutputNode[]; depth?: number }) {
  if (nodes.length === 0) return <p className="px-4 py-3 text-xs text-muted-foreground">(vazio)</p>;
  return (
    <dl className={cn("flex flex-col text-[13px]", depth === 0 && "scrollbar-thin max-h-[32rem] overflow-auto py-1")}>
      {nodes.map((node) => (
        <Fragment key={node.path}>
          <div
            className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-3 border-b border-border/60 px-4 py-1.5 last:border-b-0"
            style={{ paddingLeft: 16 + depth * 16 }}
          >
            <dt className="truncate text-muted-foreground" title={node.path}>
              {node.label}
              {node.label !== node.key && <span className="ml-1 font-mono text-[11px] opacity-70">{node.key}</span>}
            </dt>
            <dd className="min-w-0 whitespace-pre-wrap break-words">
              {node.kind === "value" ? (
                <span className={cn(node.value == null && "text-muted-foreground")}>{displayValue(node.value)}</span>
              ) : (
                <span className="text-xs text-muted-foreground">
                  {node.kind === "array" ? `${node.children?.length ?? 0} itens` : "objeto"}
                </span>
              )}
            </dd>
          </div>
          {node.children && node.children.length > 0 && <OutputTreeView nodes={node.children} depth={depth + 1} />}
        </Fragment>
      ))}
    </dl>
  );
}

// -- procedural: o estado do turno ------------------------------------------------------

export function ProcedureTurnState({ state }: { state: ProcedureState }) {
  const current = state.stages.find((s) => s.id === state.stage);
  const collected = Object.entries(state.collected).filter(([, v]) => v !== null && v !== undefined);
  const invalid = Object.entries(state.invalid);
  return (
    <Card className="flex flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
        <h2 className="text-sm font-semibold">Procedimento neste turno</h2>
        {state.done ? (
          <Badge variant="success">
            <Check /> concluído
          </Badge>
        ) : (
          <Badge variant="outline">
            Etapa {Math.min(state.stage_index + 1, state.stages_total)} de {state.stages_total}
            {current ? ` · ${current.goal || current.id}` : ""}
          </Badge>
        )}
        {state.stage_type && <Badge variant="secondary">{STAGE_TYPE_META[state.stage_type]?.label ?? state.stage_type}</Badge>}
        {state.dry_run && <Badge variant="warning">teste</Badge>}
      </div>
      <ol className="flex flex-wrap gap-1.5 border-b border-border px-4 py-2" aria-label="Etapas">
        {state.stages.map((s) => (
          <li
            key={s.id}
            className={cn(
              "rounded-md px-2 py-0.5 text-xs",
              s.status === "done" && "bg-success/10 text-success",
              s.status === "current" && "bg-primary/10 font-medium text-primary",
              s.status === "pending" && "bg-muted text-muted-foreground"
            )}
            title={s.goal}
          >
            {s.id}
          </li>
        ))}
      </ol>
      <div className="grid gap-4 p-4 md:grid-cols-3">
        <section className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-muted-foreground">Coletado</h3>
          {collected.length === 0 ? (
            <p className="text-xs text-muted-foreground">nada ainda</p>
          ) : (
            <dl className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-x-3 gap-y-1 font-mono text-[12px]">
              {collected.map(([key, value]) => (
                <Fragment key={key}>
                  <dt className="truncate text-muted-foreground">{key}</dt>
                  <dd className="break-words">{displayValue(value)}</dd>
                </Fragment>
              ))}
            </dl>
          )}
        </section>
        <section className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-muted-foreground">Faltando</h3>
          {state.missing.length === 0 ? (
            <p className="text-xs text-muted-foreground">nada nesta etapa</p>
          ) : (
            <ul className="flex flex-wrap gap-1.5">
              {state.missing.map((f) => (
                <li key={f.name}>
                  <Badge variant="outline" title={f.description}>
                    {f.label || f.name}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-muted-foreground">Inválido</h3>
          {invalid.length === 0 ? (
            <p className="text-xs text-muted-foreground">nenhum valor recusado</p>
          ) : (
            <ul className="flex flex-col gap-1 text-[12px]">
              {invalid.map(([key, reason]) => (
                <li key={key} className="flex gap-1.5 text-warning">
                  <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                  <span>
                    <span className="font-mono">{key}</span>: {reason}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
      {Object.keys(state.actions).length > 0 && (
        <div className="flex flex-wrap gap-2 border-t border-border px-4 py-2.5 text-xs">
          <span className="text-muted-foreground">Ações:</span>
          {Object.entries(state.actions).map(([id, action]) => (
            <Badge
              key={id}
              variant={action.status === "done" ? "success" : action.status === "failed" ? "destructive" : "outline"}
              title={action.error ?? undefined}
            >
              {id} · {action.status}
              {action.attempts ? ` · ${action.attempts}×` : ""}
            </Badge>
          ))}
        </div>
      )}
    </Card>
  );
}

// -- linha do tempo por correlação --------------------------------------------------------

/** Os runs com a mesma chave de correlação (`ciclo_id`...) em ordem, de qualquer agente. */
export function RelatedTimeline({ runId }: { runId: string }) {
  const [related, setRelated] = useState<RelatedRuns | null>(null);

  useEffect(() => {
    let alive = true;
    requestJson<RelatedRuns>(`/api/observability/runs/${encodeURIComponent(runId)}/related`, {
      fallbackError: "Falha ao carregar os runs relacionados",
    })
      .then((data) => alive && setRelated(data))
      .catch(() => alive && setRelated(null)); // a linha do tempo é um extra: sem ela, a tela segue
    return () => {
      alive = false;
    };
  }, [runId]);

  if (!related?.key || related.items.length < 2) return null;
  const start = new Date(related.items[0].started_at).getTime();

  return (
    <Card className="flex flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
        <h2 className="text-sm font-semibold">Linha do tempo</h2>
        <span className="font-mono text-xs text-muted-foreground">
          {related.key}={related.value}
        </span>
        <span className="ml-auto text-xs text-muted-foreground">{related.items.length} execuções</span>
      </div>
      <ol className="flex flex-col py-1">
        {related.items.map((item) => {
          const current = item.run_id === runId;
          const phase = item.metadata?.fase;
          return (
            <li key={item.run_id}>
              <Link
                href={`/runs/${encodeURIComponent(item.run_id)}`}
                aria-current={current || undefined}
                className={cn(
                  "flex min-w-0 items-center gap-3 px-4 py-1.5 text-[13px] transition-colors hover:bg-accent/50",
                  current && "bg-accent font-medium"
                )}
              >
                <GitCommitVertical className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                <span className="w-16 shrink-0 text-xs tabular-nums text-muted-foreground" title={formatDateTime(item.started_at)}>
                  +{formatMs(new Date(item.started_at).getTime() - start)}
                </span>
                <span className="min-w-0 truncate">{item.agent_name ?? item.agent_type}</span>
                {phase && <Badge variant="outline">{phase}</Badge>}
                <span className="ml-auto flex shrink-0 items-center gap-2">
                  <span className="hidden text-xs tabular-nums text-muted-foreground sm:inline">{formatMs(item.latency_ms)}</span>
                  <RunStatusBadge status={item.status} title={item.status_message} />
                </span>
              </Link>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

// -- feedback do run ----------------------------------------------------------------

/**
 * 👍/👎 na tela do run.
 *
 * - Analista: o 👎 pede um comentário e a saída correta (começa pela atual, para
 *   editar só o que errou). Ela vira a referência do run — caso de `kuro eval` em
 *   `kuro runs export` —, não regra no prompt.
 * - Conversacional e procedural com sessão: o 👎 abre o "ensinar", que mescla o
 *   comentário nas regras do agente lendo a conversa (`/agents/{tipo}/feedback`).
 */
export function RunFeedback({
  run,
  analysis,
  canTeach,
  hasReference,
  onSaved,
}: {
  run: RunSummary;
  analysis: boolean;
  canTeach: boolean;
  hasReference: boolean;
  onSaved: () => void;
}) {
  const { userId } = useWorkspace();
  const toast = useToast();
  const initialOutput = useMemo(() => {
    const parsed = parseStructured(run.output);
    return parsed && !Array.isArray(parsed) ? JSON.stringify(parsed, null, 2) : "{}";
  }, [run.output]);
  const [open, setOpen] = useState(false);
  const [comment, setComment] = useState("");
  const [expected, setExpected] = useState(initialOutput);
  const [sending, setSending] = useState(false);

  const expectedError = useMemo(() => {
    if (!analysis) return null;
    try {
      const value: unknown = JSON.parse(expected);
      return value && typeof value === "object" && !Array.isArray(value) ? null : "A saída correta precisa ser um objeto JSON.";
    } catch {
      return "JSON inválido.";
    }
  }, [analysis, expected]);

  async function send(value: 0 | 1, reference?: Record<string, unknown>) {
    setSending(true);
    try {
      await requestJson(`/api/observability/runs/${encodeURIComponent(run.run_id)}/feedback`, {
        method: "POST",
        json: { value, comment: comment.trim() || null, user_id: userId, reference: reference ?? null },
        fallbackError: "Falha ao registrar o feedback",
      });
      toast({
        title: reference ? "Saída correta gravada" : "Feedback registrado",
        description: reference ? "O run sai em `kuro runs export` como caso de eval." : undefined,
        variant: "success",
      });
      setOpen(false);
      setComment("");
      onSaved();
    } catch (err) {
      toast({ title: "Não foi possível registrar o feedback", description: errorMessage(err), variant: "error" });
    } finally {
      setSending(false);
    }
  }

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">Avaliar esta execução</h2>
        {hasReference && <Badge variant="outline">já tem saída correta</Badge>}
        <span className="ml-auto flex items-center gap-1">
          <Button variant="outline" size="sm" disabled={sending} onClick={() => void send(1)}>
            <ThumbsUp /> Correta
          </Button>
          <Button variant={open ? "secondary" : "outline"} size="sm" disabled={sending} onClick={() => setOpen((v) => !v)}>
            <ThumbsDown /> Errada
          </Button>
        </span>
      </div>
      {open && analysis && (
        <div className="flex flex-col gap-2">
          <Input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="O que estava errado?" className="text-[13px]" />
          <label className="text-xs text-muted-foreground" htmlFor={`expected-${run.run_id}`}>
            Saída correta — edite só os campos que erraram. Ela vira caso de eval (não regra no prompt): corrija as
            instructions num draft e confira com <span className="font-mono">kuro eval</span>.
          </label>
          <Textarea
            id={`expected-${run.run_id}`}
            value={expected}
            onChange={(e) => setExpected(e.target.value)}
            rows={Math.min(Math.max(expected.split("\n").length, 6), 20)}
            className="font-mono text-[12px]"
            spellCheck={false}
          />
          <div className="flex items-center justify-between gap-2">
            <p className={cn("text-xs", expectedError ? "text-destructive" : "text-muted-foreground")}>
              {expectedError ?? "Fato errado (dado da base) se corrige na base de conhecimento, não aqui."}
            </p>
            <span className="flex gap-1">
              <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
                Cancelar
              </Button>
              <Button
                size="sm"
                disabled={sending || !!expectedError}
                onClick={() => void send(0, JSON.parse(expected) as Record<string, unknown>)}
              >
                {sending && <Spinner />} Gravar saída correta
              </Button>
            </span>
          </div>
        </div>
      )}
      {open && !analysis && (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <Input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Comentário (opcional)" className="text-[13px]" />
            <Button size="sm" disabled={sending} onClick={() => void send(0)}>
              Registrar 👎
            </Button>
          </div>
          {canTeach && run.session_id ? (
            <div className="flex flex-col gap-1.5 border-t border-border pt-3">
              <p className="text-xs text-muted-foreground">
                Ou ensine o agente: a IA lê a conversa desta sessão e mescla o comentário nas regras dele.{" "}
                <Link href={`/agents/${encodeURIComponent(run.agent_type)}?tab=feedback`} className="hover:underline">
                  Ver as regras
                </Link>
              </p>
              <TeachAgent agentType={run.agent_type} sessionId={run.session_id} />
            </div>
          ) : null}
        </div>
      )}
    </Card>
  );
}
