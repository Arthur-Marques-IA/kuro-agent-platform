"use client";

import {
  KeyboardEvent,
  ReactNode,
  createContext,
  useCallback,
  useContext,
  useId,
  useState,
} from "react";
import { cn } from "@/lib/cn";

type TabsVariant = "line" | "pill";

interface TabsContextValue {
  value: string;
  setValue: (value: string) => void;
  baseId: string;
  variant: TabsVariant;
}

const TabsContext = createContext<TabsContextValue | null>(null);

function useTabsContext() {
  const ctx = useContext(TabsContext);
  if (!ctx) throw new Error("Tabs.* precisa estar dentro de <Tabs>");
  return ctx;
}

/** Controlado (`value` + `onValueChange`) ou não (`defaultValue`). */
export function Tabs({
  value,
  defaultValue,
  onValueChange,
  variant = "line",
  children,
  className,
}: {
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  variant?: TabsVariant;
  children: ReactNode;
  className?: string;
}) {
  const [internalValue, setInternalValue] = useState(defaultValue ?? "");
  const baseId = useId();
  const current = value ?? internalValue;

  const setValue = useCallback(
    (next: string) => {
      if (value === undefined) setInternalValue(next);
      onValueChange?.(next);
    },
    [value, onValueChange]
  );

  return (
    <TabsContext.Provider value={{ value: current, setValue, baseId, variant }}>
      <div className={className}>{children}</div>
    </TabsContext.Provider>
  );
}

export function TabsList({ children, className }: { children: ReactNode; className?: string }) {
  const { variant } = useTabsContext();

  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const tabs = Array.from(
      e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]:not([disabled])')
    );
    const index = tabs.indexOf(document.activeElement as HTMLButtonElement);
    if (index === -1) return;
    e.preventDefault();
    const next = tabs[(index + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
    next.focus();
    next.click();
  }

  return (
    <div
      role="tablist"
      onKeyDown={handleKeyDown}
      className={cn(
        variant === "pill"
          ? "inline-flex items-center gap-1 rounded-lg bg-muted p-1"
          : // Linha de base como sombra interna: o sublinhado da aba ativa pinta por cima.
            "scrollbar-thin flex items-center gap-6 overflow-x-auto shadow-[inset_0_-1px_0_var(--border)]",
        className
      )}
    >
      {children}
    </div>
  );
}

export function TabsTrigger({
  value,
  children,
  count,
  disabled,
  className,
}: {
  value: string;
  children: ReactNode;
  count?: number;
  disabled?: boolean;
  className?: string;
}) {
  const ctx = useTabsContext();
  const active = ctx.value === value;

  return (
    <button
      type="button"
      role="tab"
      id={`${ctx.baseId}-tab-${value}`}
      aria-controls={`${ctx.baseId}-panel-${value}`}
      aria-selected={active}
      tabIndex={active ? 0 : -1}
      disabled={disabled}
      onClick={() => ctx.setValue(value)}
      className={cn(
        "inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap text-sm font-medium outline-none transition-colors",
        "focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4",
        ctx.variant === "pill"
          ? [
              "touch-target relative h-7 rounded-md px-3 text-[13px]",
              active ? "bg-card text-foreground shadow-xs" : "text-muted-foreground hover:text-foreground",
            ]
          : [
              "h-10 border-b-2 px-0.5",
              active
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            ],
        className
      )}
    >
      {children}
      {count !== undefined && (
        <span className="rounded-full bg-muted px-1.5 text-[11px] leading-[18px] tabular-nums text-muted-foreground">
          {count}
        </span>
      )}
    </button>
  );
}

/**
 * `forceMount` mantém o conteúdo montado (só escondido) quando a aba não está
 * ativa — preserva estado local, ex. um formulário com edições não salvas.
 */
export function TabsContent({
  value,
  children,
  className,
  forceMount,
}: {
  value: string;
  children: ReactNode;
  className?: string;
  forceMount?: boolean;
}) {
  const ctx = useTabsContext();
  const active = ctx.value === value;
  if (!active && !forceMount) return null;
  return (
    <div
      role="tabpanel"
      id={`${ctx.baseId}-panel-${value}`}
      aria-labelledby={`${ctx.baseId}-tab-${value}`}
      hidden={!active}
      className={cn("outline-none", className)}
    >
      {children}
    </div>
  );
}
