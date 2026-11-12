"use client";

import { ReactNode, createContext, useCallback, useContext, useRef, useState } from "react";
import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import { announce } from "@/lib/announce";
import { cn } from "@/lib/cn";

type ToastVariant = "default" | "success" | "error";

interface ToastInput {
  title: string;
  description?: string;
  variant?: ToastVariant;
}

interface ToastItem extends Required<Pick<ToastInput, "title" | "variant">> {
  id: number;
  description?: string;
}

const ToastContext = createContext<((toast: ToastInput) => void) | null>(null);

const icons = { default: Info, success: CircleCheck, error: CircleAlert };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(0);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const toast = useCallback(
    ({ title, description, variant = "default" }: ToastInput) => {
      const id = ++nextId.current;
      setToasts((prev) => [...prev.slice(-3), { id, title, description, variant }]);
      // Lido pelo anunciador único (o mesmo do Playground e dos formulários), não por uma
      // região própria: erro interrompe, o resto espera a vez — e nada é lido duas vezes.
      announce(description ? `${title}. ${description}` : title, { assertive: variant === "error" });
      window.setTimeout(() => dismiss(id), variant === "error" ? 7000 : 4000);
    },
    [dismiss]
  );

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div
        className="pointer-events-none fixed inset-x-0 bottom-0 z-[100] flex flex-col items-center gap-2 p-4 sm:items-end"
      >
        {toasts.map((t) => {
          const Icon = icons[t.variant];
          return (
            <div
              key={t.id}
              className="pointer-events-auto flex w-full max-w-sm animate-slide-up items-start gap-3 rounded-xl border border-border bg-popover p-3.5 text-popover-foreground shadow-lg"
            >
              <Icon
                className={cn(
                  "mt-px size-4 shrink-0",
                  t.variant === "success" && "text-success",
                  t.variant === "error" && "text-destructive",
                  t.variant === "default" && "text-muted-foreground"
                )}
              />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">{t.title}</p>
                {t.description && (
                  <p className="mt-0.5 break-words text-[13px] text-muted-foreground">{t.description}</p>
                )}
              </div>
              <button
                type="button"
                onClick={() => dismiss(t.id)}
                className="touch-target relative rounded-md p-0.5 text-muted-foreground hover:text-foreground"
                aria-label="Dispensar"
              >
                <X className="size-3.5" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast precisa estar dentro de <ToastProvider>");
  return ctx;
}
