"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Activity,
  FileSearch,
  Bot,
  Check,
  Copy,
  Cpu,
  Ellipsis,
  ExternalLink,
  Library,
  MessageSquare,
  Monitor,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  RotateCcw,
  Search,
  Settings2,
  SquarePen,
  Sun,
  Trash,
  Wrench,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { formatDateTime, groupByRecency } from "@/lib/format";
import { apiUrl, errorMessage } from "@/lib/http";
import { sessionTitle } from "@/lib/sessions";
import { useTheme } from "@/lib/theme";
import { useModifierKeyLabel } from "@/lib/use-platform";
import type { SessionSummary } from "@/lib/types";
import { KuroCrow } from "@/components/brand/kuro-crow";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import { Kbd, Skeleton } from "@/components/ui/primitives";
import { useConfirm } from "@/components/ui/confirm";
import { useToast } from "@/components/ui/toast";
import { useWorkspace } from "@/components/workspace/workspace-provider";

const NAV_ITEMS = [
  { href: "/chat", label: "Playground", icon: MessageSquare },
  { href: "/analyze", label: "Análise", icon: FileSearch },
  { href: "/agents", label: "Agentes", icon: Bot },
  { href: "/tools", label: "Tools", icon: Wrench },
  { href: "/models", label: "Chaves de API", icon: Cpu },
  { href: "/knowledge", label: "Conhecimento", icon: Library },
];
const LOGS_ITEM = { href: "/logs", label: "Logs", icon: Activity };

export function Sidebar({
  collapsed = false,
  onToggleCollapsed,
  onNavigate,
}: {
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const { agents, setCommandOpen, observability } = useWorkspace();
  const modKey = useModifierKeyLabel();

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className={cn("flex h-14 shrink-0 items-center gap-2", collapsed ? "justify-center px-2" : "px-3")}>
        {!collapsed && (
          <Link href="/chat" onClick={onNavigate} className="flex min-w-0 items-center gap-2.5 rounded-lg px-1 py-1">
            <KuroCrow scale={2} />
            <span className="flex min-w-0 flex-col leading-tight">
              <span className="truncate font-mono text-sm font-semibold">kuro</span>
              <span className="truncate text-[11px] text-muted-foreground">Agents, under control.</span>
            </span>
          </Link>
        )}
        {onToggleCollapsed && (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onToggleCollapsed}
            aria-label={collapsed ? "Expandir sidebar" : "Recolher sidebar"}
            title={`${collapsed ? "Expandir" : "Recolher"} (${modKey}+B)`}
            className={cn("text-muted-foreground", !collapsed && "ml-auto")}
          >
            {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
          </Button>
        )}
      </div>

      <div className={cn("flex shrink-0 flex-col gap-1", collapsed ? "items-center px-2" : "px-3")}>
        <Link
          href="/chat"
          onClick={onNavigate}
          title="Nova conversa"
          className={buttonVariants({
            variant: "outline",
            size: collapsed ? "icon-sm" : "default",
            className: collapsed ? "" : "w-full justify-start gap-2.5 px-3",
          })}
        >
          <SquarePen />
          {!collapsed && <span>Nova conversa</span>}
        </Link>
        <button
          type="button"
          onClick={() => {
            onNavigate?.();
            setCommandOpen(true);
          }}
          title={`Buscar (${modKey}+K)`}
          className={cn(
            "flex h-9 items-center gap-2.5 rounded-lg text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
            collapsed ? "w-8 justify-center" : "w-full px-3"
          )}
        >
          <Search className="size-4 shrink-0" />
          {!collapsed && (
            <>
              <span className="flex-1 text-left">Buscar</span>
              <span className="flex gap-0.5">
                <Kbd>{modKey}</Kbd>
                <Kbd>K</Kbd>
              </span>
            </>
          )}
        </button>
      </div>

      <nav
        aria-label="Principal"
        className={cn("mt-3 flex shrink-0 flex-col gap-0.5", collapsed ? "items-center px-2" : "px-3")}
      >
        {(observability.enabled ? [...NAV_ITEMS, LOGS_ITEM] : NAV_ITEMS).map((item) => {
          const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
          return (
            <Link
              key={item.href}
              href={item.href}
              onClick={onNavigate}
              title={collapsed ? item.label : undefined}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex h-9 items-center gap-2.5 rounded-lg border-l-2 text-sm transition-colors",
                collapsed ? "w-8 justify-center border-l-0" : "pl-2.5 pr-3",
                active
                  ? "border-primary bg-accent font-medium text-foreground"
                  : "border-transparent text-muted-foreground hover:bg-accent/60 hover:text-foreground"
              )}
            >
              <item.icon className={cn("size-4 shrink-0", active && "text-primary")} />
              {!collapsed && (
                <>
                  <span className="flex-1">{item.label}</span>
                  {item.href === "/agents" && agents.length > 0 && (
                    <span className="text-xs tabular-nums text-muted-foreground">{agents.length}</span>
                  )}
                </>
              )}
            </Link>
          );
        })}
      </nav>

      {collapsed ? <div className="flex-1" /> : <SessionList onNavigate={onNavigate} />}

      <SidebarFooter collapsed={collapsed} />
    </div>
  );
}

function SessionList({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const { sessions, sessionsLoading, sessionsError, refreshSessions } = useWorkspace();
  const activeId = pathname.startsWith("/chat/") ? decodeURIComponent(pathname.slice("/chat/".length)) : null;
  const groups = useMemo(() => groupByRecency(sessions, (s) => s.updated_at ?? s.created_at), [sessions]);

  return (
    <div className="mt-5 flex min-h-0 flex-1 flex-col">
      <p className="px-6 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        Conversas
      </p>
      <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {sessionsLoading && (
          <div className="flex flex-col gap-1.5 px-2.5 pt-1" aria-label="Carregando conversas">
            {[70, 90, 55, 80].map((width) => (
              <Skeleton key={width} className="h-5" style={{ width: `${width}%` }} />
            ))}
          </div>
        )}

        {!sessionsLoading && sessionsError && sessions.length === 0 && (
          <div className="flex flex-col items-start gap-2 px-2.5 py-2 text-[13px] text-muted-foreground">
            Não foi possível carregar as conversas.
            <Button variant="outline" size="sm" onClick={() => void refreshSessions()}>
              <RotateCcw /> Tentar de novo
            </Button>
          </div>
        )}

        {!sessionsLoading && !sessionsError && sessions.length === 0 && (
          <p className="px-2.5 py-2 text-[13px] text-muted-foreground">
            Suas conversas aparecem aqui depois da primeira mensagem.
          </p>
        )}

        {groups.map((group) => (
          <div key={group.label} className="mb-3">
            <p className="px-2.5 py-1 text-xs text-muted-foreground">{group.label}</p>
            <ul className="flex flex-col gap-px">
              {group.items.map((session) => (
                <li key={session.session_id}>
                  <SessionItem
                    session={session}
                    active={session.session_id === activeId}
                    onNavigate={onNavigate}
                  />
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}

function SessionItem({
  session,
  active,
  onNavigate,
}: {
  session: SessionSummary;
  active: boolean;
  onNavigate?: () => void;
}) {
  const router = useRouter();
  const confirm = useConfirm();
  const toast = useToast();
  const { renameSession, deleteSession, getAgent } = useWorkspace();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const title = sessionTitle(session);
  const agentName = getAgent(session.agent_id)?.name ?? session.agent_id ?? "agente";

  async function commitRename() {
    setEditing(false);
    const name = draft.trim();
    if (!name || name === title) return;
    try {
      await renameSession(session.session_id, name);
    } catch (err) {
      toast({ title: "Não foi possível renomear", description: errorMessage(err), variant: "error" });
    }
  }

  async function handleDelete() {
    const confirmed = await confirm({
      title: "Excluir conversa?",
      description: `"${title}" e todo o histórico dela serão removidos do agent-service. Essa ação não pode ser desfeita.`,
      confirmLabel: "Excluir conversa",
      destructive: true,
    });
    if (!confirmed) return;
    try {
      await deleteSession(session.session_id);
      toast({ title: "Conversa excluída", variant: "success" });
      if (active) router.push("/chat");
    } catch (err) {
      toast({ title: "Não foi possível excluir", description: errorMessage(err), variant: "error" });
    }
  }

  if (editing) {
    return (
      <input
        autoFocus
        aria-label="Novo nome da conversa"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commitRename}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") setEditing(false);
        }}
        className="h-8 w-full rounded-lg border border-ring bg-card px-2.5 text-[13px] outline-none ring-3 ring-ring/20"
      />
    );
  }

  return (
    <div
      className={cn(
        "group relative flex h-8 items-center rounded-lg text-[13px] transition-colors",
        active ? "bg-accent font-medium text-foreground" : "text-foreground/80 hover:bg-accent/60"
      )}
    >
      <Link
        href={`/chat/${encodeURIComponent(session.session_id)}`}
        onClick={onNavigate}
        aria-current={active ? "page" : undefined}
        title={`${title}\n${agentName} · ${formatDateTime(session.updated_at ?? session.created_at)}`}
        className="min-w-0 flex-1 truncate rounded-lg py-1.5 pl-2.5 pr-8 outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {title}
      </Link>
      <DropdownMenu
        align="end"
        trigger={(props) => (
          <button
            {...props}
            type="button"
            aria-label="Ações da conversa"
            className={cn(
              "absolute right-1 flex size-6 items-center justify-center rounded-md text-muted-foreground transition-opacity hover:bg-background hover:text-foreground",
              props["aria-expanded"]
                ? "opacity-100"
                : "opacity-0 focus-visible:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100"
            )}
          >
            <Ellipsis className="size-4" />
          </button>
        )}
      >
        <DropdownMenuItem
          icon={Pencil}
          onSelect={() => {
            setDraft(title);
            setEditing(true);
          }}
        >
          Renomear
        </DropdownMenuItem>
        <DropdownMenuItem icon={Trash} destructive onSelect={handleDelete}>
          Excluir
        </DropdownMenuItem>
      </DropdownMenu>
    </div>
  );
}

type HealthStatus = "checking" | "online" | "offline";

function useBackendHealth(): HealthStatus {
  const [status, setStatus] = useState<HealthStatus>("checking");

  useEffect(() => {
    let cancelled = false;
    async function check() {
      try {
        const res = await fetch(apiUrl("/api/health"), { cache: "no-store" });
        if (!cancelled) setStatus(res.ok ? "online" : "offline");
      } catch {
        if (!cancelled) setStatus("offline");
      }
    }
    void check();
    const interval = window.setInterval(check, 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  return status;
}

const HEALTH_LABELS: Record<HealthStatus, string> = {
  checking: "Verificando API…",
  online: "API conectada",
  offline: "API indisponível",
};

function SidebarFooter({ collapsed }: { collapsed: boolean }) {
  const health = useBackendHealth();
  const [theme, setTheme] = useTheme();
  const { userId, publicApiUrl } = useWorkspace();
  const toast = useToast();

  const themeOptions = [
    { value: "light", label: "Claro", icon: Sun },
    { value: "dark", label: "Escuro", icon: Moon },
    { value: "system", label: "Sistema", icon: Monitor },
  ] as const;

  return (
    <div
      className={cn(
        "flex shrink-0 items-center gap-1 border-t border-border p-2",
        collapsed && "flex-col gap-2 py-3"
      )}
    >
      <div
        className={cn(
          "flex min-w-0 flex-1 items-center gap-2 px-2 text-xs text-muted-foreground",
          collapsed && "justify-center px-0"
        )}
        title={`${HEALTH_LABELS[health]} · ${publicApiUrl}`}
        role="status"
      >
        <span
          className={cn(
            "size-2 shrink-0 rounded-full",
            health === "online" && "bg-success",
            health === "offline" && "bg-destructive",
            health === "checking" && "animate-pulse bg-muted-foreground/50"
          )}
        />
        {!collapsed && <span className="truncate">{HEALTH_LABELS[health]}</span>}
      </div>

      <DropdownMenu
        side="top"
        align={collapsed ? "start" : "end"}
        trigger={(props) => (
          <Button {...props} variant="ghost" size="icon-sm" aria-label="Preferências" className="text-muted-foreground">
            <Settings2 />
          </Button>
        )}
      >
        <DropdownMenuLabel>Tema</DropdownMenuLabel>
        {themeOptions.map((option) => (
          <DropdownMenuItem
            key={option.value}
            icon={option.icon}
            onSelect={() => setTheme(option.value)}
            hint={theme === option.value ? <Check className="size-3.5 text-primary" /> : undefined}
          >
            {option.label}
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuItem
          icon={Copy}
          onSelect={async () => {
            try {
              await navigator.clipboard.writeText(userId);
              toast({ title: "user_id copiado", description: userId, variant: "success" });
            } catch {
              toast({ title: "Não foi possível copiar", description: userId, variant: "error" });
            }
          }}
        >
          Copiar meu user_id
        </DropdownMenuItem>
        <DropdownMenuItem
          icon={ExternalLink}
          onSelect={() => window.open("/api/integration/openapi", "_blank", "noopener,noreferrer")}
        >
          OpenAPI de integração
        </DropdownMenuItem>
      </DropdownMenu>
    </div>
  );
}
