"use client";

import { Fragment, KeyboardEvent, ReactNode, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Search } from "lucide-react";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";
import { findMatches, toLines, type TextMatch } from "@/lib/agent-form";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogFooter, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { PromptDiff, diffStats } from "@/components/agents/prompt-diff";

/**
 * Editor do prompt em tela cheia: numeração das linhas (uma instrução por linha), busca com
 * destaque e o diff contra a versão salva. Edita o mesmo texto do campo do formulário.
 */
export function PromptEditorDialog({
  open,
  onOpenChange,
  value,
  onChange,
  saved,
  savedVersion,
  changed,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  value: string;
  onChange: (value: string) => void;
  /** As instruções salvas, exatamente como a API devolveu (`undefined` num agente novo). */
  saved?: string[];
  savedVersion?: number;
  /** O prompt difere do salvo (com a mesma normalização do formulário: uma instrução por linha). */
  changed: boolean;
}) {
  const [view, setView] = useState<"edit" | "diff">("edit");
  const lines = toLines(value);
  // Sem alteração real, nada de diff: uma instrução salva com quebra de linha dentro apareceria
  // como "alterada" só por ser mostrada uma por linha.
  const stats = saved && changed ? diffStats(saved, lines) : null;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="xl"
      className="h-[100dvh] max-h-[100dvh] rounded-none sm:h-[calc(100dvh-2rem)] sm:max-h-none sm:max-w-[min(100rem,calc(100vw-2rem))] sm:rounded-2xl"
    >
      <DialogHeader className="pb-3">
        <DialogTitle>Prompt</DialogTitle>
        <DialogDescription>
          Uma instrução por linha · {formatNumber(lines.length)} {lines.length === 1 ? "instrução" : "instruções"} ·{" "}
          {formatNumber(value.length)} caracteres
          {savedVersion !== undefined && <> · salvo como v{savedVersion}</>}
        </DialogDescription>
        {saved && (
          <div role="group" aria-label="Visualização" className="mt-2 flex w-fit gap-1 rounded-lg bg-muted p-0.5 text-[13px]">
            <ViewButton active={view === "edit"} onClick={() => setView("edit")}>
              Editar
            </ViewButton>
            <ViewButton active={view === "diff"} onClick={() => setView("diff")}>
              Diferenças
              {stats && (
                <span className="ml-1.5 font-mono text-xs">
                  <span className="text-success">+{stats.added}</span>{" "}
                  <span className="text-destructive">−{stats.removed}</span>
                </span>
              )}
            </ViewButton>
          </div>
        )}
      </DialogHeader>
      <div className="flex min-h-0 flex-1 flex-col px-4 pb-4 sm:px-6">
        {view === "diff" && saved ? (
          changed ? (
            <PromptDiff
              from={saved}
              to={lines}
              addedLabel="nova"
              removedLabel={`só na v${savedVersion ?? ""}`}
              className="min-h-0 flex-1"
            />
          ) : (
            <p className="rounded-lg border border-dashed border-border px-4 py-3 text-[13px] text-muted-foreground">
              Igual à versão salva{savedVersion !== undefined ? ` (v${savedVersion})` : ""}.
            </p>
          )
        ) : (
          <NumberedEditor value={value} onChange={onChange} />
        )}
      </div>
      <DialogFooter>
        <Button onClick={() => onOpenChange(false)}>Concluir</Button>
      </DialogFooter>
    </Dialog>
  );
}

function ViewButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "rounded-md px-2.5 py-1 font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        active ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
      )}
    >
      {children}
    </button>
  );
}

/**
 * Textarea com numeração de linhas e destaque da busca. Atrás do texto fica um "espelho" com a
 * mesma fonte e a mesma quebra: cada linha dele é uma linha da grade, então o número acompanha a
 * altura da linha mesmo quando ela quebra em várias, e os destaques da busca caem sobre o texto.
 */
function NumberedEditor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const mirrorRef = useRef<HTMLDivElement>(null);
  const matches = useMemo(() => findMatches(value, query), [value, query]);
  const current = matches.length ? Math.min(active, matches.length - 1) : -1;
  const rawLines = value.split("\n");
  const lineStarts = useMemo(() => {
    const starts = [0];
    for (let i = 0; i < value.length; i++) if (value[i] === "\n") starts.push(i + 1);
    return starts;
  }, [value]);
  const byLine = useMemo(() => {
    const map = new Map<number, { match: TextMatch; index: number }[]>();
    matches.forEach((match, index) => {
      const list = map.get(match.line) ?? [];
      list.push({ match, index });
      map.set(match.line, list);
    });
    return map;
  }, [matches]);

  function goTo(index: number) {
    if (!matches.length) return;
    const next = (index + matches.length) % matches.length;
    setActive(next);
    mirrorRef.current
      ?.querySelector<HTMLElement>(`[data-line="${matches[next].line}"]`)
      ?.scrollIntoView({ block: "center" });
  }

  function onSearchKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== "Enter") return;
    e.preventDefault();
    goTo(current + (e.shiftKey ? -1 : 1));
  }

  /** Leva o cursor do textarea para a ocorrência atual, para editar ali. */
  function editAtMatch() {
    const match = matches[current];
    const textarea = textareaRef.current;
    if (!match || !textarea) return;
    textarea.focus({ preventScroll: true });
    textarea.setSelectionRange(match.start, match.end);
  }

  return (
    <div
      className="flex min-h-0 flex-1 flex-col gap-2"
      onKeyDown={(e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
          e.preventDefault();
          e.currentTarget.querySelector<HTMLInputElement>("input[type=search]")?.focus();
        }
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-0 flex-1 sm:max-w-sm">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            onKeyDown={onSearchKeyDown}
            placeholder="Buscar no prompt (Enter: próxima)"
            aria-label="Buscar no prompt"
            className="h-8 pl-8"
          />
        </div>
        {query && (
          <span className="text-xs text-muted-foreground" aria-live="polite">
            {matches.length ? `${current + 1} de ${formatNumber(matches.length)}` : "Nada encontrado"}
          </span>
        )}
        <Button variant="ghost" size="icon-sm" aria-label="Ocorrência anterior" disabled={!matches.length} onClick={() => goTo(current - 1)}>
          <ChevronUp />
        </Button>
        <Button variant="ghost" size="icon-sm" aria-label="Próxima ocorrência" disabled={!matches.length} onClick={() => goTo(current + 1)}>
          <ChevronDown />
        </Button>
        {matches.length > 0 && (
          <Button variant="outline" size="sm" onClick={editAtMatch}>
            Editar aqui
          </Button>
        )}
      </div>
      <div className="scrollbar-thin min-h-0 flex-1 overflow-auto rounded-lg border border-input bg-background focus-within:ring-2 focus-within:ring-ring">
        <div
          ref={mirrorRef}
          className="relative grid grid-cols-[3rem_minmax(0,1fr)] py-2 font-mono text-[13px] leading-6"
        >
          {rawLines.map((line, i) => {
            const hits = byLine.get(i);
            const start = lineStarts[i] ?? 0;
            const segments: ReactNode[] = [];
            let cursor = 0;
            hits?.forEach(({ match, index }) => {
              const from = match.start - start;
              const to = match.end - start;
              segments.push(line.slice(cursor, from));
              segments.push(
                <mark
                  key={index}
                  className={cn("rounded-sm text-transparent", index === current ? "bg-warning/60" : "bg-warning/25")}
                >
                  {line.slice(from, to)}
                </mark>
              );
              cursor = to;
            });
            segments.push(line.slice(cursor));
            return (
              <Fragment key={i}>
                <span
                  aria-hidden
                  className={cn(
                    "select-none border-r border-border pr-2 text-right text-xs leading-6 text-muted-foreground/70",
                    line.trim() === "" && "text-muted-foreground/30"
                  )}
                >
                  {i + 1}
                </span>
                <span
                  aria-hidden
                  data-line={i}
                  className="whitespace-pre-wrap break-words px-3 text-transparent"
                >
                  {line === "" ? " " : segments}
                </span>
              </Fragment>
            );
          })}
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            aria-label="Instruções, uma por linha"
            spellCheck={false}
            className="absolute inset-y-0 left-12 right-0 resize-none overflow-hidden whitespace-pre-wrap break-words bg-transparent px-3 py-2 font-mono text-[13px] leading-6 text-foreground caret-foreground outline-none"
          />
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        Cada linha é uma instrução; linhas em branco são ignoradas ao salvar.{" "}
        <Badge variant="outline" className="align-middle">
          Ctrl+F
        </Badge>{" "}
        busca.
      </p>
    </div>
  );
}
