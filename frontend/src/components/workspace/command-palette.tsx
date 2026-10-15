"use client";

import { ComponentType, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Activity,
  Bot,
  Cpu,
  FileUp,
  Library,
  MessageSquare,
  Moon,
  Plus,
  Search,
  Settings2,
  SquarePen,
  Wrench,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { sessionTitle } from "@/lib/sessions";
import { useTheme } from "@/lib/theme";
import { Dialog } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/primitives";
import { useWorkspace } from "@/components/workspace/workspace-provider";

interface PaletteItem {
  id: string;
  group: string;
  label: string;
  hint?: string;
  keywords?: string;
  icon: ComponentType<{ className?: string }>;
  href?: string;
  /** Abre em outra aba, em vez de navegar no console (ex.: a UI do Langfuse). */
  external?: boolean;
  action?: "toggle-theme";
}

function normalize(text: string) {
  return text
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

export function CommandPalette() {
  const { commandOpen, setCommandOpen } = useWorkspace();
  return (
    <Dialog
      open={commandOpen}
      onOpenChange={setCommandOpen}
      size="md"
      hideClose
      className="sm:mt-[12vh] sm:self-start"
    >
      {commandOpen && <PaletteContent onClose={() => setCommandOpen(false)} />}
    </Dialog>
  );
}

function PaletteContent({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const { agents, sessions, getAgent, observability } = useWorkspace();
  const [, setTheme] = useTheme();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  const items = useMemo<PaletteItem[]>(
    () => [
      { id: "new-chat", group: "Ações", label: "Nova conversa", icon: SquarePen, href: "/chat", keywords: "chat playground" },
      { id: "new-agent", group: "Ações", label: "Criar agente", icon: Plus, href: "/agents/new" },
      { id: "add-content", group: "Ações", label: "Adicionar conteúdo à base", icon: FileUp, href: "/knowledge?add=1", keywords: "upload documento rag ingestão" },
      { id: "toggle-theme", group: "Ações", label: "Alternar tema claro/escuro", icon: Moon, action: "toggle-theme", keywords: "dark light" },
      { id: "nav-chat", group: "Navegar", label: "Playground", icon: MessageSquare, href: "/chat" },
      { id: "nav-agents", group: "Navegar", label: "Agentes", icon: Bot, href: "/agents" },
      { id: "nav-tools", group: "Navegar", label: "Tools", icon: Wrench, href: "/tools", keywords: "api python builtin função" },
      { id: "nav-models", group: "Navegar", label: "Chaves de API", icon: Cpu, href: "/models", keywords: "provedores openai anthropic google gemini ollama chave api key llm credencial" },
      { id: "nav-knowledge", group: "Navegar", label: "Base de conhecimento", icon: Library, href: "/knowledge", keywords: "collections documentos rag" },
      ...(observability.enabled
        ? [
            {
              id: "nav-logs",
              group: "Navegar",
              label: "Logs",
              icon: Activity,
              href: "/logs",
              keywords: "observabilidade panorama dashboard custo latência tokens feedback monitoramento langfuse",
            },
            {
              id: "nav-logs-sessions",
              group: "Navegar",
              label: "Logs: sessões",
              icon: Activity,
              href: "/logs/sessions",
              keywords: "conversas transcrição usuário",
            },
            {
              id: "nav-logs-runs",
              group: "Navegar",
              label: "Logs: execuções",
              icon: Activity,
              href: "/logs/runs",
              keywords: "runs traces revisão fila tool falhou",
            },
          ]
        : []),
      ...agents.flatMap((agent) => [
        {
          id: `chat-${agent.agent_type}`,
          group: "Agentes",
          label: `Conversar com ${agent.name}`,
          hint: agent.agent_type,
          icon: MessageSquare,
          href: `/chat?agent=${encodeURIComponent(agent.agent_type)}`,
        },
        {
          id: `agent-${agent.agent_type}`,
          group: "Agentes",
          label: `Configurar ${agent.name}`,
          hint: agent.agent_type,
          icon: Settings2,
          href: `/agents/${encodeURIComponent(agent.agent_type)}`,
          keywords: "editar prompt versões integração",
        },
      ]),
      ...sessions.map((session) => ({
        id: `session-${session.session_id}`,
        group: "Conversas",
        label: sessionTitle(session),
        hint: getAgent(session.agent_id)?.name ?? session.agent_id ?? undefined,
        icon: MessageSquare,
        href: `/chat/${encodeURIComponent(session.session_id)}`,
      })),
    ],
    [agents, sessions, getAgent, observability]
  );

  const filtered = useMemo(() => {
    const q = normalize(query.trim());
    if (!q) {
      // Sem busca: ações, navegação e só as conversas mais recentes.
      let recentSessions = 0;
      return items.filter((item) => item.group !== "Conversas" || recentSessions++ < 5);
    }
    return items.filter((item) =>
      normalize(`${item.label} ${item.hint ?? ""} ${item.keywords ?? ""} ${item.group}`).includes(q)
    );
  }, [items, query]);

  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  function run(item: PaletteItem | undefined) {
    if (!item) return;
    onClose();
    if (item.action === "toggle-theme") {
      setTheme(document.documentElement.classList.contains("dark") ? "light" : "dark");
    } else if (item.external && item.href) {
      window.open(item.href, "_blank", "noopener,noreferrer");
    } else if (item.href) {
      router.push(item.href);
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(filtered[activeIndex]);
    }
  }

  return (
    <div className="flex min-h-0 flex-col">
      <div className="flex h-12 items-center gap-3 border-b border-border px-4">
        <Search className="size-4 shrink-0 text-muted-foreground" />
        <input
          data-autofocus
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActiveIndex(0);
          }}
          onKeyDown={handleKeyDown}
          placeholder="Buscar agentes, conversas e ações…"
          aria-label="Buscar comandos"
          role="combobox"
          aria-expanded="true"
          aria-controls="command-palette-list"
          className="h-full flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
        />
        <Kbd>Esc</Kbd>
      </div>

      <div
        ref={listRef}
        id="command-palette-list"
        role="listbox"
        className="scrollbar-thin max-h-[min(60vh,420px)] overflow-y-auto p-2"
      >
        {filtered.length === 0 && (
          <p className="px-3 py-8 text-center text-sm text-muted-foreground">Nada encontrado para “{query}”.</p>
        )}
        {filtered.map((item, index) => {
          const showGroup = index === 0 || filtered[index - 1].group !== item.group;
          const active = index === activeIndex;
          return (
            <div key={item.id}>
              {showGroup && (
                <p className="px-2.5 pb-1 pt-2.5 text-[11px] font-medium text-muted-foreground first:pt-1">
                  {item.group}
                </p>
              )}
              <button
                type="button"
                role="option"
                aria-selected={active}
                data-active={active}
                onMouseMove={() => active || setActiveIndex(index)}
                onClick={() => run(item)}
                className={cn(
                  "flex h-9 w-full items-center gap-3 rounded-lg px-2.5 text-left text-sm",
                  active ? "bg-accent text-foreground" : "text-foreground/85"
                )}
              >
                <item.icon className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate">{item.label}</span>
                {item.hint && <span className="max-w-[40%] truncate text-xs text-muted-foreground">{item.hint}</span>}
              </button>
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-4 border-t border-border bg-surface px-4 py-2 text-[11px] text-muted-foreground">
        <span className="flex items-center gap-1">
          <Kbd>↑</Kbd>
          <Kbd>↓</Kbd> navegar
        </span>
        <span className="flex items-center gap-1">
          <Kbd>Enter</Kbd> abrir
        </span>
      </div>
    </div>
  );
}
