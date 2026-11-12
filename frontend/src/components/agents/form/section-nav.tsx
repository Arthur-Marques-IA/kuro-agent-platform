"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import type { FormSectionId, FormSectionInfo } from "@/lib/agent-form";

/**
 * Índice das seções do formulário: coluna fixa ao lado em telas largas, barra compacta
 * (rolagem horizontal) no topo em telas estreitas. Cada item marca se a seção tem alteração
 * não salva (ponto amarelo) ou erro (ponto vermelho), e a seção visível fica destacada.
 */
export function SectionNav({
  sections,
  anchorOf,
  changed,
  invalid,
  orientation,
}: {
  sections: FormSectionInfo[];
  anchorOf: (id: FormSectionId) => string;
  changed: Set<FormSectionId>;
  invalid: Set<FormSectionId>;
  orientation: "vertical" | "horizontal";
}) {
  const [active, setActive] = useState<FormSectionId | null>(null);
  const key = sections.map((s) => s.id).join(",");

  useEffect(() => {
    const ids = key.split(",") as FormSectionId[];
    let frame = 0;
    // A seção ativa é a última cujo título já passou do primeiro terço da tela; no topo, a primeira.
    function measure() {
      frame = 0;
      const threshold = window.innerHeight / 3;
      let current = ids[0];
      for (const id of ids) {
        const el = document.getElementById(anchorOf(id));
        if (el && el.getBoundingClientRect().top <= threshold) current = id;
      }
      setActive(current);
    }
    function onScroll() {
      if (!frame) frame = requestAnimationFrame(measure);
    }
    frame = requestAnimationFrame(measure);
    // `capture`: a rolagem acontece num contêiner da página, não na janela, e `scroll` não sobe.
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      document.removeEventListener("scroll", onScroll, { capture: true });
      window.removeEventListener("resize", onScroll);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [key, anchorOf]);

  function go(id: FormSectionId) {
    setActive(id);
    document.getElementById(anchorOf(id))?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  return (
    <nav aria-label="Seções do formulário">
      <ul
        className={cn(
          orientation === "vertical"
            ? "flex flex-col gap-0.5"
            : "scrollbar-thin flex gap-1 overflow-x-auto whitespace-nowrap"
        )}
      >
        {sections.map((section) => {
          const hasError = invalid.has(section.id);
          const hasChange = changed.has(section.id);
          const current = active === section.id;
          return (
            <li key={section.id} className="shrink-0">
              <button
                type="button"
                onClick={() => go(section.id)}
                aria-current={current ? "location" : undefined}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md text-left text-[13px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  orientation === "vertical" ? "px-2.5 py-1.5" : "px-2.5 py-1",
                  current
                    ? "bg-accent font-medium text-foreground"
                    : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                  hasError && "text-destructive hover:text-destructive"
                )}
              >
                <span className="min-w-0 flex-1 truncate">{section.label}</span>
                {(hasError || hasChange) && (
                  <span
                    className={cn("size-1.5 shrink-0 rounded-full", hasError ? "bg-destructive" : "bg-warning")}
                    aria-hidden
                  />
                )}
                {hasError ? (
                  <span className="sr-only">(com erro)</span>
                ) : hasChange ? (
                  <span className="sr-only">(alterada)</span>
                ) : null}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
