"use client";

import {
  ComponentType,
  KeyboardEvent,
  ReactNode,
  RefObject,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/cn";

interface TriggerProps {
  ref: RefObject<HTMLButtonElement | null>;
  onClick: () => void;
  "aria-haspopup": "menu";
  "aria-expanded": boolean;
}

interface Position {
  top?: number;
  bottom?: number;
  left?: number;
  right?: number;
}

const MenuContext = createContext<{ close: () => void } | null>(null);

/**
 * Menu suspenso posicionado em `fixed` via portal — não é cortado por
 * containers com overflow (ex. a lista de conversas rolável da sidebar).
 */
export function DropdownMenu({
  trigger,
  children,
  align = "start",
  side = "bottom",
  className,
}: {
  trigger: (props: TriggerProps) => ReactNode;
  children: ReactNode;
  align?: "start" | "end";
  side?: "bottom" | "top";
  className?: string;
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<Position | null>(null);
  const open = position !== null;

  const close = useCallback(() => setPosition(null), []);

  function toggle() {
    if (open) {
      close();
      return;
    }
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    setPosition({
      ...(side === "bottom" ? { top: rect.bottom + 4 } : { bottom: window.innerHeight - rect.top + 4 }),
      ...(align === "start" ? { left: rect.left } : { right: window.innerWidth - rect.right }),
    });
  }

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]:not([disabled])')?.focus();

    function onPointerDown(e: PointerEvent) {
      const target = e.target as Node;
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      close();
    }
    function onScrollOrResize(e: Event) {
      if (e.target instanceof Node && menuRef.current?.contains(e.target)) return;
      close();
    }
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("resize", onScrollOrResize);
    window.addEventListener("scroll", onScrollOrResize, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("resize", onScrollOrResize);
      window.removeEventListener("scroll", onScrollOrResize, true);
    };
  }, [open, close]);

  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Escape") {
      e.stopPropagation();
      close();
      triggerRef.current?.focus();
      return;
    }
    if (e.key === "Tab") {
      close();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') ?? []
    );
    const index = items.indexOf(document.activeElement as HTMLElement);
    const next = items[(index + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length];
    next?.focus();
  }

  return (
    <>
      {trigger({ ref: triggerRef, onClick: toggle, "aria-haspopup": "menu", "aria-expanded": open })}
      {open &&
        createPortal(
          <MenuContext.Provider value={{ close }}>
            <div
              ref={menuRef}
              role="menu"
              onKeyDown={handleKeyDown}
              style={position}
              className={cn(
                "fixed z-[60] min-w-48 animate-zoom-in rounded-xl border border-border bg-popover p-1 text-popover-foreground shadow-lg",
                className
              )}
            >
              {children}
            </div>
          </MenuContext.Provider>,
          document.body
        )}
    </>
  );
}

export function DropdownMenuItem({
  children,
  onSelect,
  icon: Icon,
  destructive,
  disabled,
  hint,
  className,
}: {
  children: ReactNode;
  onSelect?: () => void;
  icon?: ComponentType<{ className?: string }>;
  destructive?: boolean;
  disabled?: boolean;
  hint?: ReactNode;
  className?: string;
}) {
  const ctx = useContext(MenuContext);
  return (
    <button
      type="button"
      role="menuitem"
      disabled={disabled}
      onClick={() => {
        ctx?.close();
        onSelect?.();
      }}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left text-[13px] outline-none transition-colors max-md:min-h-10",
        "hover:bg-accent focus-visible:bg-accent disabled:pointer-events-none disabled:opacity-50",
        destructive && "text-destructive hover:bg-destructive/10 focus-visible:bg-destructive/10",
        className
      )}
    >
      {Icon && <Icon className="size-4 shrink-0 opacity-80" />}
      <span className="min-w-0 flex-1">{children}</span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </button>
  );
}

export function DropdownMenuLabel({ children }: { children: ReactNode }) {
  return <div className="px-2.5 pb-1 pt-1.5 text-[11px] font-medium text-muted-foreground">{children}</div>;
}

export function DropdownMenuSeparator() {
  return <div role="separator" className="-mx-1 my-1 h-px bg-border" />;
}
