"use client";

import { ReactNode, createContext, useContext, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Menu } from "lucide-react";
import { cn } from "@/lib/cn";
import { useLocalStorage } from "@/lib/use-local-storage";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Sidebar } from "@/components/workspace/sidebar";
import { CommandPalette } from "@/components/workspace/command-palette";
import { KuroMascot } from "@/components/brand/kuro-mascot";
import { useWorkspace } from "@/components/workspace/workspace-provider";

const ShellContext = createContext<{ openMobileSidebar: () => void } | null>(null);

function isEditableTarget(target: EventTarget | null) {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
  );
}

/**
 * Casca do console: sidebar persistente (recolhível no desktop, gaveta no
 * mobile), área de conteúdo e atalhos globais — Ctrl/⌘+K paleta de comandos,
 * Ctrl/⌘+Shift+O nova conversa, Ctrl/⌘+B recolhe a sidebar.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const { setCommandOpen } = useWorkspace();
  const [collapsed, setCollapsed] = useLocalStorage<"0" | "1">("agent-service:sidebar-collapsed", "0");
  const [mobileOpen, setMobileOpen] = useState(false);
  const isCollapsed = collapsed === "1";

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey)) return;
      const key = e.key.toLowerCase();
      if (key === "k") {
        e.preventDefault();
        setCommandOpen((open) => !open);
      } else if (key === "o" && e.shiftKey) {
        e.preventDefault();
        router.push("/chat");
      } else if (key === "b" && !isEditableTarget(e.target)) {
        e.preventDefault();
        setCollapsed(isCollapsed ? "0" : "1");
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isCollapsed, setCollapsed, setCommandOpen, router]);

  return (
    <ShellContext.Provider value={{ openMobileSidebar: () => setMobileOpen(true) }}>
      <div className="flex h-dvh overflow-hidden bg-background">
        <aside
          className={cn(
            "hidden shrink-0 border-r border-border bg-surface transition-[width] duration-200 ease-out lg:flex",
            isCollapsed ? "w-[60px]" : "w-[272px]"
          )}
        >
          <Sidebar collapsed={isCollapsed} onToggleCollapsed={() => setCollapsed(isCollapsed ? "0" : "1")} />
        </aside>

        <Dialog open={mobileOpen} onOpenChange={setMobileOpen} side="left" hideClose className="bg-surface">
          <Sidebar onNavigate={() => setMobileOpen(false)} />
        </Dialog>

        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">{children}</main>
        <CommandPalette />
        <KuroMascot sidebarCollapsed={isCollapsed} />
      </div>
    </ShellContext.Provider>
  );
}

/** Botão que abre a sidebar no mobile — cada cabeçalho de página o inclui. */
export function SidebarTrigger({ className }: { className?: string }) {
  const ctx = useContext(ShellContext);
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      onClick={ctx?.openMobileSidebar}
      aria-label="Abrir menu"
      className={cn("-ml-1.5 lg:hidden", className)}
    >
      <Menu />
    </Button>
  );
}
