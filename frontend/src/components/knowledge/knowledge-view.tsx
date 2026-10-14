"use client";

import { Fragment, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  Ellipsis,
  FileText,
  Globe,
  Library,
  LoaderCircle,
  Plus,
  RotateCcw,
  Trash,
  Trash2,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { errorMessage, requestJson } from "@/lib/http";
import { formatBytes, pluralize, toDate } from "@/lib/format";
import type { ContentStatus, KnowledgeContent, Paginated, Collection } from "@/lib/types";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DropdownMenu, DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { EmptyState, RelativeTime, SectionHeading, Skeleton, Spinner } from "@/components/ui/primitives";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useConfirm } from "@/components/ui/confirm";
import { useToast } from "@/components/ui/toast";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { AddContentDialog } from "@/components/knowledge/add-content-dialog";
import { NewCollectionDialog } from "@/components/knowledge/new-collection-dialog";
import { SearchPanel } from "@/components/knowledge/search-panel";

const PAGE_SIZE = 20;
const POLL_INTERVAL_MS = 3000;
const STALE_PROCESSING_MS = 15 * 60 * 1000;

/** Item "processando" há muito tempo provavelmente travou no backend — não justifica polling contínuo. */
function hasActiveProcessing(page: Paginated<KnowledgeContent>, now: number) {
  return page.data.some((d) => {
    const lastUpdate = d.updated_at ?? d.created_at;
    // Sem timestamp não dá para saber se ainda está andando: não mantém polling por causa dele.
    return d.status === "processing" && lastUpdate != null && now - toDate(lastUpdate).getTime() < STALE_PROCESSING_MS;
  });
}

const STATUS: Record<ContentStatus, { label: string; variant: BadgeVariant; icon: typeof CircleCheck }> = {
  processing: { label: "Processando", variant: "secondary", icon: LoaderCircle },
  completed: { label: "Indexado", variant: "success", icon: CircleCheck },
  partial: { label: "Parcial", variant: "warning", icon: CircleAlert },
  failed: { label: "Falhou", variant: "destructive", icon: CircleAlert },
};

function contentKind(type?: string | null) {
  if (!type || type === "manual" || type.startsWith("text/")) return { label: "Texto", icon: FileText };
  if (type === "url" || type.startsWith("http")) return { label: "URL", icon: Globe };
  if (type.includes("pdf")) return { label: "PDF", icon: FileText };
  return { label: type.split("/").pop()?.toUpperCase() ?? type, icon: FileText };
}

export function KnowledgeView({
  openAddInitially,
  initialCollection,
}: {
  openAddInitially: boolean;
  /** Vem do link "Trechos consultados" do trace. */
  initialCollection?: string;
}) {
  const toast = useToast();
  const router = useRouter();
  const confirm = useConfirm();
  const { collections, backendReachable } = useWorkspace();
  const [collection, setCollection] = useState(
    (initialCollection && collections.some((c) => c.name === initialCollection) ? initialCollection : null) ??
      collections[0]?.name ??
      "general"
  );
  const [newOpen, setNewOpen] = useState(false);
  const current = collections.find((c) => c.name === collection);
  const [addOpen, setAddOpen] = useState(openAddInitially);
  const [page, setPage] = useState(1);
  const [reloadKey, setReloadKey] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [result, setResult] = useState<{
    requestKey: string;
    data: Paginated<KnowledgeContent> | null;
    error: string | null;
    polling: boolean;
  } | null>(null);

  const requestKey = `${collection}|${page}|${reloadKey}`;

  useEffect(() => {
    let cancelled = false;
    // Pela coleção, não pela tabela inteira: a tabela de conteúdo do Agno é
    // compartilhada por todas as coleções, então a lista antiga mostrava
    // documentos de outras coleções sob a coleção selecionada.
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), page: String(page) });
    requestJson<Paginated<KnowledgeContent>>(
      `/api/collections/${encodeURIComponent(collection)}/documents?${params}`,
      { fallbackError: "Falha ao listar documentos" }
    )
      .then((data) => {
        if (!cancelled) {
          setResult({ requestKey, data, error: null, polling: hasActiveProcessing(data, Date.now()) });
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setResult((prev) => ({ requestKey, data: prev?.data ?? null, error: errorMessage(err), polling: false }));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [requestKey, page, collection]);

  const documents = result?.data?.data ?? [];
  const meta = result?.data?.meta;
  const polling = result?.polling ?? false;
  const refreshing = result !== null && result.requestKey !== requestKey;

  // A ingestão é assíncrona: enquanto houver item processando (recente), a lista se atualiza sozinha.
  useEffect(() => {
    if (!polling) return;
    const timeout = window.setTimeout(() => setReloadKey((k) => k + 1), POLL_INTERVAL_MS);
    return () => window.clearTimeout(timeout);
  }, [polling, result]);

  const reload = () => setReloadKey((k) => k + 1);

  async function handleDeleteCollection(target: Collection) {
    const confirmed = await confirm({
      title: `Excluir a coleção "${target.label}"?`,
      description:
        "O cadastro sai da lista; os documentos já indexados continuam na tabela de vetores e voltam a aparecer se a coleção for recriada com o mesmo nome.",
      confirmLabel: "Excluir coleção",
      destructive: true,
    });
    if (!confirmed) return;
    try {
      await requestJson(`/api/collections/${encodeURIComponent(target.name)}`, {
        method: "DELETE",
        fallbackError: "Falha ao excluir coleção",
      });
      toast({ title: "Coleção excluída", variant: "success" });
      setCollection(collections.find((c) => c.name !== target.name)?.name ?? "general");
      router.refresh();
    } catch (err) {
      toast({ title: "Não foi possível excluir", description: errorMessage(err), variant: "error" });
    }
  }

  function handleAddOpenChange(open: boolean) {
    setAddOpen(open);
    if (!open && window.location.search.includes("add=")) {
      window.history.replaceState(null, "", window.location.pathname);
    }
  }

  async function handleDelete(doc: KnowledgeContent) {
    const confirmed = await confirm({
      title: "Excluir documento?",
      description: `"${doc.name || "Sem nome"}" e seus embeddings serão removidos da coleção e deixam de aparecer nas buscas.`,
      confirmLabel: "Excluir documento",
      destructive: true,
    });
    if (!confirmed) return;
    try {
      await requestJson(
        `/api/collections/${encodeURIComponent(collection)}/documents/${encodeURIComponent(doc.id)}`,
        { method: "DELETE", fallbackError: "Falha ao excluir documento" }
      );
      toast({ title: "Documento excluído", variant: "success" });
      reload();
    } catch (err) {
      toast({ title: "Não foi possível excluir", description: errorMessage(err), variant: "error" });
    }
  }

  const failedOnPage = documents.filter((d) => d.status === "failed").length;
  const summary = meta
    ? [
        pluralize(meta.total_count, "documento", "documentos"),
        failedOnPage > 0 && `${failedOnPage} com falha${meta.total_pages > 1 ? " nesta página" : ""}`,
        polling && "atualizando automaticamente",
      ]
        .filter(Boolean)
        .join(" · ")
    : undefined;

  return (
    <>
      <PageHeader
        title="Base de conhecimento"
        description="Documentos indexados com embeddings (pgvector) para RAG. A ingestão roda em segundo plano — acompanhe o status de cada item."
        actions={
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setNewOpen(true)} disabled={!backendReachable}>
              <Plus /> Nova coleção
            </Button>
            <Button onClick={() => setAddOpen(true)} disabled={!backendReachable}>
              <Plus /> Adicionar conteúdo
            </Button>
          </div>
        }
      />

      <PageBody className="flex flex-col gap-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          {collections.length > 1 && (
            <Tabs
              value={collection}
              onValueChange={(value) => {
                setCollection(value);
                setPage(1);
              }}
              variant="pill"
            >
              <TabsList>
                {collections.map((c) => (
                  <TabsTrigger key={c.name} value={c.name}>
                    {c.label}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
          )}
          {current && (
            <div className="flex items-center gap-3 text-sm text-muted-foreground">
              <span>
                {current.agents_using.length > 0
                  ? `Consultada por: ${current.agents_using.join(", ")}`
                  : "Nenhum agente consulta esta coleção — ligue uma na aba Modelo do agente."}
              </span>
              {!current.is_seed && (
                <Button variant="ghost" size="sm" onClick={() => handleDeleteCollection(current)}>
                  <Trash2 /> Excluir coleção
                </Button>
              )}
            </div>
          )}
        </div>

        <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
          <section className="flex min-w-0 flex-col gap-3">
            <SectionHeading
              title={
                <span className="flex items-center gap-2">
                  Documentos
                  <Badge variant="outline" className="font-mono">
                    knowledge_{collection}
                  </Badge>
                </span>
              }
              description={summary}
              action={
                <Button variant="ghost" size="sm" onClick={reload} disabled={refreshing} className="-mr-2">
                  {refreshing ? <Spinner /> : <RotateCcw />} Atualizar
                </Button>
              }
            />

            <Card className="overflow-hidden">
              {result === null ? (
                <div className="flex flex-col gap-3 p-4" aria-busy="true">
                  {[1, 2, 3].map((i) => (
                    <Skeleton key={i} className="h-11" />
                  ))}
                </div>
              ) : result.error && documents.length === 0 ? (
                <EmptyState
                  icon={CircleAlert}
                  title="Não foi possível carregar os documentos"
                  description={result.error}
                  action={
                    <Button variant="outline" size="sm" onClick={reload}>
                      <RotateCcw /> Tentar de novo
                    </Button>
                  }
                />
              ) : documents.length === 0 ? (
                <EmptyState
                  icon={Library}
                  title="Nenhum documento nesta coleção"
                  description="Adicione textos, arquivos ou URLs para que os agentes tenham uma base para consultar."
                  action={
                    <Button variant="outline" size="sm" onClick={() => setAddOpen(true)}>
                      <Plus /> Adicionar conteúdo
                    </Button>
                  }
                />
              ) : (
                <table className="w-full table-fixed text-left text-[13px]">
                  <thead className="border-b border-border bg-surface text-xs text-muted-foreground">
                    <tr>
                      <th className="px-4 py-2.5 font-medium">Documento</th>
                      <th className="hidden w-36 px-4 py-2.5 font-medium sm:table-cell">Status</th>
                      <th className="hidden w-24 px-4 py-2.5 text-right font-medium md:table-cell">Tamanho</th>
                      <th className="hidden w-32 px-4 py-2.5 font-medium lg:table-cell">Adicionado</th>
                      <th className="w-12 px-2 py-2.5">
                        <span className="sr-only">Ações</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {documents.map((doc) => {
                      const kind = contentKind(doc.type);
                      const size = Number(doc.size);
                      const isExpanded = expanded === doc.id && Boolean(doc.status_message);
                      return (
                        <Fragment key={doc.id}>
                          <tr className="transition-colors hover:bg-accent/30">
                            <td className="px-4 py-3">
                              <div className="flex min-w-0 items-center gap-3">
                                <div className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-border bg-surface text-muted-foreground">
                                  <kind.icon className="size-4" />
                                </div>
                                <div className="min-w-0">
                                  <p className="truncate font-medium" title={doc.name ?? undefined}>
                                    {doc.name || "Sem nome"}
                                  </p>
                                  <p className="truncate text-xs text-muted-foreground">
                                    {kind.label}
                                    {doc.description ? ` · ${doc.description}` : ""}
                                  </p>
                                  <div className="mt-1.5 sm:hidden">
                                    <StatusBadge
                                      doc={doc}
                                      expanded={isExpanded}
                                      onToggle={() => setExpanded(isExpanded ? null : doc.id)}
                                    />
                                  </div>
                                </div>
                              </div>
                            </td>
                            <td className="hidden px-4 py-3 sm:table-cell">
                              <StatusBadge
                                doc={doc}
                                expanded={isExpanded}
                                onToggle={() => setExpanded(isExpanded ? null : doc.id)}
                              />
                            </td>
                            <td className="hidden px-4 py-3 text-right tabular-nums text-muted-foreground md:table-cell">
                              {Number.isFinite(size) && doc.size ? formatBytes(size) : "—"}
                            </td>
                            <td className="hidden px-4 py-3 text-muted-foreground lg:table-cell">
                              {doc.created_at ? <RelativeTime date={doc.created_at} /> : "—"}
                            </td>
                            <td className="px-2 py-3 text-right">
                              <DropdownMenu
                                align="end"
                                trigger={(props) => (
                                  <Button {...props} variant="ghost" size="icon-sm" aria-label={`Ações para ${doc.name ?? "documento"}`}>
                                    <Ellipsis />
                                  </Button>
                                )}
                              >
                                <DropdownMenuItem icon={Trash} destructive onSelect={() => handleDelete(doc)}>
                                  Excluir
                                </DropdownMenuItem>
                              </DropdownMenu>
                            </td>
                          </tr>
                          {isExpanded && (
                            <tr>
                              <td colSpan={5} className="bg-destructive/5 px-4 py-3">
                                <p className="whitespace-pre-wrap break-words font-mono text-xs leading-5 text-destructive">
                                  {doc.status_message}
                                </p>
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </Card>

            {meta && meta.total_pages > 1 && (
              <div className="flex items-center justify-end gap-3 text-[13px] text-muted-foreground">
                Página {meta.page} de {meta.total_pages}
                <div className="flex gap-1">
                  <Button
                    variant="outline"
                    size="icon-sm"
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={page <= 1}
                    aria-label="Página anterior"
                  >
                    <ChevronLeft />
                  </Button>
                  <Button
                    variant="outline"
                    size="icon-sm"
                    onClick={() => setPage((p) => p + 1)}
                    disabled={page >= meta.total_pages}
                    aria-label="Próxima página"
                  >
                    <ChevronRight />
                  </Button>
                </div>
              </div>
            )}
          </section>

          <SearchPanel collection={collection} />
        </div>
      </PageBody>

      <NewCollectionDialog
        open={newOpen}
        onOpenChange={setNewOpen}
        onCreated={(name) => {
          setCollection(name);
          setPage(1);
          router.refresh();
        }}
      />

      <AddContentDialog
        open={addOpen}
        onOpenChange={handleAddOpenChange}
        collection={collection}
        onAdded={() => {
          setPage(1);
          reload();
        }}
      />
    </>
  );
}

function StatusBadge({
  doc,
  expanded,
  onToggle,
}: {
  doc: KnowledgeContent;
  expanded: boolean;
  onToggle: () => void;
}) {
  const status = doc.status ? STATUS[doc.status] : null;
  if (!status) return <Badge variant="outline">{doc.status ?? "desconhecido"}</Badge>;

  const badge = (
    <Badge variant={status.variant}>
      <status.icon className={cn(doc.status === "processing" && "animate-spin")} />
      {status.label}
      {doc.status_message && (doc.status === "failed" || doc.status === "partial") && (
        <ChevronDown className={cn("transition-transform", expanded && "rotate-180")} />
      )}
    </Badge>
  );

  if (!doc.status_message || (doc.status !== "failed" && doc.status !== "partial")) return badge;

  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={expanded}
      title="Ver detalhes do erro"
      className="rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {badge}
    </button>
  );
}
