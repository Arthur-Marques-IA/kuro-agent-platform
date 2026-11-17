"use client";

import { type ReactNode, useMemo, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, Bot, CircleAlert, FileSearch, GitCompare, MessageSquare, Plus, Search, X } from "lucide-react";
import { modelLabel } from "@/lib/agent-meta";
import {
  KIND_LABELS,
  SORT_LABELS,
  draftDifferences,
  draftOf,
  entryLastUsed,
  mergeLastUsed,
  pairAgents,
  sortEntries,
  type AgentEntry,
  type AgentSort,
} from "@/lib/agent-list";
import { cn } from "@/lib/cn";
import { formatDateTime, formatRelativeShort, pluralize, toDate } from "@/lib/format";
import type { AgentDefinition, AgentKind } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardFooter } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { EmptyState, Skeleton } from "@/components/ui/primitives";
import { Select } from "@/components/ui/select";
import { AgentAvatar } from "@/components/agents/agent-avatar";
import { memoryLabel } from "@/components/agents/agent-labels";
import { CompareDraftDialog } from "@/components/agents/compare-draft-dialog";
import { PromoteDialog } from "@/components/agents/revisions-panel";
import { useOverview } from "@/components/observability/overview-panel";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";

/** Janela do "uso recente": as execuções dos últimos 30 dias, testes do Playground incluídos. */
const USAGE_WINDOW_DAYS = 30;
const KINDS: AgentKind[] = ["conversational", "analysis", "procedural"];

export function AgentsIndex() {
  const { agents, sessions, sessionsLoading, backendReachable } = useWorkspace();
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<AgentKind | null>(null);
  const [sort, setSort] = useState<AgentSort>("recent");
  const [since] = useState(() => new Date(Date.now() - USAGE_WINDOW_DAYS * 86_400_000).toISOString());
  // O panorama dos Logs já traz a última execução de cada agente. Sem ele (trace store no
  // Langfuse responde 501), o uso vem só das conversas deste navegador.
  const overview = useOverview({ since, include_dry_run: "true" }, backendReachable && agents.length > 0);

  const conversationCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const s of sessions) if (s.agent_id) counts.set(s.agent_id, (counts.get(s.agent_id) ?? 0) + 1);
    return counts;
  }, [sessions]);

  const lastUsed = useMemo(
    () =>
      mergeLastUsed(
        (overview.data?.agents ?? []).map((a) => [a.agent_type, a.last_run_at]),
        sessions.filter((s) => s.agent_id).map((s) => [s.agent_id!, s.updated_at ?? s.created_at])
      ),
    [overview.data, sessions]
  );

  const entries = useMemo(() => pairAgents(agents), [agents]);

  const kindCounts = useMemo(() => {
    const counts = new Map<AgentKind, number>();
    for (const e of entries) counts.set(e.agent.kind, (counts.get(e.agent.kind) ?? 0) + 1);
    return counts;
  }, [entries]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matches = (a: AgentDefinition) =>
      `${a.name} ${a.agent_type} ${a.instructions.join(" ")}`.toLowerCase().includes(q);
    const visible = entries.filter(
      (e) => (!kind || e.agent.kind === kind) && (!q || matches(e.agent) || (e.draft != null && matches(e.draft)))
    );
    return sortEntries(visible, sort, lastUsed);
  }, [entries, query, kind, sort, lastUsed]);

  const filtering = query.trim() !== "" || kind !== null;
  const usageReady = sort !== "recent" || ((overview.data !== null || overview.state === "error") && !sessionsLoading);
  const usedKinds = KINDS.filter((k) => kindCounts.has(k));

  const newAgentLink = (
    <Link href="/agents/new" className={buttonVariants()}>
      <Plus /> Novo agente
    </Link>
  );

  return (
    <>
      <PageHeader
        title="Agentes"
        description="Crie e ajuste agentes sem deploy: prompt versionado, modelo, memória e tools — disponíveis na API assim que salvos."
        actions={backendReachable && agents.length > 0 ? newAgentLink : undefined}
      />
      <PageBody>
        {!backendReachable ? (
          <Card>
            <EmptyState
              icon={CircleAlert}
              title="agent-service indisponível"
              description="Não foi possível carregar os agentes. Verifique se o backend está rodando e recarregue a página."
            />
          </Card>
        ) : agents.length === 0 ? (
          <Card>
            <EmptyState
              icon={Bot}
              title="Nenhum agente ainda"
              description="Crie o primeiro agente definindo um nome e as instruções do prompt."
              action={newAgentLink}
            />
          </Card>
        ) : (
          <div className="flex flex-col gap-5">
            <div className="flex flex-col gap-3">
              <div className="flex flex-wrap items-center gap-3">
                <div className="relative w-full sm:max-w-xs">
                  <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    type="search"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Buscar por nome, slug ou prompt"
                    aria-label="Buscar agentes"
                    className="pl-9"
                  />
                </div>
                <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
                  Ordenar
                  <Select
                    value={sort}
                    onChange={(e) => setSort(e.target.value as AgentSort)}
                    aria-label="Ordenar agentes"
                    className="text-foreground"
                  >
                    {(Object.keys(SORT_LABELS) as AgentSort[]).map((value) => (
                      <option key={value} value={value}>
                        {SORT_LABELS[value]}
                      </option>
                    ))}
                  </Select>
                </label>
                <p className="ml-auto text-[13px] text-muted-foreground">
                  {filtering
                    ? `${filtered.length} de ${pluralize(entries.length, "agente", "agentes")}`
                    : pluralize(entries.length, "agente", "agentes")}
                </p>
              </div>
              {usedKinds.length > 1 && (
                <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filtrar por tipo">
                  <Chip active={kind === null} onClick={() => setKind(null)}>
                    Todos <Count n={entries.length} />
                  </Chip>
                  {usedKinds.map((k) => (
                    <Chip key={k} active={kind === k} onClick={() => setKind(kind === k ? null : k)}>
                      {KIND_LABELS[k]} <Count n={kindCounts.get(k) ?? 0} />
                    </Chip>
                  ))}
                </div>
              )}
            </div>

            {!usageReady ? (
              // Em "uso recente", desenhar antes das datas chegarem faria os cartões trocarem
              // de lugar debaixo do cursor um instante depois.
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-busy>
                {entries.slice(0, 6).map((e) => (
                  <Skeleton key={e.agent.agent_type} className="h-56 rounded-xl" />
                ))}
              </div>
            ) : filtered.length === 0 ? (
              <Card>
                <EmptyState
                  icon={Search}
                  title="Nenhum agente encontrado"
                  description={
                    query.trim() ? `Nada corresponde a “${query}”.` : "Nenhum agente deste tipo."
                  }
                  action={
                    <Button
                      variant="outline"
                      onClick={() => {
                        setQuery("");
                        setKind(null);
                      }}
                    >
                      <X /> Limpar filtros
                    </Button>
                  }
                />
              </Card>
            ) : (
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {filtered.map((entry) => (
                  <AgentCard
                    key={entry.agent.agent_type}
                    entry={entry}
                    conversations={
                      (conversationCounts.get(entry.agent.agent_type) ?? 0) +
                      (entry.draft ? (conversationCounts.get(entry.draft.agent_type) ?? 0) : 0)
                    }
                    lastUsed={entryLastUsed(entry, lastUsed)}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </PageBody>
    </>
  );
}

function Count({ n }: { n: number }) {
  return <span className="tabular-nums text-muted-foreground">{n}</span>;
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "inline-flex h-8 items-center gap-1.5 rounded-full border px-3 text-xs transition-colors",
        active
          ? "border-primary bg-primary-soft font-medium text-foreground"
          : "border-border text-muted-foreground hover:text-foreground"
      )}
    >
      {children}
    </button>
  );
}

/** Data curta ("há 3 dias") com a data completa no title. */
function ShortTime({ date }: { date: string }) {
  return (
    <time dateTime={toDate(date).toISOString()} title={formatDateTime(date)} suppressHydrationWarning>
      {formatRelativeShort(date)}
    </time>
  );
}

function AgentCard({
  entry,
  conversations,
  lastUsed,
}: {
  entry: AgentEntry;
  conversations: number;
  lastUsed: string | null;
}) {
  const { agent, draft } = entry;
  const slug = encodeURIComponent(agent.agent_type);
  const isAnalysis = agent.kind === "analysis";
  return (
    <Card className="group relative flex flex-col transition-[border-color,box-shadow] hover:border-ring/40 hover:shadow-md">
      <div className="flex items-start gap-3 p-5 pb-3">
        <AgentAvatar agentType={agent.agent_type} name={agent.name} />
        <div className="min-w-0 flex-1">
          <h3 className="line-clamp-2 break-words text-sm font-semibold leading-snug" title={agent.name}>
            <Link
              href={`/agents/${slug}`}
              className="outline-none after:absolute after:inset-0 after:rounded-xl focus-visible:after:ring-2 focus-visible:after:ring-ring"
            >
              {agent.name}
            </Link>
          </h3>
          <p className="truncate font-mono text-xs text-muted-foreground" title={agent.agent_type}>
            {agent.agent_type}
          </p>
        </div>
        {agent.is_seed && <Badge variant="secondary">sistema</Badge>}
        {/* Draft sem o agente de produção: o cartão é dele mesmo. */}
        {draftOf(agent.agent_type) && <Badge variant="warning">draft</Badge>}
      </div>
      <p className="line-clamp-2 min-h-10 px-5 text-[13px] text-muted-foreground">{agent.instructions.join(" ")}</p>
      <div className="flex flex-wrap gap-1.5 px-5 pb-4 pt-3">
        <Badge variant="outline">{KIND_LABELS[agent.kind] ?? agent.kind}</Badge>
        <Badge variant="outline" className="max-w-full truncate">
          {modelLabel(agent.model_id)}
        </Badge>
        {/* Analista é one-shot: não monta memória nenhuma. */}
        {!isAnalysis && <Badge variant="outline">{memoryLabel(agent)}</Badge>}
        <Badge variant="outline">Prompt v{agent.prompt_version}</Badge>
      </div>
      {draft && <DraftRow prod={agent} draft={draft} />}
      <CardFooter className="mt-auto justify-between gap-2">
        <span className="min-w-0 text-xs text-muted-foreground">
          {lastUsed ? (
            <>
              Usado <ShortTime date={lastUsed} />
            </>
          ) : (
            <span title={`Nenhuma execução nos últimos ${USAGE_WINDOW_DAYS} dias`}>Sem uso recente</span>
          )}
          {!isAnalysis && conversations > 0 && <> · {pluralize(conversations, "conversa", "conversas")}</>}
        </span>
        {isAnalysis ? (
          <Link
            href="/analyze"
            className={buttonVariants({ variant: "ghost", size: "sm", className: "relative z-10 -mr-2 shrink-0" })}
          >
            <FileSearch /> Analisar
          </Link>
        ) : (
          <Link
            href={`/chat?agent=${slug}`}
            className={buttonVariants({ variant: "ghost", size: "sm", className: "relative z-10 -mr-2 shrink-0" })}
          >
            <MessageSquare /> Conversar
          </Link>
        )}
      </CardFooter>
    </Card>
  );
}

/** O draft do agente, dentro do cartão de produção: o que difere e o atalho para promover. */
function DraftRow({ prod, draft }: { prod: AgentDefinition; draft: AgentDefinition }) {
  const [promoteOpen, setPromoteOpen] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const differences = draftDifferences(prod, draft);
  const draftSlug = encodeURIComponent(draft.agent_type);
  return (
    <div className="relative z-10 mx-5 mb-4 flex flex-col gap-2 rounded-lg border border-dashed border-border bg-muted/40 px-3 py-2.5">
      <div className="flex min-w-0 items-center gap-2 text-xs">
        <Badge variant="warning">draft</Badge>
        <Link
          href={`/agents/${draftSlug}`}
          className="min-w-0 truncate font-mono text-muted-foreground hover:text-foreground hover:underline"
          title={draft.agent_type}
        >
          {draft.agent_type}
        </Link>
      </div>
      <p className="text-xs text-muted-foreground">
        {differences.length === 0 ? (
          "Igual à produção"
        ) : (
          <>
            Difere em <span className="text-foreground">{differences.join(", ")}</span>
          </>
        )}
        {" · editado "}
        <ShortTime date={draft.updated_at} />
      </p>
      <div className="flex flex-wrap gap-1.5">
        <Button variant="outline" size="sm" onClick={() => setCompareOpen(true)}>
          <GitCompare /> Comparar
        </Button>
        <Button variant="outline" size="sm" onClick={() => setPromoteOpen(true)}>
          <ArrowUpRight /> Promover
        </Button>
      </div>
      <CompareDraftDialog
        prod={prod}
        draft={draft}
        open={compareOpen}
        onOpenChange={setCompareOpen}
        onPromote={() => setPromoteOpen(true)}
      />
      <PromoteDialog agentType={draft.agent_type} open={promoteOpen} onOpenChange={setPromoteOpen} />
    </div>
  );
}
