"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Brain, GraduationCap, Layers, MessageSquare, RefreshCw } from "lucide-react";
import { formatCost, formatDateTime, formatMs, formatNumber } from "@/lib/format";
import { ApiError, apiUrl, errorMessage, requestJson } from "@/lib/http";
import { contextTurnFor, sessionTitle } from "@/lib/timeline";
import type { SessionMemory } from "@/lib/transcript";
import type { SessionTimeline } from "@/lib/types";
import { useEscapeBack } from "@/lib/use-list-nav";
import { Badge } from "@/components/ui/badge";
import { CopyButton } from "@/components/ui/copy-button";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState, Skeleton, Spinner } from "@/components/ui/primitives";
import { Select } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TeachAgent } from "@/components/agents/teach-agent";
import { ContextPanel, SessionConversation, canTeach } from "@/components/observability/session-conversation";
import { RunsTable, useRunsPager } from "@/components/observability/runs-table";
import { StatsPanel } from "@/components/observability/stats-panel";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";

const PAGE_SIZE = 100;

type TimelineState =
  | { kind: "loading" }
  | { kind: "not-found" }
  | { kind: "unavailable"; message: string }
  | { kind: "error"; message: string }
  | { kind: "ready"; timeline: SessionTimeline };

/** Carrega a linha do tempo e as páginas seguintes (turnos mais novos) pelo `next_cursor`. */
function useTimeline(sessionId: string) {
  const [state, setState] = useState<TimelineState>({ kind: "loading" });
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  const fetchPage = useCallback(
    (cursor: string | null) => {
      const query = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (cursor) query.set("cursor", cursor);
      return requestJson<SessionTimeline>(`/api/sessions/${encodeURIComponent(sessionId)}/timeline?${query}`, {
        fallbackError: "Falha ao carregar a conversa",
      });
    },
    [sessionId]
  );

  const reload = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      setState({ kind: "ready", timeline: await fetchPage(null) });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setState({ kind: "not-found" });
      else if (err instanceof ApiError && err.status === 501) setState({ kind: "unavailable", message: err.message });
      else setState({ kind: "error", message: errorMessage(err) });
    }
  }, [fetchPage]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
  }, [reload]);

  async function loadMore() {
    if (state.kind !== "ready" || !state.timeline.next_cursor) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await fetchPage(state.timeline.next_cursor);
      setState({
        kind: "ready",
        timeline: { ...page, items: [...state.timeline.items, ...page.items] },
      });
    } catch (err) {
      setMoreError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }

  return { state, reload, loadMore, loadingMore, moreError };
}

/**
 * Uma sessão como gente lê: a conversa (cada evento uma bolha, as tools numa linha, a
 * resposta do agente) com o contexto do turno em foco ao lado — num painel fixo em tela
 * larga, numa gaveta em tela estreita. Embaixo, em abas: as execuções, o que o agente
 * lembra e os gráficos.
 *
 * Tudo vem da linha do tempo (`GET /observability/sessions/{id}/timeline`), que já traz
 * os campos separados pela entrada estruturada do `/chat`: a tela não recorta texto. Um
 * run de antes dela aparece como "entrada não estruturada", com o texto original.
 */
export function SessionDetailView({ sessionId }: { sessionId: string }) {
  const { observability, userId, getAgent } = useWorkspace();
  const { state, reload, loadMore, loadingMore, moreError } = useTimeline(sessionId);
  const timeline = state.kind === "ready" ? state.timeline : null;
  const turns = useMemo(() => timeline?.items ?? [], [timeline]);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Esc volta para a lista de onde se veio (ou para Sessões); com a gaveta aberta, o Esc é dela.
  useEscapeBack("/logs/sessions");

  // O painel mostra o turno em foco se ele tem contexto; senão, o último turno com contexto antes dele.
  const contextTurn = contextTurnFor(turns, focusId);
  const hasContext = turns.some((t) => t.context.length > 0);
  // A lista sempre começa no início da sessão (as páginas seguintes vêm depois), então o
  // primeiro turno estruturado carregado é o primeiro da sessão: não há anterior para comparar.
  const firstContext = !!contextTurn && turns.findIndex((t) => t.structured) === contextTurn.i;

  const agentTypes = timeline?.agent_types ?? [];
  const conversationalAgents = agentTypes.filter((type) => getAgent(type)?.kind === "conversational");
  const canOpenInPlayground = !!timeline && timeline.user_id === userId && conversationalAgents.length > 0;

  const onFocus = useCallback((id: string) => setFocusId(id), []);
  const onShowContext = useCallback((id: string) => {
    setFocusId(id);
    if (typeof window !== "undefined" && !window.matchMedia("(min-width: 1024px)").matches) setDrawerOpen(true);
  }, []);

  return (
    <>
      <PageHeader
        breadcrumbs={[
          { label: "Logs", href: "/logs" },
          { label: "Sessões", href: "/logs/sessions" },
          { label: "Sessão" },
        ]}
        title={timeline ? sessionTitle(timeline) : "Sessão"}
        description={<SessionFacts sessionId={sessionId} timeline={timeline} />}
        actions={
          <>
            {hasContext && (
              <Button variant="outline" className="h-10 lg:hidden" onClick={() => setDrawerOpen(true)}>
                <Layers />
                Contexto
              </Button>
            )}
            {canOpenInPlayground && (
              <Link href={`/chat/${encodeURIComponent(sessionId)}`} className={buttonVariants({ variant: "outline" })}>
                <MessageSquare />
                <span className="hidden sm:inline">Continuar no Playground</span>
              </Link>
            )}
          </>
        }
      />
      <PageBody className="flex flex-col gap-6">
        {state.kind === "loading" ? (
          <Skeleton className="h-96" />
        ) : state.kind !== "ready" ? (
          <Card>
            <EmptyState
              icon={MessageSquare}
              title={
                state.kind === "not-found"
                  ? "Sem execuções nesta sessão"
                  : state.kind === "unavailable"
                    ? "Conversa indisponível neste backend"
                    : "Não foi possível carregar a conversa"
              }
              description={
                state.kind === "not-found"
                  ? "Nenhuma execução registrada com este session_id."
                  : state.kind === "unavailable"
                    ? "A conversa separada em campos só existe com o trace store local (TRACE_STORE_BACKEND=db). As execuções continuam na aba abaixo."
                    : state.message
              }
              action={
                state.kind === "error" ? (
                  <Button variant="outline" size="sm" onClick={() => void reload()}>
                    <RefreshCw /> Tentar de novo
                  </Button>
                ) : undefined
              }
            />
          </Card>
        ) : (
          <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_340px] xl:grid-cols-[minmax(0,1fr)_400px]">
            <Card className="flex min-w-0 flex-col">
              <div className="px-1 py-3 sm:px-3">
                <SessionConversation
                  turns={turns}
                  sessionId={sessionId}
                  focusId={focusId}
                  onFocus={onFocus}
                  onShowContext={onShowContext}
                />
                {(timeline?.next_cursor || moreError) && (
                  <div className="flex flex-col items-center gap-1 py-3">
                    {moreError && <p className="text-xs text-destructive">{moreError}</p>}
                    {timeline?.next_cursor && (
                      <Button variant="outline" className="h-10" onClick={() => void loadMore()} disabled={loadingMore}>
                        {loadingMore && <Spinner />}
                        Carregar turnos mais novos
                      </Button>
                    )}
                  </div>
                )}
              </div>
              <TeachConversation agentTypes={agentTypes} sessionId={sessionId} />
            </Card>
            {hasContext && (
              <aside
                aria-label="Contexto do turno em foco"
                className="scrollbar-thin sticky top-4 hidden max-h-[calc(100dvh-2rem)] overflow-y-auto rounded-xl border border-border bg-surface lg:block"
              >
                <ContextPanel turn={contextTurn?.turn ?? null} number={contextTurn ? contextTurn.i + 1 : null} first={firstContext} />
              </aside>
            )}
          </div>
        )}

        <Dialog open={drawerOpen} onOpenChange={setDrawerOpen} side="right" className="bg-surface">
          <DialogHeader className="border-b border-border">
            <DialogTitle>Contexto</DialogTitle>
          </DialogHeader>
          <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto">
            <ContextPanel turn={contextTurn?.turn ?? null} number={contextTurn ? contextTurn.i + 1 : null} first={firstContext} />
          </div>
        </Dialog>

        <SessionDetailsTabs sessionId={sessionId} statsEnabled={observability.enabled} />
      </PageBody>
    </>
  );
}

/** Quem, por onde, quando, quanto: o resumo da sessão embaixo do título. O id fica copiável. */
function SessionFacts({ sessionId, timeline }: { sessionId: string; timeline: SessionTimeline | null }) {
  const { getAgent } = useWorkspace();
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-muted-foreground">
      {timeline && (
        <>
          {timeline.contact_name && timeline.user_id && <span>usuário {timeline.user_id}</span>}
          {timeline.agent_types.map((type) => (
            <Badge key={type} variant="outline" title={type}>
              {getAgent(type)?.name ?? type}
            </Badge>
          ))}
          {timeline.channel && <Badge variant="secondary">{timeline.channel}</Badge>}
          <span title="Início">{formatDateTime(timeline.started_at)}</span>
          <span title="Do primeiro ao último turno">{formatMs(timeline.duration_ms)}</span>
          <span>
            {formatNumber(timeline.turns)} {timeline.turns === 1 ? "turno" : "turnos"}
            {timeline.error_count > 0 && <span className="text-destructive"> · {timeline.error_count} com erro</span>}
          </span>
          <span title={`${formatNumber(timeline.total_tokens)} tokens`}>{formatCost(timeline.cost_usd)}</span>
        </>
      )}
      <span className="inline-flex items-center rounded-md bg-muted pl-2 font-mono text-[11px] text-foreground/80">
        {sessionId}
        <CopyButton value={sessionId} label="Copiar id da sessão" className="size-8" />
      </span>
    </div>
  );
}

/** Em agente conversacional ou procedural: o feedback que vale para a conversa inteira. */
function TeachConversation({ agentTypes, sessionId }: { agentTypes: string[]; sessionId: string }) {
  const { getAgent } = useWorkspace();
  // O procedural também aprende pela nota de feedback (`agents/registry.py` a injeta na resposta).
  const teachable = agentTypes.filter((type) => canTeach(getAgent(type)?.kind));
  const [target, setTarget] = useState<string | null>(null);
  const agentType = teachable.includes(target ?? "") ? target! : teachable[0];
  if (!agentType) return null;
  return (
    <div className="border-t border-border bg-surface/60 px-4 py-4">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="flex items-center gap-2 text-sm font-semibold">
            <GraduationCap className="size-4" /> Feedback sobre a conversa inteira
          </p>
          {teachable.length > 1 && (
            <Select aria-label="Agente" value={agentType} onChange={(e) => setTarget(e.target.value)} wrapperClassName="w-52">
              {teachable.map((type) => (
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
          <Link href={`/agents/${encodeURIComponent(agentType)}?tab=feedback`} className="hover:underline">
            Ver as regras
          </Link>
        </p>
        <TeachAgent agentType={agentType} sessionId={sessionId} />
      </div>
    </div>
  );
}

/** O resto da sessão, fora do caminho da leitura: execuções, memória do agente e gráficos. */
function SessionDetailsTabs({ sessionId, statsEnabled }: { sessionId: string; statsEnabled: boolean }) {
  const [tab, setTab] = useState("runs");
  return (
    <Tabs value={tab} onValueChange={setTab} className="flex flex-col gap-4">
      <TabsList>
        <TabsTrigger value="runs">Execuções</TabsTrigger>
        <TabsTrigger value="memory">O que o agente lembra</TabsTrigger>
        {statsEnabled && <TabsTrigger value="stats">Gráficos</TabsTrigger>}
      </TabsList>
      <TabsContent value="runs" className="flex flex-col gap-3">
        <SessionRuns sessionId={sessionId} />
      </TabsContent>
      <TabsContent value="memory">
        <AgentMemory sessionId={sessionId} />
      </TabsContent>
      {statsEnabled && (
        <TabsContent value="stats">
          <StatsPanel params={{ session_id: sessionId }} showTrend={false} />
        </TabsContent>
      )}
    </Tabs>
  );
}

function SessionRuns({ sessionId }: { sessionId: string }) {
  const params = useMemo(() => ({ session_id: sessionId }), [sessionId]);
  const pager = useRunsPager(params);
  return (
    <RunsTable
      items={pager.items}
      state={pager.state}
      error={pager.error}
      cursor={pager.cursor}
      loadingMore={pager.loadingMore}
      onLoadMore={() => void pager.loadMore()}
      onRetry={() => void pager.reload()}
      emptyTitle="Nenhuma execução encontrada para esta sessão"
      showAgentColumn
    />
  );
}

/** O que o agente lembra desta conversa: o resumo da sessão e as memórias do contato. */
function AgentMemory({ sessionId }: { sessionId: string }) {
  const [data, setData] = useState<SessionMemory | null | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/transcript`))
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => alive && setData(j))
      .catch(() => alive && setData(null));
    return () => {
      alive = false;
    };
  }, [sessionId]);
  if (data === undefined) return <Skeleton className="h-32" />;
  if (!data) {
    return (
      <Card>
        <EmptyState
          icon={Brain}
          title="Nada guardado pelo agente"
          description="A sessão não existe no armazenamento do agente (agente analista, ou conversa apagada)."
        />
      </Card>
    );
  }
  const memorias = data.memorias_do_contato ?? [];
  return (
    <Card className="flex flex-col gap-4 p-4 text-sm">
      <div>
        <p className="mb-1 flex items-center gap-2 font-medium">
          <Brain className="size-4" /> Resumo da sessão
        </p>
        {data.resumo_da_sessao?.texto ? (
          <p className="whitespace-pre-wrap text-muted-foreground">{data.resumo_da_sessao.texto}</p>
        ) : (
          <p className="text-muted-foreground">Sem resumo — o agente não resume ou a conversa ainda cabe na janela de histórico.</p>
        )}
      </div>
      <div>
        <p className="mb-1 font-medium">Memórias do contato ({memorias.length})</p>
        {memorias.length === 0 ? (
          <p className="text-muted-foreground">Nenhuma memória de longo prazo para este contato.</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {memorias.map((m, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-2">
                <span>{m.texto}</span>
                {m.topicos.map((t) => (
                  <Badge key={t} variant="outline">
                    {t}
                  </Badge>
                ))}
                {m.atualizado_em && <span className="text-xs text-muted-foreground">{m.atualizado_em}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
