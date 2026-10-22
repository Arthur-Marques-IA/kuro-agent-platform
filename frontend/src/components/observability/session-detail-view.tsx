"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { ChevronRight, GraduationCap, MessageSquare } from "lucide-react";
import { formatDateTime } from "@/lib/format";
import type { AgentKind, ChatMessage, RunSummary } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { CopyButton } from "@/components/ui/copy-button";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, RelativeTime, SectionHeading, Skeleton } from "@/components/ui/primitives";
import { Select } from "@/components/ui/select";
import { TeachAgent } from "@/components/agents/teach-agent";
import { MessageBubble } from "@/components/chat/message-bubble";
import { AnalysisIO, isAnalysisRun } from "@/components/observability/run-insights";
import { RunStatusBadge } from "@/components/observability/run-status";
import { RunsTable, useRunsPager } from "@/components/observability/runs-table";
import { StatsPanel } from "@/components/observability/stats-panel";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";

/**
 * Visão completa de uma sessão: a conversa, o feedback para o agente, gráficos e
 * todas as execuções.
 *
 * A conversa vem das execuções registradas (mensagem e resposta de cada run), e
 * não do Playground: ela aparece para qualquer sessão — de outro navegador, da
 * CLI, de um sistema integrado ou de um agente analista, que nem guarda
 * conversa. "Abrir no Playground" só aparece quando a sessão é deste navegador
 * (mesmo `user_id`) e de um agente conversacional; nos outros casos o Playground
 * não teria como abri-la, e o link levava a "Conversa não encontrada".
 */
export function SessionDetailView({ sessionId }: { sessionId: string }) {
  const { observability, userId, getAgent } = useWorkspace();
  const params = useMemo(() => ({ session_id: sessionId }), [sessionId]);
  const runsPager = useRunsPager(params);
  const runs = runsPager.items;
  const firstRun = runs[runs.length - 1];

  const conversationalAgents = useMemo(
    () =>
      [...new Set(runs.map((r) => r.agent_type))].filter((type) => getAgent(type)?.kind === "conversational"),
    [runs, getAgent]
  );
  const ownedByThisBrowser = runs.length > 0 && runs.every((r) => r.user_id === userId);
  const canOpenInPlayground = ownedByThisBrowser && conversationalAgents.length > 0;

  return (
    <>
      <PageHeader
        breadcrumbs={[
          { label: "Logs", href: "/logs" },
          { label: "Sessões", href: "/logs/sessions" },
          { label: "Sessão" },
        ]}
        title={
          <span className="inline-flex items-center gap-1.5 font-mono text-base">
            {sessionId}
            <CopyButton value={sessionId} label="Copiar id da sessão" className="size-6" />
          </span>
        }
        description={firstRun?.user_id ? `Usuário ${firstRun.user_id}` : undefined}
        actions={
          canOpenInPlayground ? (
            <Link href={`/chat/${encodeURIComponent(sessionId)}`} className={buttonVariants({ variant: "outline" })}>
              <MessageSquare />
              <span className="hidden sm:inline">Continuar no Playground</span>
            </Link>
          ) : undefined
        }
      />
      <PageBody className="flex flex-col gap-6">
        <div className="flex flex-col gap-3">
          <SectionHeading
            title="Conversa"
            description="As mensagens da sessão como no chat. Cada resposta leva ao trace dela; a tabela abaixo tem todas as execuções."
          />
          <Conversation runs={runs} loading={runsPager.state === "loading"} sessionId={sessionId} />
        </div>
        {observability.enabled && <StatsPanel params={params} showTrend={false} />}
        <RunsTable
          items={runs}
          state={runsPager.state}
          error={runsPager.error}
          cursor={runsPager.cursor}
          loadingMore={runsPager.loadingMore}
          onLoadMore={() => void runsPager.loadMore()}
          onRetry={() => void runsPager.reload()}
          emptyTitle="Nenhuma execução encontrada para esta sessão"
          showAgentColumn
        />
      </PageBody>
    </>
  );
}

function canTeach(kind: AgentKind | undefined): boolean {
  return kind === "conversational" || kind === "procedural";
}

/**
 * Um run de analista na sessão (ex.: `grupo-18`: triagem, redação...): o documento
 * de entrada costuma ser enorme, então ele fica recolhido atrás do cabeçalho com a
 * fase, o horário e o link para a execução.
 */
function AnalysisRunCard({ run }: { run: RunSummary }) {
  const { getAgent } = useWorkspace();
  const phase = run.metadata?.fase;
  return (
    <details className="group rounded-xl border border-border bg-background">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 px-4 py-2.5 text-[13px]">
        <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" aria-hidden />
        <span className="font-medium">{run.agent_name ?? getAgent(run.agent_type)?.name ?? run.agent_type}</span>
        {phase && <Badge variant="outline">{phase}</Badge>}
        <RunStatusBadge status={run.status} title={run.status_message} />
        <span className="text-xs text-muted-foreground" title={formatDateTime(run.started_at)}>
          <RelativeTime date={run.started_at} />
        </span>
        <Link
          href={`/runs/${encodeURIComponent(run.run_id)}`}
          className="ml-auto text-xs text-muted-foreground hover:text-foreground hover:underline"
          onClick={(e) => e.stopPropagation()}
        >
          Ver execução
        </Link>
      </summary>
      <div className="border-t border-border p-3">
        <AnalysisIO run={run} schema={getAgent(run.agent_type)?.response_schema} />
      </div>
    </details>
  );
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

function toMessages(run: RunSummary): ChatMessage[] {
  const messages: ChatMessage[] = [];
  if (run.message || run.attachments?.length) {
    messages.push({ id: `${run.run_id}:u`, role: "user", content: run.message ?? "", attachments: run.attachments });
  }
  messages.push({
    id: `${run.run_id}:a`,
    role: "assistant",
    content: run.output ? asMarkdown(run.output) : "",
    error: run.status === "success" ? undefined : run.status_message || run.status,
    runId: run.run_id,
    usage: {
      input_tokens: run.input_tokens,
      output_tokens: run.output_tokens,
      total_tokens: run.total_tokens,
      duration: run.latency_ms != null ? run.latency_ms / 1000 : undefined,
    },
  });
  return messages;
}

/**
 * A sessão como chat — as mesmas bolhas do Playground, só leitura. Cada resposta
 * mantém o link para o trace dela (spans, tokens, custo) e, em agente
 * conversacional, o 👎 abre "ensinar": o feedback vai com a conversa inteira
 * como contexto e esta resposta marcada.
 */
function Conversation({ runs, loading, sessionId }: { runs: RunSummary[]; loading: boolean; sessionId: string }) {
  const { getAgent } = useWorkspace();
  const ordered = useMemo(() => [...runs].reverse(), [runs]);
  // O procedural também aprende pela nota de feedback (`agents/registry.py` a injeta na resposta).
  const conversational = [...new Set(ordered.map((r) => r.agent_type))].filter((type) => canTeach(getAgent(type)?.kind));
  const [teachTarget, setTeachTarget] = useState<string | null>(null);
  const teachAgent = conversational.includes(teachTarget ?? "") ? teachTarget! : conversational[0];

  if (loading) return <Skeleton className="h-64" />;
  if (ordered.length === 0) {
    return (
      <Card>
        <EmptyState icon={MessageSquare} title="Sem mensagens" description="Nenhuma execução registrada nesta sessão." />
      </Card>
    );
  }

  return (
    <Card className="flex flex-col">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6">
        {ordered.flatMap((run) =>
          isAnalysisRun(run) ? [<AnalysisRunCard key={run.run_id} run={run} />] : toMessages(run).map((message) => (
            <MessageBubble
              key={message.id}
              message={message}
              agentType={run.agent_type}
              agentName={run.agent_name ?? getAgent(run.agent_type)?.name}
              sessionId={sessionId}
              canTeach={canTeach(getAgent(run.agent_type)?.kind)}
            />
          ))
        )}
      </div>
      {teachAgent && (
        <div className="border-t border-border bg-surface/60 px-4 py-4">
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="flex items-center gap-2 text-sm font-semibold">
                <GraduationCap className="size-4" /> Feedback sobre a conversa inteira
              </p>
              {conversational.length > 1 && (
                <Select
                  aria-label="Agente"
                  value={teachAgent}
                  onChange={(e) => setTeachTarget(e.target.value)}
                  wrapperClassName="w-52"
                >
                  {conversational.map((type) => (
                    <option key={type} value={type}>
                      {getAgent(type)?.name ?? type}
                    </option>
                  ))}
                </Select>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              Para uma resposta específica, use o 👎 embaixo dela (ou na tela da execução). Aqui o comentário vale para a conversa toda — a IA lê
              as mensagens acima e transforma em regra do agente.{" "}
              <Link href={`/agents/${encodeURIComponent(teachAgent)}?tab=feedback`} className="hover:underline">
                Ver as regras
              </Link>
            </p>
            <TeachAgent agentType={teachAgent} sessionId={sessionId} />
          </div>
        </div>
      )}
    </Card>
  );
}
