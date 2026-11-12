"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Activity, Download, RefreshCw, Search } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  RUN_SORTS,
  apiSearchParams,
  logsHref,
  parseLogsQuery,
  searchParamsRecord,
  sharedLogsQuery,
  type LogsPath,
  type LogsQuery
} from "@/lib/logs-query";
import { toCsv } from "@/lib/csv";
import {
  AUTO_REFRESH_OPTIONS,
  AUTO_REFRESH_STORAGE_KEY,
  RUNS_EXPORT_LIMIT,
  RUNS_EXPORT_PAGE,
  RUN_CSV_COLUMNS,
  SESSIONS_EXPORT_LIMIT,
  SESSION_CSV_COLUMNS,
  collectPages,
  exportFileName,
  parseAutoRefresh,
  testsProbeParams,
} from "@/lib/logs-lists";
import { formatNumber } from "@/lib/format";
import { errorMessage, requestJson } from "@/lib/http";
import { useLocalStorage } from "@/lib/use-local-storage";
import type { LogSessionPage, RunPage } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { EmptyState, Spinner } from "@/components/ui/primitives";
import { Select } from "@/components/ui/select";
import { useToast } from "@/components/ui/toast";
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

// -- Atualização automática ------------------------------------------------------

/**
 * A preferência (desligada, 10, 30 ou 60 s), lembrada no navegador, e um contador que sobe a cada volta.
 * Com a aba oculta o relógio para; ao voltar, atualiza na hora se já passou o intervalo. Quem consome o
 * contador atualiza em silêncio: sem remontar a lista, a rolagem e o foco ficam onde estavam.
 */
function useAutoRefresh() {
  const [stored, setStored] = useLocalStorage<string>(AUTO_REFRESH_STORAGE_KEY, "0");
  const seconds = parseAutoRefresh(stored);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!seconds) return;
    const every = seconds * 1000;
    let last = Date.now();
    let timer: number | undefined;
    const fire = () => {
      last = Date.now();
      setTick((t) => t + 1);
    };
    const stop = () => {
      if (timer !== undefined) window.clearInterval(timer);
      timer = undefined;
    };
    const start = () => {
      stop();
      timer = window.setInterval(fire, every);
    };
    const onVisibility = () => {
      if (document.hidden) return stop();
      if (Date.now() - last >= every) fire();
      start();
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [seconds]);
  return { seconds, setSeconds: (value: number) => setStored(String(parseAutoRefresh(String(value)))), tick };
}

/** Chama `onTick` a cada volta do contador (não na montagem). */
function useOnTick(tick: number, onTick: () => void) {
  const last = useRef(tick);
  const callback = useRef(onTick);
  useEffect(() => {
    callback.current = onTick;
  });
  useEffect(() => {
    if (last.current === tick) return;
    last.current = tick;
    callback.current();
  }, [tick]);
}

type AutoRefresh = ReturnType<typeof useAutoRefresh>;

/**
 * No cabeçalho a partir de `sm`; no celular, na linha dos filtros (no cabeçalho ele espremeria o título
 * e a descrição). As duas cópias leem a mesma preferência.
 */
function AutoRefreshSelect({ auto, className }: { auto: AutoRefresh; className?: string }) {
  return (
    <Select
      aria-label="Atualização automática"
      title="Atualiza a lista sozinha, sem perder a rolagem. Pausa com a aba oculta."
      value={String(auto.seconds)}
      onChange={(e) => auto.setSeconds(Number(e.target.value))}
      wrapperClassName={cn("w-36", className)}
      className="h-10 text-[13px] md:h-8"
    >
      {AUTO_REFRESH_OPTIONS.map((s) => (
        <option key={s} value={s}>
          {s ? `Auto: ${s} s` : "Auto: desligada"}
        </option>
      ))}
    </Select>
  );
}

function LogsShell({
  current,
  query,
  description,
  onReload,
  auto,
  loading = false,
  children,
}: {
  current: LogsPath;
  query: LogsQuery;
  description: string;
  onReload: () => void;
  auto: AutoRefresh;
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
          <>
            <AutoRefreshSelect auto={auto} className="hidden sm:block" />
            <Button variant="outline" size="sm" className="h-10 md:h-8" onClick={onReload} disabled={loading}>
              <RefreshCw className={loading ? "animate-spin" : undefined} />
              Atualizar
            </Button>
          </>
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
  const auto = useAutoRefresh();
  useOnTick(auto.tick, overview.refresh);

  // Vazio sem os testes: uma sondagem de uma linha diz se há testes no recorte (só neste caso).
  const probeParams = useMemo(() => rangeParams(query.agents, range), [query.agents, range]);
  const onlyTests = useTestsExist(
    probeParams,
    overview.state === "ready" && overview.data?.totals.runs === 0 && !overview.data.include_dry_run && !query.tests
  );

  // Cada número do Panorama leva às execuções que ele conta, com o mesmo período, agentes e testes.
  const runsHref = (drill: Partial<LogsQuery>) => logsHref("/logs/runs", { ...sharedLogsQuery(query), ...drill });

  return (
    <LogsShell
      current="/logs"
      query={query}
      description="Como os agentes foram no período: o que falhou, quanto custou e se a versão nova melhorou."
      onReload={overview.reload}
      auto={auto}
      loading={overview.state === "loading"}
    >
      <div className="flex flex-wrap items-center gap-2">
        <PeriodPicker query={query} onChange={update} />
        <AgentMultiSelect value={query.agents} onChange={(agents) => update({ agents })} />
        <label
          className="inline-flex h-10 items-center gap-2 rounded-lg border border-border px-3 text-[13px] md:h-9"
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
        <AutoRefreshSelect auto={auto} className="sm:hidden" />
      </div>
      <OverviewPanel
        overview={overview}
        runsHref={runsHref}
        emptyAction={onlyTests ? <IncludeTestsButton onClick={() => update({ tests: true })} /> : undefined}
      />
    </LogsShell>
  );
}

// -- "Incluir testes" no estado vazio -------------------------------------------------

/**
 * Há execuções de teste (dry_run) neste recorte? Só pergunta quando `enabled` (a lista sem testes veio
 * vazia), com `limit=1`: as telas com dados não pagam nada por isso.
 */
function useTestsExist(params: Record<string, string | string[]>, enabled: boolean): boolean {
  const key = JSON.stringify(params);
  const [found, setFound] = useState<{ key: string; value: boolean } | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let active = true;
    requestJson<RunPage>(`/api/observability/runs?${apiSearchParams(testsProbeParams(params))}`, { fallbackError: "" })
      .then((page) => {
        if (active) setFound({ key, value: page.items.length > 0 });
      })
      .catch(() => {}); // sem resposta, fica o vazio comum
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled]);
  return enabled && found?.key === key && found.value;
}

function IncludeTestsButton({ onClick }: { onClick: () => void }) {
  return (
    <Button variant="outline" size="sm" className="h-10 md:h-8" onClick={onClick}>
      Incluir testes
    </Button>
  );
}

// -- Exportar CSV ----------------------------------------------------------------

/** Baixa o texto como arquivo, sem passar pelo servidor. */
function downloadText(text: string, fileName: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * Exporta o recorte atual, com todos os filtros, buscado no servidor (não só o que está na tela):
 * execuções página a página até 5.000 linhas; sessões numa chamada só, até 200 (a API não pagina
 * sessões). O aviso no fim diz quantas linhas saíram e se o teto cortou o resto.
 */
function ExportCsvButton({ kind, params }: { kind: "runs" | "sessions"; params: Record<string, string | string[]> }) {
  const toast = useToast();
  const [progress, setProgress] = useState<number | null>(null);
  const cap = kind === "runs" ? RUNS_EXPORT_LIMIT : SESSIONS_EXPORT_LIMIT;
  const noun = kind === "runs" ? "execuções" : "sessões";

  async function exportCsv() {
    setProgress(0);
    try {
      let csv: string;
      let count: number;
      let truncated: boolean;
      if (kind === "runs") {
        // `_seed` só serve para a lista recarregar; a amostra exporta as linhas sorteadas, sem paginar.
        const apiParams = Object.fromEntries(Object.entries(params).filter(([key]) => key !== "_seed"));
        const result = await collectPages(
          (cursor) =>
            requestJson<RunPage>(
              `/api/observability/runs?${apiSearchParams({ ...apiParams, limit: String(RUNS_EXPORT_PAGE), ...(cursor ? { cursor } : {}) })}`,
              { fallbackError: "Falha ao buscar as execuções" }
            ),
          { max: RUNS_EXPORT_LIMIT, pageSize: RUNS_EXPORT_PAGE, onProgress: setProgress }
        );
        csv = toCsv(RUN_CSV_COLUMNS, result.items);
        count = result.items.length;
        truncated = result.truncated;
      } else {
        const page = await requestJson<LogSessionPage>(
          `/api/observability/sessions?${apiSearchParams({ ...params, limit: String(SESSIONS_EXPORT_LIMIT) })}`,
          { fallbackError: "Falha ao buscar as sessões" }
        );
        csv = toCsv(SESSION_CSV_COLUMNS, page.items);
        count = page.items.length;
        truncated = count >= SESSIONS_EXPORT_LIMIT;
      }
      downloadText(csv, exportFileName(kind === "runs" ? "execucoes" : "sessoes", new Date()));
      toast({
        title: count === 1 ? `1 ${noun.replace(/ões$/, "ão")} exportada` : `${formatNumber(count)} ${noun} exportadas`,
        description: truncated
          ? `Parou no teto de ${formatNumber(cap)} (as mais recentes). Estreite o período ou os filtros para pegar o resto.`
          : "Com os filtros ativos.",
        variant: "success",
      });
    } catch (err) {
      toast({ title: "Não foi possível exportar", description: errorMessage(err), variant: "error" });
    } finally {
      setProgress(null);
    }
  }

  const help =
    kind === "runs"
      ? `Baixa as execuções do recorte atual (com todos os filtros), buscadas no servidor até ${formatNumber(cap)} linhas — não só as que estão na tela.`
      : `Baixa as sessões do recorte atual (com todos os filtros), até ${formatNumber(cap)} de atividade mais recente.`;

  return (
    <Button
      variant="outline"
      size="sm"
      className="h-10 md:h-9"
      onClick={() => void exportCsv()}
      disabled={progress != null}
      title={help}
      aria-label={`Exportar CSV: ${help}`}
    >
      {progress != null ? <Spinner /> : <Download />}
      {progress != null ? (progress ? `Exportando… ${formatNumber(progress)}` : "Exportando…") : `Exportar CSV (até ${formatNumber(cap)})`}
    </Button>
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
  // A busca mudou por fora (link da paleta, "Limpar filtros"): o campo acompanha. Enquanto a pessoa
  // digita, o texto aparado bate com o filtro e o campo fica como está (com o espaço do fim).
  const [seenSearch, setSeenSearch] = useState(query.search);
  if (seenSearch !== query.search) {
    setSeenSearch(query.search);
    if (searchText.trim() !== query.search) setSearchText(query.search);
  }
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
  // "Atualizar" remonta as listas e os números do filtro fixo, que buscam de novo. A atualização
  // automática não remonta: as listas recebem o `tick` e buscam em silêncio.
  const [nonce, setNonce] = useState(0);
  const auto = useAutoRefresh();

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
      auto={auto}
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
            className="h-10 pl-9 md:h-9"
          />
        </div>
        {filtered && (
          <Button
            variant="ghost"
            size="sm"
            className="h-10 md:h-8"
            onClick={() => {
              update({ agents: [], statuses: [], search: "", version: null });
              setSearchText("");
            }}
          >
            Limpar filtros
          </Button>
        )}
        <AutoRefreshSelect auto={auto} className="sm:hidden" />
        {range.ready && (
          <div className="sm:ml-auto">
            <ExportCsvButton kind={view === "sessions" ? "sessions" : "runs"} params={view === "sessions" ? params : runsParams} />
          </div>
        )}
      </div>

      {/* Com um usuário ou uma sessão fixos, os números são deles: o Panorama não recorta por isso. */}
      {range.ready && (query.userId || params.session_id) && <StatsPanel key={`stats-${nonce}`} params={params} />}

      {view === "sessions" ? (
        range.ready && <SessionsTable key={`sessions-${nonce}`} params={params} refreshTick={auto.tick} />
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
              refreshTick={auto.tick}
              includeTests={query.tests}
              onIncludeTests={() => update({ tests: true })}
              emptyTitle={filtered || fixed.length || reviewing ? "Nenhuma execução com esses filtros" : "Nenhuma execução registrada"}
            />
          )}
        </>
      )}
    </LogsShell>
  );
}

function RunsList({
  params,
  emptyTitle,
  refreshTick,
  includeTests,
  onIncludeTests,
}: {
  params: Record<string, string | string[]>;
  emptyTitle: string;
  refreshTick: number;
  includeTests: boolean;
  onIncludeTests: () => void;
}) {
  const pager = useRunsPager(params);
  useOnTick(refreshTick, () => void pager.refresh());
  // Vazio de verdade (sem página seguinte) e sem os testes: há testes neste recorte?
  const onlyTests = useTestsExist(
    params,
    !includeTests && pager.state === "ready" && pager.items.length === 0 && !pager.cursor && !("sample" in params)
  );
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
      emptyDescription={
        onlyTests
          ? "Só há execuções de teste (Playground, dry_run) neste recorte, e elas ficam de fora por padrão."
          : undefined
      }
      emptyAction={onlyTests ? <IncludeTestsButton onClick={onIncludeTests} /> : undefined}
      showAgentColumn
    />
  );
}
