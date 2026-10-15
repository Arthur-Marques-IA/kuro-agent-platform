"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  CircleAlert,
  CircleCheck,
  CircleSlash,
  ExternalLink,
  Globe,
  LoaderCircle,
  RefreshCw,
  Sparkles,
  Trash2,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { errorMessage, requestJson } from "@/lib/http";
import { formatCost, formatNumber, pluralize } from "@/lib/format";
import type { KnowledgeSource, KnowledgeSourceDetail, KnowledgeSourcePage, SourceStatus } from "@/lib/types";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { RelativeTime, SectionHeading, Spinner } from "@/components/ui/primitives";
import { useConfirm } from "@/components/ui/confirm";
import { useToast } from "@/components/ui/toast";

const POLL_INTERVAL_MS = 2500;

const STATUS: Record<SourceStatus, { label: string; variant: BadgeVariant }> = {
  pending: { label: "Na fila", variant: "secondary" },
  running: { label: "Sincronizando", variant: "secondary" },
  ready: { label: "Sincronizado", variant: "success" },
  partial: { label: "Parcial", variant: "warning" },
  failed: { label: "Falhou", variant: "destructive" },
};

const MODE_LABEL = {
  crawl: "site",
  sitemap: "sitemap",
  page: "página",
} as const;
const REFRESH_LABEL = {
  never: "manual",
  daily: "diário",
  weekly: "semanal",
  monthly: "mensal",
} as const;

function isActive(source: KnowledgeSource) {
  return source.status === "running" || source.status === "pending";
}

/** Sites que alimentam a coleção: status da sincronização, páginas e ações. */
export function SourcesPanel({
  collection,
  reloadKey,
  onChanged,
}: {
  collection: string;
  /** Muda quando a página recarrega (ex.: um site acabou de ser cadastrado). */
  reloadKey: number;
  onChanged: () => void;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const [tick, setTick] = useState(0);
  const [result, setResult] = useState<{
    collection: string;
    sources: KnowledgeSource[] | null;
    error: string | null;
  } | null>(null);
  const [expanded, setExpanded] = useState<{ collection: string; id: string } | null>(null);
  const wasActive = useRef(false);
  // `onChanged` muda a cada render de quem chama; pela ref ele não refaz a busca.
  const onChangedRef = useRef(onChanged);
  useEffect(() => {
    onChangedRef.current = onChanged;
  });
  const refresh = () => setTick((t) => t + 1);

  useEffect(() => {
    let cancelled = false;
    requestJson<KnowledgeSource[]>(`/api/collections/${encodeURIComponent(collection)}/sources`, {
      fallbackError: "Falha ao carregar os sites",
    })
      .then((rows) => {
        if (cancelled) return;
        setResult({ collection, sources: rows, error: null });
        // Uma sincronização acabou de terminar: a lista de documentos mudou.
        const active = rows.some(isActive);
        if (wasActive.current && !active) onChangedRef.current();
        wasActive.current = active;
      })
      .catch((err) => {
        if (cancelled) return;
        setResult((prev) => ({
          collection,
          sources: prev?.collection === collection ? prev.sources : null,
          error: errorMessage(err),
        }));
      });
    return () => {
      cancelled = true;
    };
  }, [collection, reloadKey, tick]);

  const current = result?.collection === collection ? result : null;
  const sources = current?.sources ?? null;
  const error = current?.error ?? null;
  const openId = expanded?.collection === collection ? expanded.id : null;

  const polling = Boolean(sources?.some(isActive));
  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => setTick((t) => t + 1), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [polling]);

  async function sync(source: KnowledgeSource) {
    try {
      await requestJson(
        `/api/collections/${encodeURIComponent(collection)}/sources/${encodeURIComponent(source.id)}/sync`,
        { method: "POST", fallbackError: "Falha ao sincronizar" }
      );
      toast({
        title: "Sincronização iniciada",
        description: "Só as páginas que mudaram são reprocessadas.",
        variant: "success",
      });
      refresh();
    } catch (err) {
      toast({
        title: "Não foi possível sincronizar",
        description: errorMessage(err),
        variant: "error",
      });
    }
  }

  async function remove(source: KnowledgeSource) {
    const ok = await confirm({
      title: "Remover site?",
      description: `${source.url} e as ${pluralize(source.pages_indexed, "página indexada", "páginas indexadas")} saem da coleção. Os agentes deixam de encontrá-las.`,
      confirmLabel: "Remover",
      destructive: true,
    });
    if (!ok) return;
    try {
      await requestJson(`/api/collections/${encodeURIComponent(collection)}/sources/${encodeURIComponent(source.id)}`, {
        method: "DELETE",
        fallbackError: "Falha ao remover o site",
      });
      toast({ title: "Site removido", variant: "success" });
      refresh();
      onChanged();
    } catch (err) {
      toast({
        title: "Não foi possível remover",
        description: errorMessage(err),
        variant: "error",
      });
    }
  }

  if (sources === null && !error) return null;
  if (sources !== null && sources.length === 0) return null;

  return (
    <section className="flex min-w-0 flex-col gap-3">
      <SectionHeading
        title={
          <span className="flex items-center gap-2">
            <Globe className="size-4 text-muted-foreground" aria-hidden /> Sites
          </span>
        }
        description={polling ? "Sincronizando — atualiza sozinho." : "Coletados, formatados e indexados pelo Kuro."}
      />
      {error && <p className="text-[13px] text-destructive">{error}</p>}
      <Card className="divide-y divide-border overflow-hidden">
        {(sources ?? []).map((source) => (
          <Fragment key={source.id}>
            <SourceRow
              source={source}
              open={openId === source.id}
              onToggle={() => setExpanded(openId === source.id ? null : { collection, id: source.id })}
              onSync={() => void sync(source)}
              onRemove={() => void remove(source)}
            />
            {openId === source.id && <SourcePages collection={collection} source={source} />}
          </Fragment>
        ))}
      </Card>
    </section>
  );
}

function SourceRow({
  source,
  open,
  onToggle,
  onSync,
  onRemove,
}: {
  source: KnowledgeSource;
  open: boolean;
  onToggle: () => void;
  onSync: () => void;
  onRemove: () => void;
}) {
  const status = STATUS[source.status] ?? STATUS.pending;
  const progress = source.progress ?? {};
  const active = isActive(source);
  const pct =
    active && progress.discovered ? Math.round(((progress.done ?? 0) / Math.max(progress.discovered, 1)) * 100) : null;
  const tokens = (progress.format_input_tokens ?? 0) + (progress.format_output_tokens ?? 0);

  return (
    <div className="flex flex-col gap-2 px-4 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <ChevronDown
            className={cn("size-4 shrink-0 text-muted-foreground transition-transform", !open && "-rotate-90")}
          />
          <span className="min-w-0">
            <span className="block truncate text-sm font-medium">{source.label || source.url}</span>
            <span className="block truncate text-xs text-muted-foreground">
              {source.label ? `${source.url} · ` : ""}
              {MODE_LABEL[source.mode]} · atualização {REFRESH_LABEL[source.refresh]}
              {source.options.format === false ? " · sem formatador" : ""}
            </span>
          </span>
        </button>
        <Badge variant={status.variant} className="shrink-0">
          {active && <LoaderCircle className="size-3 animate-spin" aria-hidden />}
          {status.label}
        </Badge>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onSync}
            disabled={active}
            aria-label="Sincronizar agora"
            title="Sincronizar agora"
          >
            <RefreshCw />
          </Button>
          <Button variant="ghost" size="icon-sm" onClick={onRemove} aria-label="Remover site" title="Remover site">
            <Trash2 />
          </Button>
        </div>
      </div>

      {pct !== null && (
        <div className="flex items-center gap-3 pl-6">
          <span className="relative block h-1.5 flex-1 rounded-full bg-muted" aria-hidden>
            <span
              className="absolute inset-y-0 left-0 rounded-full bg-primary transition-[width]"
              style={{ width: `${pct}%` }}
            />
          </span>
          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
            {progress.done ?? 0}/{progress.discovered} páginas
          </span>
        </div>
      )}

      <div className="flex flex-wrap gap-x-4 gap-y-1 pl-6 text-xs text-muted-foreground">
        <span>{pluralize(source.pages_indexed, "página indexada", "páginas indexadas")}</span>
        {source.pages_failed > 0 && <span className="text-destructive">{source.pages_failed} com falha</span>}
        {!active && progress.indexed != null && (
          <span>
            última: {progress.indexed} nova(s)/alterada(s), {progress.unchanged ?? 0} igual(is)
            {progress.removed ? `, ${progress.removed} removida(s)` : ""}
          </span>
        )}
        {tokens > 0 && (
          <span title="Tokens gastos pelo agente formatador na última sincronização (somados ao custo dos Logs)">
            {formatNumber(tokens)} tokens de formatação
            {progress.format_cost_usd != null && ` · ${formatCost(progress.format_cost_usd)}`}
          </span>
        )}
        {source.last_synced_at && (
          <span>
            sincronizado <RelativeTime date={source.last_synced_at} />
          </span>
        )}
        {source.next_sync_at && !active && (
          <span>
            próxima <RelativeTime date={source.next_sync_at} />
          </span>
        )}
      </div>
      {source.status_message && <p className="pl-6 text-xs text-muted-foreground">{source.status_message}</p>}
    </div>
  );
}

const PAGE_STATUS: Record<string, { icon: typeof CircleCheck; className: string; label: string }> = {
  indexed: { icon: CircleCheck, className: "text-success", label: "Indexada" },
  failed: { icon: CircleAlert, className: "text-destructive", label: "Falhou" },
  skipped: {
    icon: CircleSlash,
    className: "text-muted-foreground",
    label: "Ignorada",
  },
};

function SourcePages({ collection, source }: { collection: string; source: KnowledgeSource }) {
  const [detail, setDetail] = useState<KnowledgeSourceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Recarrega quando a sincronização avança ou termina.
  const version = `${source.status}|${source.updated_at ?? ""}|${source.progress?.done ?? 0}`;

  useEffect(() => {
    let cancelled = false;
    requestJson<KnowledgeSourceDetail>(
      `/api/collections/${encodeURIComponent(collection)}/sources/${encodeURIComponent(source.id)}`,
      { fallbackError: "Falha ao carregar as páginas" }
    )
      .then((d) => !cancelled && setDetail(d))
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [collection, source.id, version]);

  if (error) return <p className="bg-surface px-10 py-3 text-[13px] text-destructive">{error}</p>;
  if (!detail)
    return (
      <div className="flex items-center gap-2 bg-surface px-10 py-3 text-[13px] text-muted-foreground">
        <Spinner /> Carregando páginas…
      </div>
    );
  if (detail.pages.length === 0)
    return <p className="bg-surface px-10 py-3 text-[13px] text-muted-foreground">Nenhuma página coletada ainda.</p>;

  return (
    <ul className="scrollbar-thin max-h-80 divide-y divide-border overflow-auto bg-surface">
      {detail.pages.map((page) => (
        <PageRow key={page.url} page={page} />
      ))}
    </ul>
  );
}

function PageRow({ page }: { page: KnowledgeSourcePage }) {
  const meta = PAGE_STATUS[page.status] ?? PAGE_STATUS.skipped;
  const Icon = meta.icon;
  return (
    <li className="flex min-w-0 items-start gap-2.5 px-4 py-2 pl-10 text-[13px]">
      <Icon className={cn("mt-0.5 size-3.5 shrink-0", meta.className)} aria-label={meta.label} />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-medium">{page.title || page.url}</span>
          <a
            href={page.url}
            target="_blank"
            rel="noreferrer noopener"
            className="shrink-0 text-muted-foreground hover:text-foreground"
            aria-label="Abrir página"
          >
            <ExternalLink className="size-3" />
          </a>
        </div>
        <p className="truncate text-xs text-muted-foreground">{page.url}</p>
        {page.error && (
          <p className={cn("text-xs", page.status === "indexed" ? "text-muted-foreground" : "text-destructive")}>
            {page.status === "indexed" ? "Texto extraído usado: " : ""}
            {page.error}
          </p>
        )}
      </div>
      {page.status === "indexed" && (
        <span className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
          {page.formatted === "agent" && (
            <span title="Formatada pelo agente" className="flex items-center gap-1">
              <Sparkles className="size-3" aria-hidden /> formatada
            </span>
          )}
          {pluralize(page.chunks, "trecho", "trechos")}
        </span>
      )}
    </li>
  );
}
