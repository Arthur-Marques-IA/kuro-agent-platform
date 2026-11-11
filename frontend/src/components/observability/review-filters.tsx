"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Dices } from "lucide-react";
import { requestJson } from "@/lib/http";
import type { ToolSummary } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

/**
 * A fila de revisão: filtros que apontam o que vale um humano olhar, em vez de
 * uma lista com centenas de "ok" e "oi". Cada filtro é visível e combinável — o
 * revisor sabe por que um run está na fila. A amostra aleatória existe porque o
 * erro silencioso (o agente falou um preço sem chamar a tool que valida) não
 * aparece em filtro nenhum.
 */

export interface ReviewState {
  complexity: number[];
  toolFailed: boolean;
  sideEffect: boolean;
  hideShort: boolean;
  feedbackDown: boolean;
  includeTests: boolean;
  /** Quando preenchido, a lista é uma amostra aleatória (o número muda para sortear de novo). */
  sampleSeed: number | null;
}

export const EMPTY_REVIEW: ReviewState = {
  complexity: [],
  toolFailed: false,
  sideEffect: false,
  hideShort: false,
  feedbackDown: false,
  includeTests: false,
  sampleSeed: null,
};

/** Mensagens mais curtas que isto ("ok", "oi", "obrigado") somem com "Esconder curtas". */
export const SHORT_MESSAGE_CHARS = 20;
export const SAMPLE_SIZE = 20;

export function reviewParams(review: ReviewState): Record<string, string | string[]> {
  const p: Record<string, string | string[]> = {};
  if (review.complexity.length) p.complexity = review.complexity.map(String);
  if (review.toolFailed) p.tool_failed = "true";
  if (review.sideEffect) p.side_effect = "true";
  if (review.hideShort) p.min_message_chars = String(SHORT_MESSAGE_CHARS);
  if (review.feedbackDown) p.feedback = "down";
  if (!review.includeTests) p.include_dry_run = "false";
  if (review.sampleSeed != null) {
    p.sample = String(SAMPLE_SIZE);
    p._seed = String(review.sampleSeed); // só para a lista recarregar ao sortear de novo
  }
  return p;
}

const COMPLEXITY_HELP: Record<number, string> = {
  1: "Nenhuma tool de negócio (memória e busca na base não contam)",
  2: "Uma ou duas tools distintas",
  3: "Três ou mais tools distintas",
};

function Chip({
  active,
  onClick,
  children,
  title,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      title={title}
      onClick={onClick}
      className={cn(
        "inline-flex h-10 items-center gap-1.5 rounded-full border px-3 text-xs transition-colors md:h-8",
        active ? "border-primary bg-primary-soft font-medium text-foreground" : "border-border text-muted-foreground hover:text-foreground"
      )}
    >
      {children}
    </button>
  );
}

function useUnclassifiedTools(): number | null {
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    let cancelled = false;
    requestJson<ToolSummary[]>("/api/tools", { fallbackError: "" })
      .then((tools) => {
        if (!cancelled) setCount(tools.filter((t) => t.side_effect == null && t.enabled).length);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  return count;
}

export function ReviewFilters({ review, onChange }: { review: ReviewState; onChange: (next: ReviewState) => void }) {
  const unclassified = useUnclassifiedTools();
  const set = (patch: Partial<ReviewState>) => onChange({ ...review, ...patch });
  const toggleComplexity = (level: number) =>
    set({
      complexity: review.complexity.includes(level)
        ? review.complexity.filter((c) => c !== level)
        : [...review.complexity, level].sort(),
    });

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-border bg-card px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-xs font-medium">Revisar</span>
        <span className="text-xs text-muted-foreground">Complexidade</span>
        {[1, 2, 3].map((level) => (
          <Chip key={level} active={review.complexity.includes(level)} onClick={() => toggleComplexity(level)} title={COMPLEXITY_HELP[level]}>
            {level}
          </Chip>
        ))}
        <span className="mx-1 h-4 w-px bg-border" aria-hidden />
        <Chip active={review.toolFailed} onClick={() => set({ toolFailed: !review.toolFailed })} title="O agente respondeu, mas alguma tool falhou">
          Tool falhou
        </Chip>
        <Chip
          active={review.sideEffect}
          onClick={() => set({ sideEffect: !review.sideEffect })}
          title="Chamou uma tool que grava, cobra, transfere ou envia algo"
        >
          Efeito colateral
        </Chip>
        <Chip active={review.feedbackDown} onClick={() => set({ feedbackDown: !review.feedbackDown })}>
          👎 Feedback negativo
        </Chip>
        <Chip
          active={review.hideShort}
          onClick={() => set({ hideShort: !review.hideShort })}
          title={`Esconde mensagens com menos de ${SHORT_MESSAGE_CHARS} caracteres ("ok", "oi")`}
        >
          Esconder mensagens curtas
        </Chip>
        <Chip
          active={review.includeTests}
          onClick={() => set({ includeTests: !review.includeTests })}
          title="Execuções do Playground e de testes com dry_run"
        >
          Incluir testes
        </Chip>
        <span className="mx-1 h-4 w-px bg-border" aria-hidden />
        <Button
          variant={review.sampleSeed != null ? "secondary" : "ghost"}
          size="sm"
          className="h-10 md:h-8"
          onClick={() => set({ sampleSeed: Date.now() })}
          title="Sorteia execuções do recorte atual — o erro silencioso não aparece em filtro nenhum"
        >
          <Dices /> {review.sampleSeed != null ? "Sortear de novo" : `Amostra de ${SAMPLE_SIZE}`}
        </Button>
        {review.sampleSeed != null && (
          <Button variant="ghost" size="sm" className="h-10 md:h-8" onClick={() => set({ sampleSeed: null })}>
            Voltar às mais recentes
          </Button>
        )}
      </div>
      {review.sideEffect && !!unclassified && (
        <p className="text-xs text-warning">
          {unclassified} {unclassified === 1 ? "tool ativa ainda não está classificada" : "tools ativas ainda não estão classificadas"} —
          execuções com elas ficam de fora deste filtro.{" "}
          <Link href="/tools" className="underline">
            Classificar em Tools
          </Link>
        </p>
      )}
    </div>
  );
}
