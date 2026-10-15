"use client";

import { useMemo, useState, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronRight, CircleAlert, Ellipsis, Play, Plus, Search, Wrench, X } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import { pluralize } from "@/lib/format";
import { TOOL_KIND_META } from "@/lib/agent-meta";
import { cn } from "@/lib/cn";
import {
  EMPTY_FILTERS,
  NO_USAGE,
  SYSTEM_GROUP,
  apiEndpoint,
  firstSentence,
  groupTools,
  hasActiveFilters,
  isUnclassified,
  isUnused,
  matchesFilters,
  toolUsageIndex,
  type ToolFilters,
  type ToolGroup,
  type ToolUsage,
} from "@/lib/tools-grouping";
import type { ApiToolParam, ToolKind, ToolSummary, ToolUpdateInput } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DropdownMenu, DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { EmptyState, RelativeTime } from "@/components/ui/primitives";
import { useConfirm } from "@/components/ui/confirm";
import { useToast } from "@/components/ui/toast";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { ToolEditorDialog } from "@/components/tools/tool-editor-dialog";
import { ToolInvokeDialog } from "@/components/tools/tool-invoke-dialog";

const KIND_ORDER: ToolKind[] = ["api", "python", "builtin"];

const UNCLASSIFIED_HINT =
  "Ainda não se sabe se esta tool grava, cobra ou envia algo. A fila de revisão dos Logs usa isso para apontar " +
  "as execuções que mudaram algo — edite a tool e classifique.";

export function ToolsIndex() {
  const router = useRouter();
  const { availableTools, agents, backendReachable } = useWorkspace();
  const toast = useToast();
  const confirm = useConfirm();

  const [filters, setFilters] = useState<ToolFilters>(EMPTY_FILTERS);
  const [editing, setEditing] = useState<ToolSummary | "new" | null>(null);
  const [invoking, setInvoking] = useState<ToolSummary | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set([SYSTEM_GROUP]));
  const [toggling, setToggling] = useState<string | null>(null);

  const usage = useMemo(() => toolUsageIndex(agents), [agents]);
  const filtering = hasActiveFilters(filters);

  const counts = useMemo(
    () => ({
      kinds: KIND_ORDER.map((k) => [k, availableTools.filter((t) => t.kind === k).length] as const).filter(([, n]) => n > 0),
      sideEffect: availableTools.filter((t) => t.side_effect === true).length,
      unclassified: availableTools.filter(isUnclassified).length,
      disabled: availableTools.filter((t) => !t.enabled).length,
      unused: availableTools.filter((t) => isUnused(t, usage)).length,
    }),
    [availableTools, usage]
  );

  const filtered = useMemo(
    () => availableTools.filter((t) => matchesFilters(t, filters, usage)),
    [availableTools, filters, usage]
  );
  const groups = useMemo(() => groupTools(filtered), [filtered]);

  const set = (patch: Partial<ToolFilters>) => setFilters((f) => ({ ...f, ...patch }));

  function toggleIn(setter: typeof setExpanded, key: string) {
    setter((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function refresh() {
    router.refresh();
  }

  async function handleToggleEnabled(tool: ToolSummary) {
    const u = usage.get(tool.tool_name) ?? NO_USAGE;
    const users = [...u.agents, ...u.drafts];
    if (tool.enabled && users.length > 0) {
      const confirmed = await confirm({
        title: `Desligar "${tool.label}"?`,
        description: `${users.join(", ")} ${users.length === 1 ? "usa" : "usam"} esta tool e deixa${users.length === 1 ? "" : "m"} de conseguir chamá-la até você ligá-la de novo.`,
        confirmLabel: "Desligar",
        destructive: true,
      });
      if (!confirmed) return;
    }
    setToggling(tool.tool_name);
    try {
      const payload: ToolUpdateInput = { enabled: !tool.enabled };
      await requestJson(`/api/tools/${encodeURIComponent(tool.tool_name)}`, {
        method: "PUT",
        json: payload,
        fallbackError: "Falha ao salvar a tool"
      });
      toast({ title: tool.enabled ? "Tool desligada" : "Tool ligada", variant: "success" });
      refresh();
    } catch (err) {
      toast({ title: "Não foi possível salvar", description: errorMessage(err), variant: "error" });
    } finally {
      setToggling(null);
    }
  }

  async function handleDelete(tool: ToolSummary) {
    const confirmed = await confirm({
      title: `Excluir "${tool.label}"?`,
      description: "A tool some do catálogo e não tem volta. Nenhum agente a usa hoje.",
      confirmLabel: "Excluir tool",
      destructive: true,
    });
    if (!confirmed) return;
    try {
      await requestJson(`/api/tools/${encodeURIComponent(tool.tool_name)}`, {
        method: "DELETE",
        fallbackError: "Falha ao excluir a tool",
      });
      toast({ title: "Tool excluída", variant: "success" });
      refresh();
    } catch (err) {
      toast({ title: "Não foi possível excluir", description: errorMessage(err), variant: "error" });
    }
  }

  const newToolButton = (
    <Button onClick={() => setEditing("new")}>
      <Plus /> Nova tool
    </Button>
  );

  return (
    <>
      <PageHeader
        title="Tools"
        description="Funções que os agentes podem chamar — APIs HTTP, funções Python ou toolkits padrão. A descrição de cada uma vai para o modelo decidir quando usá-la."
        actions={backendReachable && availableTools.length > 0 ? newToolButton : undefined}
      />
      <PageBody>
        {!backendReachable ? (
          <Card>
            <EmptyState
              icon={Wrench}
              title="agent-service indisponível"
              description="Não foi possível carregar as tools. Verifique se o backend está rodando e recarregue a página."
            />
          </Card>
        ) : availableTools.length === 0 ? (
          <Card>
            <EmptyState
              icon={Wrench}
              title="Nenhuma tool ainda"
              description="Crie a primeira: uma chamada de API, uma função Python ou uma toolkit padrão do Agno."
              action={newToolButton}
            />
          </Card>
        ) : (
          <div className="flex flex-col gap-5">
            <div className="flex flex-col gap-3">
              <div className="flex flex-wrap items-center gap-3">
                <div className="relative w-full max-w-xs">
                  <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    type="search"
                    value={filters.query}
                    onChange={(e) => set({ query: e.target.value })}
                    placeholder="Buscar por nome, descrição, URL ou agente"
                    aria-label="Buscar tools"
                    className="pl-9"
                  />
                </div>
                <p className="ml-auto text-[13px] text-muted-foreground">
                  {filtering
                    ? `${pluralize(filtered.length, "tool", "tools")} de ${availableTools.length}`
                    : pluralize(availableTools.length, "tool", "tools")}
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {counts.kinds.length > 1 &&
                  counts.kinds.map(([kind, n]) => (
                    <Chip
                      key={kind}
                      active={filters.kind === kind}
                      onClick={() => set({ kind: filters.kind === kind ? null : kind })}
                      title={TOOL_KIND_META[kind].description}
                    >
                      {TOOL_KIND_META[kind].label} <Count n={n} />
                    </Chip>
                  ))}
                {counts.kinds.length > 1 && <span aria-hidden className="mx-1 h-5 w-px bg-border" />}
                <Chip
                  active={filters.sideEffect}
                  onClick={() => set({ sideEffect: !filters.sideEffect })}
                  title="Tools que gravam, cobram ou enviam algo no sistema chamado"
                >
                  Efeito colateral <Count n={counts.sideEffect} />
                </Chip>
                {(counts.unclassified > 0 || filters.unclassified) && (
                  <Chip
                    active={filters.unclassified}
                    onClick={() => set({ unclassified: !filters.unclassified })}
                    title={UNCLASSIFIED_HINT}
                    tone="warning"
                  >
                    Não classificada <Count n={counts.unclassified} />
                  </Chip>
                )}
                {(counts.disabled > 0 || filters.disabled) && (
                  <Chip active={filters.disabled} onClick={() => set({ disabled: !filters.disabled })}>
                    Desligada <Count n={counts.disabled} />
                  </Chip>
                )}
                {(counts.unused > 0 || filters.unused) && (
                  <Chip
                    active={filters.unused}
                    onClick={() => set({ unused: !filters.unused })}
                    title="Nenhum agente (fora os drafts) tem esta tool em tools ou numa etapa action"
                  >
                    Sem agente <Count n={counts.unused} />
                  </Chip>
                )}
                {filtering && (
                  <Button variant="ghost" size="sm" onClick={() => setFilters(EMPTY_FILTERS)}>
                    <X /> Limpar
                  </Button>
                )}
              </div>
            </div>

            {counts.unclassified > 0 && !filters.unclassified && (
              <p className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2 text-[13px]">
                <CircleAlert className="size-4 shrink-0 text-warning" />
                <span>
                  {counts.unclassified === 1
                    ? "1 tool ainda não diz se tem efeito colateral"
                    : `${counts.unclassified} tools ainda não dizem se têm efeito colateral`}
                  <span className="text-muted-foreground"> — a fila de revisão dos Logs depende disso.</span>
                </span>
                <Button variant="link" size="sm" className="text-[13px]" onClick={() => set({ unclassified: true })}>
                  Mostrar
                </Button>
              </p>
            )}

            {filtered.length === 0 ? (
              <Card>
                <EmptyState
                  icon={Search}
                  title="Nenhuma tool encontrada"
                  description="Nada corresponde à busca e aos filtros escolhidos."
                  action={
                    <Button variant="outline" onClick={() => setFilters(EMPTY_FILTERS)}>
                      Limpar filtros
                    </Button>
                  }
                />
              </Card>
            ) : (
              groups.map((group) => (
                <GroupSection
                  key={group.key}
                  group={group}
                  open={filtering || !collapsed.has(group.key)}
                  onToggle={filtering ? undefined : () => toggleIn(setCollapsed, group.key)}
                >
                  {group.tools.map((tool) => (
                    <ToolRow
                      key={tool.tool_name}
                      tool={tool}
                      usage={usage.get(tool.tool_name) ?? NO_USAGE}
                      expanded={expanded.has(tool.tool_name)}
                      busy={toggling === tool.tool_name}
                      onExpand={() => toggleIn(setExpanded, tool.tool_name)}
                      onEdit={() => setEditing(tool)}
                      onInvoke={() => setInvoking(tool)}
                      onToggleEnabled={() => void handleToggleEnabled(tool)}
                      onDelete={() => void handleDelete(tool)}
                    />
                  ))}
                </GroupSection>
              ))
            )}
          </div>
        )}
      </PageBody>

      {editing && (
        <ToolEditorDialog
          tool={editing === "new" ? null : editing}
          existingNames={availableTools.map((t) => t.tool_name)}
          onOpenChange={(open) => !open && setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
      {invoking && <ToolInvokeDialog tool={invoking} onOpenChange={(open) => !open && setInvoking(null)} />}
    </>
  );
}

function Count({ n }: { n: number }) {
  return <span className="tabular-nums text-muted-foreground">{n}</span>;
}

function Chip({
  active,
  onClick,
  children,
  title,
  tone
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
  title?: string;
  tone?: "warning";
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      title={title}
      onClick={onClick}
      className={cn(
        "inline-flex h-8 items-center gap-1.5 rounded-full border px-3 text-xs transition-colors",
        active
          ? "border-primary bg-primary-soft font-medium text-foreground"
          : tone === "warning"
            ? "border-warning/40 text-warning hover:bg-warning/5"
            : "border-border text-muted-foreground hover:text-foreground"
      )}
    >
      {children}
    </button>
  );
}

function GroupSection({
  group,
  open,
  onToggle,
  children
}: {
  group: ToolGroup;
  open: boolean;
  onToggle?: () => void;
  children: ReactNode;
}) {
  const sideEffects = group.tools.filter((t) => t.side_effect === true).length;
  const unclassified = group.tools.filter(isUnclassified).length;
  const isSystem = group.key === SYSTEM_GROUP;
  return (
    <section className="flex flex-col gap-2">
      <button
        type="button"
        onClick={onToggle}
        disabled={!onToggle}
        aria-expanded={open}
        className="group flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-left disabled:cursor-default"
      >
        <ChevronRight
          className={cn(
            "size-4 shrink-0 self-center text-muted-foreground transition-transform",
            open && "rotate-90",
            !onToggle && "opacity-40"
          )}
        />
        <h2 className={cn("text-sm font-semibold", onToggle && "group-hover:underline")}>{group.title}</h2>
        <span className="text-[13px] text-muted-foreground">
          {pluralize(group.tools.length, "tool", "tools")}
          {sideEffects > 0 && ` · ${sideEffects} com efeito colateral`}
        </span>
        {unclassified > 0 && <Badge variant="warning">{unclassified} não classificada{unclassified === 1 ? "" : "s"}</Badge>}
        {isSystem ? (
          <span className="text-xs text-muted-foreground">vêm com o serviço; não dá para excluir</span>
        ) : (
          group.hosts.length > 0 && (
            <span className="truncate font-mono text-xs text-muted-foreground">{group.hosts.join(", ")}</span>
          )
        )}
      </button>
      {open && <Card className="divide-y divide-border overflow-hidden">{children}</Card>}
    </section>
  );
}

function ToolRow({
  tool,
  usage,
  expanded,
  busy,
  onExpand,
  onEdit,
  onInvoke,
  onToggleEnabled,
  onDelete
}: {
  tool: ToolSummary;
  usage: ToolUsage;
  expanded: boolean;
  busy: boolean;
  onExpand: () => void;
  onEdit: () => void;
  onInvoke: () => void;
  onToggleEnabled: () => void;
  onDelete: () => void;
}) {
  const endpoint = apiEndpoint(tool);
  const summary = firstSentence(tool.description);
  const blockedBy = [...usage.agents, ...usage.drafts];
  const builtinId = tool.kind === "builtin" && typeof tool.config.builtin_id === "string" ? tool.config.builtin_id : null;

  return (
    <div className={cn(!tool.enabled && "bg-muted/40")}>
      <div className="flex items-start gap-2 px-3 py-3 sm:px-4">
        <Button
          variant="ghost"
          size="icon-xs"
          onClick={onExpand}
          aria-expanded={expanded}
          aria-label={expanded ? "Recolher detalhes" : "Ver detalhes"}
          className="mt-px"
        >
          <ChevronRight className={cn("transition-transform", expanded && "rotate-90")} />
        </Button>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <button
              type="button"
              onClick={onExpand}
              className={cn("text-left text-sm font-medium hover:underline", !tool.enabled && "text-muted-foreground")}
            >
              {tool.label}
            </button>
            <span className="font-mono text-xs text-muted-foreground">{tool.tool_name}</span>
            <StatusBadges tool={tool} />
          </div>
          {summary.head ? (
            <p className="line-clamp-2 text-[13px] text-muted-foreground" title={tool.description ?? undefined}>
              {summary.head}
              {summary.rest && !expanded && <span className="text-muted-foreground/70"> …</span>}
            </p>
          ) : (
            <p className="text-[13px] italic text-muted-foreground/70">
              {tool.kind === "builtin" ? "Descrição vem da toolkit." : "Sem descrição — o modelo só vê o nome."}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              <Badge variant="outline">{TOOL_KIND_META[tool.kind].label}</Badge>
              {endpoint && (
                <span className="font-mono">
                  <span className="font-semibold text-foreground/80">{endpoint.method}</span> {endpoint.host}
                </span>
              )}
              {builtinId && <span className="font-mono">{builtinId}</span>}
            </span>
            <UsageLine usage={usage} isSeed={tool.is_seed} />
            <span className="hidden sm:inline">
              editada <RelativeTime date={tool.updated_at} />
            </span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button variant="ghost" size="sm" onClick={onInvoke} title="Testar a tool sem montar agente">
            <Play /> <span className="hidden sm:inline">Testar</span>
          </Button>
          <DropdownMenu
            align="end"
            trigger={(props) => (
              <Button {...props} variant="ghost" size="icon-sm" aria-label="Mais ações">
                <Ellipsis />
              </Button>
            )}
          >
            <DropdownMenuItem onSelect={onEdit}>Editar</DropdownMenuItem>
            <DropdownMenuItem onSelect={onToggleEnabled} disabled={busy}>
              {tool.enabled ? "Desligar" : "Ligar"}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              destructive
              disabled={tool.is_seed || blockedBy.length > 0}
              hint={tool.is_seed ? "sistema" : blockedBy.length > 0 ? "em uso" : undefined}
              onSelect={onDelete}
            >
              Excluir
            </DropdownMenuItem>
          </DropdownMenu>
        </div>
      </div>
      {expanded && <ToolDetailPanel tool={tool} usage={usage} onEdit={onEdit} />}
    </div>
  );
}

function StatusBadges({ tool }: { tool: ToolSummary }) {
  return (
    <>
      {!tool.enabled && <Badge variant="secondary">desligada</Badge>}
      {tool.side_effect === true && (
        <Badge variant="outline" title="Grava, cobra ou envia algo no sistema chamado">
          efeito colateral
        </Badge>
      )}
      {isUnclassified(tool) && (
        <Badge variant="warning" title={UNCLASSIFIED_HINT}>
          não classificada
        </Badge>
      )}
    </>
  );
}

function UsageLine({ usage, isSeed }: { usage: ToolUsage; isSeed: boolean }) {
  if (usage.agents.length === 0) {
    const drafts = usage.drafts.length ? ` (só ${pluralize(usage.drafts.length, "draft", "drafts")})` : "";
    return (
      <span className={cn(!isSeed && !drafts && "text-warning/90")}>
        nenhum agente usa{drafts}
      </span>
    );
  }
  const shown = usage.agents.slice(0, 2);
  const extra = usage.agents.length - shown.length;
  return (
    <span title={[...usage.agents, ...usage.drafts].join(", ")}>
      usada por{" "}
      {shown.map((a, i) => (
        <span key={a}>
          {i > 0 && ", "}
          <Link href={`/agents/${encodeURIComponent(a)}`} className="text-foreground/80 hover:underline">
            {a}
          </Link>
        </span>
      ))}
      {extra > 0 && ` +${extra}`}
    </span>
  );
}

const SOURCE_LABEL: Record<string, string> = { model: "modelo", dependency: "dependency", const: "fixo" };

function ToolDetailPanel({ tool, usage, onEdit }: { tool: ToolSummary; usage: ToolUsage; onEdit: () => void }) {
  const endpoint = apiEndpoint(tool);
  const params = Array.isArray(tool.config.parameters) ? (tool.config.parameters as ApiToolParam[]) : [];
  const entrypoint = tool.kind === "python" && typeof tool.config.entrypoint === "string" ? tool.config.entrypoint : null;

  return (
    <div className="flex flex-col gap-4 border-t border-border bg-muted/30 px-4 py-4 sm:pl-12">
      {tool.description && (
        <DetailBlock label="Descrição (o que o modelo lê)">
          <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed">{tool.description}</p>
        </DetailBlock>
      )}
      {endpoint && (
        <DetailBlock label="Chamada">
          <p className="break-all font-mono text-xs">
            <span className="font-semibold">{endpoint.method}</span> {endpoint.url}
          </p>
        </DetailBlock>
      )}
      {entrypoint && (
        <DetailBlock label="Função">
          <p className="font-mono text-xs">{entrypoint}()</p>
        </DetailBlock>
      )}
      {endpoint && (
        <DetailBlock label="Parâmetros">
          {params.length === 0 ? (
            <p className="text-[13px] text-muted-foreground">Nenhum.</p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {params.map((p) => (
                <li key={`${p.location}:${p.name}`} className="flex flex-wrap items-baseline gap-x-2 text-[13px]">
                  <span className="font-mono text-xs">
                    {p.name}
                    {p.required && <span className="text-destructive">*</span>}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {p.type} · {p.location}
                  </span>
                  <Badge variant={p.source === "dependency" ? "default" : "secondary"}>
                    {SOURCE_LABEL[p.source ?? "model"] ?? p.source}
                    {p.source === "dependency" && p.dependency && p.dependency !== p.name ? `: ${p.dependency}` : ""}
                  </Badge>
                  {p.description && <span className="min-w-0 text-xs text-muted-foreground">{p.description}</span>}
                </li>
              ))}
            </ul>
          )}
        </DetailBlock>
      )}
      <DetailBlock label="Agentes">
        {usage.agents.length + usage.drafts.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">Nenhum agente usa esta tool.</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {[...usage.agents, ...usage.drafts].map((a) => (
              <Link
                key={a}
                href={`/agents/${encodeURIComponent(a)}`}
                className={cn(
                  "rounded-md border border-border px-2 py-0.5 font-mono text-xs hover:bg-accent",
                  usage.drafts.includes(a) && "text-muted-foreground"
                )}
              >
                {a}
              </Link>
            ))}
          </div>
        )}
      </DetailBlock>
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <Button variant="outline" size="sm" onClick={onEdit}>
          Editar tool
        </Button>
        <span>
          criada <RelativeTime date={tool.created_at} /> · editada <RelativeTime date={tool.updated_at} />
        </span>
      </div>
    </div>
  );
}

function DetailBlock({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      {children}
    </div>
  );
}
