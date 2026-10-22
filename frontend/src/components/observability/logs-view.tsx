"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Activity, RefreshCw, Search } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  RUN_SORTS,
  logsHref,
  parseLogsQuery,
  searchParamsRecord,
  sharedLogsQuery,
  type LogsPath,
  type LogsQuery
} from "@/lib/logs-query";
import { requestJson } from "@/lib/http";
import type { RunPage } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/primitives";
import { useLogsRange } from "@/components/observability/period-filter";
import { AgentMultiSelect, FixedFilter, PeriodPicker, StatusMultiSelect } from "@/components/observability/logs-filters";
import { RunsTable, useRunsPager } from "@/components/observability/runs-table";
import { SessionsTable } from "@/components/observability/sessions-table";
import { OverviewPanel, useOverview } from "@/components/observability/overview-panel";
import { ReviewFilters, reviewParams, type ReviewState } from "@/components/observability/review-filters";
import { StatsPanel } from "@/components/observability/stats-panel";
import { RagFilterBar } from "@/components/observability/rag-filters";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";

/**
 * Logs da plataforma — o substituto interno da UI do Langfuse — em duas partes com propósitos
 * diferentes: o **Panorama** (`/logs`) responde "como está?" com números agregados do período, e
 * as listas de **Sessões** (`/logs/sessions`), **Execuções** (`/logs/runs`) e **RAG** (`/logs/rag`,
 * as execuções que buscaram na base) servem para investigar um caso. O recorte (período, agente, filtros) fica na URL: os links do Panorama para as listas
 * são só URLs, e recarregar ou compartilhar a página volta ao mesmo lugar.
 */

/** O recorte da página em estado, espelhado na URL sem navegar (sem ida ao servidor a cada filtro). */
function useLogsQuery(path: LogsPath, initial: LogsQuery) {
  // Ao voltar pelo navegador, o Next remonta a página com o `initial` da primeira carga, e os filtros
  // mudados depois (que só estão na URL) se perderiam: aí vale a URL. Num link, a página nova monta
  // antes de a URL trocar — por isso só quando a URL já é desta rota.
  const [query, setQuery] = useState(() =>
    typeof window !== "undefined" && window.location.pathname === path
      ? parseLogsQuery(searchParamsRecord(new URLSearchParams(window.location.search)))
      : initial
  );
  // Um link para esta mesma rota com outro recorte (a paleta de comandos, por exemplo) não remonta.
  const initialKey = JSON.stringify(initial);
  const [seenKey, setSeenKey] = useState(initialKey);
  if (seenKey !== initialKey) {
    setSeenKey(initialKey);
    setQuery(initial);
  }
  useEffect(() => {
    window.history.replaceState(null, "", logsHref(path, query));
  }, [path, query]);
  const update = (patch: Partial<LogsQuery>) => setQuery((q) => ({ ...q, ...patch }));
  return [query, update] as const;
}

/** As abas dos Logs são rotas: o período e o agente vão junto de uma para a outra. */
function LogsNav({ current, query }: { current: LogsPath; query: LogsQuery }) {
  const shared = sharedLogsQuery(query);
  const tabs: { path: LogsPath; label: string; query: Partial<LogsQuery> }[] = [
    { path: "/logs", label: "Panorama", query: shared },
    { path: "/logs/sessions", label: "Sessões", query: { ...shared, userId: query.userId } },
    {
      path: "/logs/runs",
      label: "Execuções",
      query: { ...shared, userId: query.userId, sessionId: query.sessionId, version: query.version },
    },
    { path: "/logs/rag", label: "RAG", query: { ...shared, rag: query.rag, collection: query.collection } },
  ];
  return (
    <nav aria-label="Seções dos Logs" className="scrollbar-thin flex items-center gap-6 overflow-x-auto">
      {tabs.map((tab) => {
        const active = tab.path === current;
        return (
          <Link
            key={tab.path}
            href={logsHref(tab.path, tab.query)}
            aria-current={active ? "page" : undefined}
            className={cn(
              "inline-flex h-10 shrink-0 items-center border-b-2 px-0.5 text-sm font-medium whitespace-nowrap transition-colors",
              "outline-none focus-visible:ring-2 focus-visible:ring-ring",
              active ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}

function LogsShell({
  current,
  query,
  description,
  onReload,
  loading = false,
  children,
}: {
  current: LogsPath;
  query: LogsQuery;
  description: string;
  onReload: () => void;
  loading?: boolean;
  children: ReactNode;
}) {
  const { observability } = useWorkspace();

  if (!observability.enabled) {
    return (
      <>
        <PageHeader title="Logs" />
        <PageBody>
          <Card>
            <EmptyState
              icon={Activity}
              title="Logs desligados"
              description="O agent-service está com TRACE_STORE_BACKEND=langfuse e o Langfuse desligado. Volte para TRACE_STORE_BACKEND=db ou ligue o Langfuse."
            />
          </Card>
        </PageBody>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Logs"
        description={description}
        actions={
          <Button variant="outline" size="sm" onClick={onReload} disabled={loading}>
            <RefreshCw className={loading ? "animate-spin" : undefined} />
            Atualizar
          </Button>
        }
        tabs={<LogsNav current={current} query={query} />}
      />
      <PageBody className="flex flex-col gap-4">{children}</PageBody>
    </>
  );
}

// -- Panorama --------------------------------------------------------------------

/** Os parâmetros do recorte comum (período, agentes) na forma da API. */
function rangeParams(agents: string[], range: { since: string | null; until: string | null }) {
  const p: Record<string, string | string[]> = {};
  if (agents.length) p.agent_type = agents;
  if (range.since) p.since = range.since;
  if (range.until) p.until = range.until;
  return p;
}

export function LogsOverviewView({ initial }: { initial: LogsQuery }) {
  const [query, update] = useLogsQuery("/logs", initial);
  const range = useLogsRange(query);

  const overviewParams = useMemo(() => {
    const p = rangeParams(query.agents, range);
    if (query.tests) p.include_dry_run = "true";
    return p;
  }, [query.agents, range, query.tests]);
  const overview = useOverview(overviewParams, range.ready);

  // Cada número do Panorama leva às execuções que ele conta, com o mesmo período, agentes e testes.
  const runsHref = (drill: Partial<LogsQuery>) => logsHref("/logs/runs", { ...sharedLogsQuery(query), ...drill });

  return (
    <LogsShell
      current="/logs"
      query={query}
      description="Como os agentes foram no período: o que falhou, quanto custou e se a versão nova melhorou."
      onReload={overview.reload}
      loading={overview.state === "loading"}
    >
      <div className="flex flex-wrap items-center gap-2">
        <PeriodPicker query={query} onChange={update} />
        <AgentMultiSelect value={query.agents} onChange={(agents) => update({ agents })} />
        <label
          className="inline-flex h-9 items-center gap-2 rounded-lg border border-border px-3 text-[13px]"
          title="Execuções do Playground e de testes com dry_run. Ficam fora das métricas por padrão."
        >
          <input
            type="checkbox"
            checked={query.tests}
            onChange={(e) => update({ tests: e.target.checked })}
            className="accent-primary"
          />
          Incluir testes
        </label>
      </div>
      <OverviewPanel overview={overview} runsHref={runsHref} />
    </LogsShell>
  );
}

// -- Sessões e execuções ---------------------------------------------------------

/** run_id (UUID) ou trace_id (32 hex) inteiro. */
const RUN_ID_PATTERN = /^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})$/i;

export function LogsExplorerView({ view, initial }: { view: "sessions" | "runs" | "rag"; initial: LogsQuery }) {
  const path = view === "sessions" ? "/logs/sessions" : view === "rag" ? "/logs/rag" : "/logs/runs";
  const [query, update] = useLogsQuery(path, initial);
  const range = useLogsRange(query);
  // O texto digitado só vira filtro (URL e chamada à API) depois de uma pausa.
  const [searchText, setSearchText] = useState(query.search);
  useEffect(() => {
    const timer = window.setTimeout(() => update({ search: searchText.trim() }), 300);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchText]);
  // Um run_id ou trace_id colado inteiro abre a execução direto, mesmo que seja teste.
  const router = useRouter();
  useEffect(() => {
    const term = query.search.trim();
    if (!RUN_ID_PATTERN.test(term)) return;
    let active = true;
    const params = new URLSearchParams({ search: term, limit: "1", include_dry_run: "true" });
    requestJson<RunPage>(`/api/observability/runs?${params}`, { fallbackError: "Falha ao buscar a execução" })
      .then((page) => {
        const hit = page.items.find((run) => run.run_id === term || run.trace_id === term);
        if (active && hit) router.push(`/runs/${encodeURIComponent(hit.run_id)}`);
      })
      .catch(() => {}); // sem execução com esse id, fica a busca comum
    return () => {
      active = false;
    };
  }, [query.search, router]);
  // A amostra aleatória não vai para a URL: recarregar sorteia de novo.
  const [sampleSeed, setSampleSeed] = useState<number | null>(null);
  // "Atualizar" remonta as listas e os números do filtro fixo, que buscam de novo.
  const [nonce, setNonce] = useState(0);

  const params = useMemo(() => {
    const p = rangeParams(query.agents, range);
    if (query.statuses.length) p.status = query.statuses;
    if (query.userId) p.user_id = query.userId;
    // A lista de sessões não recorta por sessão; esse filtro é das execuções.
    if (query.sessionId && view !== "sessions") p.session_id = query.sessionId;
    if (query.search) p.search = query.search;
    return p;
  }, [query.agents, query.statuses, query.userId, query.sessionId, view, range, query.search]);

  const review: ReviewState = {
    complexity: query.complexity,
    toolFailed: query.toolFailed,
    sideEffect: query.sideEffect,
    hideShort: query.hideShort,
    feedbackDown: query.feedbackDown,
    includeTests: query.tests,
    sampleSeed,
  };
  const reviewing = Boolean(
    query.complexity.length || query.toolFailed || query.sideEffect || query.hideShort || query.feedbackDown || sampleSeed
  );
  const runsParams: Record<string, string | string[]> = { ...params, ...reviewParams(review) };
  if (query.version != null) runsParams.agent_version = String(query.version);
  // Na aba RAG, o recorte (buscou, busca vazia, trecho marcado) e a collection.
  const ragBase: Record<string, string | string[]> = { ...params, include_dry_run: query.tests ? "true" : "false" };
  if (query.collection) ragBase.knowledge_collection = query.collection;
  if (view === "rag") {
    runsParams.rag = query.rag;
    if (query.collection) runsParams.knowledge_collection = query.collection;
  }
  // A amostra é aleatória: com ela, a ordenação não se aplica (e o chip diz isso).
  if (query.sort && sampleSeed == null) runsParams.sort = query.sort;

  const filtered = Boolean(query.agents.length || query.statuses.length || query.search);
  const sortLabel = RUN_SORTS.find((s) => s.value === query.sort)?.label;
  const fixed = [
    query.userId && { label: "Usuário", value: query.userId, remove: { userId: "" } },
    query.sessionId && view !== "sessions" && { label: "Sessão", value: query.sessionId, remove: { sessionId: "" } },
    query.version != null && view !== "sessions" && { label: "Versão", value: `v${query.version}`, remove: { version: null } },
    sortLabel &&
      view !== "sessions" && {
        label: "Ordem",
        value: sampleSeed != null ? `${sortLabel} (pausada na amostra)` : sortLabel,
        remove: { sort: "" },
      },
  ].filter(Boolean) as { label: string; value: string; remove: Partial<LogsQuery> }[];

  return (
    <LogsShell
      current={path}
      query={query}
      description={
        view === "sessions"
          ? "Conversas de todos os agentes — pelo console, pela API ou por outros módulos. Abra uma para ler a transcrição."
          : view === "rag"
            ? "As execuções que buscaram na base de conhecimento. Abra uma para ver os trechos que o agente leu e marcar os errados."
            : "Cada execução de todos os agentes, com a fila de revisão para achar o que vale olhar. Abra uma para ver o trace."
      }
      onReload={() => setNonce((n) => n + 1)}
    >
      <div className="flex flex-wrap items-center gap-2">
        {fixed.map((f) => (
          <FixedFilter key={f.label} label={f.label} value={f.value} onRemove={() => update(f.remove)} />
        ))}
        <PeriodPicker query={query} onChange={update} />
        {/* A versão é de um agente: mudar a seleção de agentes tira esse filtro. */}
        <AgentMultiSelect value={query.agents} onChange={(agents) => update({ agents, version: null })} />
        <StatusMultiSelect value={query.statuses} onChange={(statuses) => update({ statuses })} />
        <div className="relative w-full sm:w-64">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <Input
            type="search"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            placeholder="Buscar sessão, usuário, conversa ou run_id…"
            aria-label="Buscar por sessão, usuário ou conversation_id"
            className="pl-9"
          />
        </div>
        {filtered && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              update({ agents: [], statuses: [], search: "", version: null });
              setSearchText("");
            }}
          >
            Limpar filtros
          </Button>
        )}
      </div>

      {/* Com um usuário ou uma sessão fixos, os números são deles: o Panorama não recorta por isso. */}
      {range.ready && (query.userId || params.session_id) && <StatsPanel key={`stats-${nonce}`} params={params} />}

      {view === "sessions" ? (
        range.ready && <SessionsTable key={`sessions-${nonce}`} params={params} />
      ) : (
        <>
          {view === "rag" && range.ready && (
            <RagFilterBar
              key={`rag-${nonce}`}
              value={query.rag}
              collection={query.collection}
              params={ragBase}
              onChange={(rag) => update({ rag })}
              onCollectionChange={(collection) => update({ collection })}
            />
          )}
          <ReviewFilters
            review={review}
            onChange={(next) => {
              setSampleSeed(next.sampleSeed);
              update({
                complexity: next.complexity,
                toolFailed: next.toolFailed,
                sideEffect: next.sideEffect,
                hideShort: next.hideShort,
                feedbackDown: next.feedbackDown,
                tests: next.includeTests,
              });
            }}
          />
          {range.ready && (
            <RunsList
              key={nonce}
              params={runsParams}
              emptyTitle={filtered || fixed.length || reviewing ? "Nenhuma execução com esses filtros" : "Nenhuma execução registrada"}
            />
          )}
        </>
      )}
    </LogsShell>
  );
}

function RunsList({ params, emptyTitle }: { params: Record<string, string | string[]>; emptyTitle: string }) {
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
      emptyTitle={emptyTitle}
      showAgentColumn
    />
  );
}
