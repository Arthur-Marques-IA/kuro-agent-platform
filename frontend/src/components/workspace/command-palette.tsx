"use client";

import { ComponentType, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Activity,
  Bot,
  Cpu,
  FileUp,
  Hash,
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
import { detectId, filterEntries, idLookup, type IdTarget } from "@/lib/command-search";
import { requestJson } from "@/lib/http";
import { sessionTitle } from "@/lib/sessions";
import { useTheme } from "@/lib/theme";
import type { Collection, RunPage, ToolSummary } from "@/lib/types";
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

const ID_GROUP = "Ir para o id";
const TOOLS_GROUP = "Tools";
const COLLECTIONS_GROUP = "Bases de conhecimento";
const SESSIONS_GROUP = "Conversas deste navegador";

const FILTER_OPTIONS = {
  // São muitas: só aparecem quando a pessoa digita algo.
  searchOnlyGroups: [TOOLS_GROUP, COLLECTIONS_GROUP],
  limitPerGroup: { [TOOLS_GROUP]: 6, [COLLECTIONS_GROUP]: 6 },
  // Sem busca: ações, navegação e só as conversas mais recentes.
  emptyQueryLimit: { [SESSIONS_GROUP]: 5 },
};

type Lookup = { id: string; found: Record<IdTarget, boolean> } | null;

/**
 * Tools e collections frescas ao abrir a paleta (este componente só monta com ela aberta).
 * O layout já trouxe uma lista, que vale até a resposta chegar — e continua valendo se a
 * chamada falhar, sem aviso: a busca não é lugar de erro de rede.
 */
function useFreshList<T>(url: string, seed: T[]): T[] {
  const [fresh, setFresh] = useState<T[] | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    requestJson<T[]>(url, { signal: controller.signal })
      .then((data) => {
        if (Array.isArray(data)) setFresh(data);
      })
      .catch(() => {});
    return () => controller.abort();
  }, [url]);
  return fresh ?? seed;
}

/**
 * Confere, sem travar nada, se o id colado existe: a busca das execuções casa o `run_id`
 * inteiro e o `session_id`. Enter navega na hora; a resposta só muda a dica.
 */
function useIdLookup(id: string | null, enabled: boolean): Lookup {
  const [lookup, setLookup] = useState<Lookup>(null);
  useEffect(() => {
    if (!id || !enabled) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ search: id, limit: "5" });
      requestJson<RunPage>(`/api/observability/runs?${params}`, { signal: controller.signal })
        .then((page) => setLookup({ id, found: idLookup(id, page?.items ?? []) }))
        .catch(() => {});
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [id, enabled]);
  return lookup && lookup.id === id ? lookup : null;
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
  const { agents, sessions, getAgent, observability, availableTools, collections: seedCollections } = useWorkspace();
  const tools = useFreshList<ToolSummary>("/api/tools", availableTools);
  const collections = useFreshList<Collection>("/api/collections", seedCollections);
  const [, setTheme] = useTheme();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const detected = useMemo(() => detectId(query), [query]);
  const lookup = useIdLookup(detected?.id ?? null, observability.enabled);

  const items = useMemo<PaletteItem[]>(
    () => [
      { id: "new-chat", group: "Ações", label: "Nova conversa", icon: SquarePen, href: "/chat", keywords: "chat playground" },
      { id: "new-agent", group: "Ações", label: "Criar agente", icon: Plus, href: "/agents/new" },
      { id: "add-content", group: "Ações", label: "Adicionar conteúdo à base", icon: FileUp, href: "/knowledge?add=1", keywords: "upload documento rag ingestão" },
      { id: "toggle-theme", group: "Ações", label: "Alternar tema claro/escuro", icon: Moon, action: "toggle-theme", keywords: "dark light" },
      { id: "nav-chat", group: "Navegar", label: "Playground", icon: MessageSquare, href: "/chat" },
      { id: "nav-agents", group: "Navegar", label: "Agentes", icon: Bot, href: "/agents" },
      { id: "nav-tools", group: "Navegar", label: "Tools", icon: Wrench, href: "/tools", keywords: "api python builtin função" },
      { id: "nav-models", group: "Navegar", label: "Credenciais de modelos", icon: Cpu, href: "/models", keywords: "chaves de api provedores openai anthropic google gemini ollama chave api key llm credencial" },
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
              keywords: "conversas transcrição usuário todas as sessões",
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
      ...tools.map((tool) => ({
        id: `tool-${tool.tool_name}`,
        group: TOOLS_GROUP,
        label: tool.label || tool.tool_name,
        hint: tool.tool_name,
        icon: Wrench,
        href: `/tools?q=${encodeURIComponent(tool.tool_name)}`,
        keywords: `${tool.kind} ${tool.description ?? ""}`,
      })),
      ...collections.map((collection) => ({
        id: `collection-${collection.name}`,
        group: COLLECTIONS_GROUP,
        label: collection.label || collection.name,
        hint: collection.name,
        icon: Library,
        href: `/knowledge?collection=${encodeURIComponent(collection.name)}`,
        keywords: "collection documentos rag",
      })),
      ...sessions.map((session) => ({
        id: `session-${session.session_id}`,
        group: SESSIONS_GROUP,
        label: sessionTitle(session),
        hint: getAgent(session.agent_id)?.name ?? session.agent_id ?? undefined,
        icon: MessageSquare,
        href: `/chat/${encodeURIComponent(session.session_id)}`,
        // Colar o id de uma conversa daqui também a encontra.
        keywords: session.session_id,
      })),
    ],
    [agents, sessions, getAgent, observability, tools, collections]
  );

  const idItems = useMemo<PaletteItem[]>(() => {
    if (!detected) return [];
    const { id, targets } = detected;
    const hint = (target: IdTarget) =>
      lookup ? (lookup.found[target] ? "encontrada nos Logs" : "não encontrada nos Logs") : undefined;
    // Se a conferência achou só a sessão, ela vem primeiro.
    const ordered = lookup && lookup.found.session && !lookup.found.run ? [...targets].reverse() : targets;
    return ordered.map((target) =>
      target === "run"
        ? {
            id: `id-run-${id}`,
            group: ID_GROUP,
            label: `Abrir execução ${id}`,
            hint: hint("run"),
            icon: Hash,
            href: `/runs/${encodeURIComponent(id)}`,
          }
        : {
            id: `id-session-${id}`,
            group: ID_GROUP,
            label: `Abrir sessão ${id}`,
            hint: hint("session"),
            icon: Hash,
            href: `/logs/sessions/${encodeURIComponent(id)}`,
          }
    );
  }, [detected, lookup]);

  // O id colado vem primeiro, fora do filtro; depois o resto, filtrado e ordenado.
  const filtered = useMemo(
    () => [...idItems, ...filterEntries(items, query, FILTER_OPTIONS)],
    [idItems, items, query]
  );

  // A lista muda sob o cursor quando as tools/collections ou a dica do id chegam.
  const safeIndex = Math.min(activeIndex, Math.max(filtered.length - 1, 0));

  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [safeIndex]);

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
      setActiveIndex(Math.min(safeIndex + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex(Math.max(safeIndex - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(filtered[safeIndex]);
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
          placeholder="Buscar agentes, tools, bases e conversas, ou colar um id…"
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
          const active = index === safeIndex;
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
