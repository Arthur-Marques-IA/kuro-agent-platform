"use client";

import {
  HTMLAttributes,
  KeyboardEvent,
  ReactNode,
  createContext,
  useContext,
  useEffect,
  useId,
  useRef,
} from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";
import { overlayLayers } from "@/lib/overlay-layers";
import { useIsClient } from "@/lib/use-local-storage";

type DialogSize = "sm" | "md" | "lg" | "xl";
type DialogSide = "center" | "right" | "left";

interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
  size?: DialogSize;
  /** `center` = modal; `right`/`left` = painel lateral (sheet). */
  side?: DialogSide;
  className?: string;
  hideClose?: boolean;
}

const sizeClasses: Record<DialogSize, string> = {
  sm: "sm:max-w-sm",
  md: "sm:max-w-lg",
  lg: "sm:max-w-2xl",
  xl: "sm:max-w-4xl",
};

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

const DialogContext = createContext<{ titleId: string } | null>(null);

/**
 * Modal/sheet sem dependência nova: portal no <body>, foco preso dentro do
 * painel, Esc fecha, scroll da página travado e foco devolvido ao fechar.
 * Um elemento com `data-autofocus` recebe o foco inicial.
 */
export function Dialog({
  open,
  onOpenChange,
  children,
  size = "md",
  side = "center",
  className,
  hideClose,
}: DialogProps) {
  const isClient = useIsClient();
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    if (!open) return;
    // Abrir por cima fecha os menus abertos (ex.: Preferências sob a paleta do Ctrl+K). Um
    // diálogo nunca é dispensado por outra camada, então o `dismiss` não é chamado.
    const releaseLayer = overlayLayers.open("modal", () => {});
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const panel = panelRef.current;
    (panel?.querySelector<HTMLElement>("[data-autofocus]") ?? panel)?.focus();
    return () => {
      releaseLayer();
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus?.();
    };
  }, [open]);

  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Escape") {
      e.stopPropagation();
      onOpenChange(false);
      return;
    }
    if (e.key !== "Tab" || !panelRef.current) return;
    const focusables = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (focusables.length === 0) {
      e.preventDefault();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === panelRef.current)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }

  if (!open || !isClient) return null;

  return createPortal(
    <DialogContext.Provider value={{ titleId }}>
      <div
        className={cn(
          "fixed inset-0 z-50 flex",
          side === "center" && "items-end justify-center sm:items-center sm:p-4",
          side === "right" && "justify-end",
          side === "left" && "justify-start"
        )}
      >
        <div
          aria-hidden
          className="absolute inset-0 animate-fade-in bg-black/40 backdrop-blur-[2px] dark:bg-black/60"
          onClick={() => onOpenChange(false)}
        />
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          tabIndex={-1}
          onKeyDown={handleKeyDown}
          className={cn(
            "relative z-10 flex w-full flex-col overflow-hidden border-border bg-popover text-popover-foreground shadow-2xl outline-none",
            side === "center" && [
              "max-h-[92dvh] animate-zoom-in rounded-t-2xl border sm:rounded-2xl",
              sizeClasses[size],
            ],
            side === "right" && "h-full max-w-md animate-slide-in-right border-l",
            side === "left" && "h-full max-w-[18rem] animate-slide-in-left border-r",
            className
          )}
        >
          {!hideClose && (
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              className="touch-target absolute right-3 top-3 z-10 rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label="Fechar"
            >
              <X className="size-4" />
            </button>
          )}
          {children}
        </div>
      </div>
    </DialogContext.Provider>,
    document.body
  );
}

export function DialogHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1 px-6 pb-4 pr-12 pt-5", className)} {...props} />;
}

export function DialogTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  const ctx = useContext(DialogContext);
  return <h2 id={ctx?.titleId} className={cn("text-base font-semibold", className)} {...props} />;
}

export function DialogDescription({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-sm text-muted-foreground", className)} {...props} />;
}

export function DialogBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("scrollbar-thin flex-1 overflow-y-auto px-6 pb-6", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex flex-col-reverse gap-2 border-t border-border bg-surface px-6 py-3.5 sm:flex-row sm:justify-end",
        className
      )}
      {...props}
    />
  );
}
