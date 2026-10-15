"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Search, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { PERIODS, hasCustomRange, isoDay, localDay, type LogsQuery, type RunStatusValue } from "@/lib/logs-query";
import { fieldControlClasses } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { RUN_STATUS_OPTIONS } from "@/components/observability/run-status";
import { useWorkspace } from "@/components/workspace/workspace-provider";

/**
 * Os filtros do recorte dos Logs: período (atalho ou intervalo livre), agentes (vários, com busca) e
 * status (vários). Valem para o Panorama, as Sessões e as Execuções, e o estado fica na URL
 * (`lib/logs-query.ts`).
 */

// -- Período ---------------------------------------------------------------------

const CUSTOM = "custom";

export function PeriodPicker({
  query,
  onChange,
}: {
  query: Pick<LogsQuery, "period" | "from" | "to">;
  onChange: (patch: Partial<LogsQuery>) => void;
}) {
  const custom = hasCustomRange(query);
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select
        aria-label="Filtrar por período"
        value={custom ? CUSTOM : query.period}
        onChange={(e) => {
          const value = e.target.value;
          if (value === CUSTOM) {
            // Começa pelos últimos 7 dias, inteiros: daí a pessoa ajusta as pontas.
            const today = new Date();
            const weekAgo = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 6);
            onChange({ from: localDay(weekAgo), to: localDay(today) });
          } else {
            onChange({ period: value as LogsQuery["period"], from: "", to: "" });
          }
        }}
        wrapperClassName="w-48"
      >
        {PERIODS.map((period) => (
          <option key={period.value} value={period.value}>
            {period.label}
          </option>
        ))}
        <option value={CUSTOM}>Intervalo personalizado…</option>
      </Select>
      {custom && (
        <div className="flex items-center gap-1.5 text-[13px] text-muted-foreground">
          <DateField
            label="De"
            ariaLabel="Data inicial (inclusa)"
            value={query.from}
            max={query.to}
            onCommit={(from) => onChange({ from })}
          />
          <DateField
            label="até"
            ariaLabel="Data final (inclusa)"
            value={query.to}
            min={query.from}
            onCommit={(to) => onChange({ to })}
          />
        </div>
      )}
    </div>
  );
}

/**
 * Campo de data com rascunho local: o navegador avisa a cada tecla do ano (`0002-…`, `0202-…`), e só uma
 * data plausível (`isoDay`) ou o campo vazio vira recorte. Sem o rascunho, devolver o valor anterior ao
 * campo controlado apagaria o que a pessoa está digitando.
 */
function DateField({
  label,
  ariaLabel,
  value,
  min,
  max,
  onCommit,
}: {
  label: string;
  ariaLabel: string;
  value: string;
  min?: string;
  max?: string;
  onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  const [seen, setSeen] = useState(value);
  if (seen !== value) {
    // O valor mudou por fora (outro atalho, link): o rascunho acompanha.
    setSeen(value);
    setDraft(value);
  }
  return (
    <label className="inline-flex items-center gap-1.5">
      {label}
      <input
        type="date"
        value={draft}
        min={min || undefined}
        max={max || undefined}
        onChange={(e) => {
          const next = e.target.value;
          setDraft(next);
          if (next === "" || isoDay(next)) onCommit(next);
        }}
        className={cn(fieldControlClasses, "h-9 w-[9.5rem] px-2 tabular-nums")}
        aria-label={ariaLabel}
      />
    </label>
  );
}

// -- Multi-seleção ---------------------------------------------------------------

interface Option {
  value: string;
  label: string;
  /** Segunda linha discreta (o `agent_type`, que desempata nomes repetidos). */
  hint?: string;
}

/**
 * Lista com caixas de seleção num painel suspenso: marcar não fecha (o `DropdownMenu` fecha a cada item).
 * Nada marcado = todos. Com `searchable`, um campo filtra por rótulo e por `hint`.
 */
function MultiSelect({
  options,
  value,
  onChange,
  label,
  allLabel,
  pluralLabel,
  searchable = false,
  searchPlaceholder,
  className,
}: {
  options: Option[];
  value: string[];
  onChange: (value: string[]) => void;
  /** Nome do filtro para leitores de tela ("Filtrar por agente"). */
  label: string;
  allLabel: string;
  /** "agentes", "status": o resumo com mais de um marcado vira "3 agentes". */
  pluralLabel: string;
  searchable?: boolean;
  searchPlaceholder?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listId = useId();

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  const selected = options.filter((o) => value.includes(o.value));
  const summary =
    selected.length === 0 ? allLabel : selected.length === 1 ? selected[0].label : `${selected.length} ${pluralLabel}`;
  const needle = term.trim().toLowerCase();
  const visible = needle
    ? options.filter((o) => o.label.toLowerCase().includes(needle) || o.hint?.toLowerCase().includes(needle))
    : options;
  const toggle = (option: string) =>
    onChange(value.includes(option) ? value.filter((v) => v !== option) : [...value, option]);

  return (
    <div
      ref={rootRef}
      className={cn("relative", className)}
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.stopPropagation();
          setOpen(false);
          triggerRef.current?.focus();
        }
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        aria-label={`${label}: ${summary}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        onClick={() => {
          setOpen((o) => !o);
          setTerm("");
        }}
        className={cn(
          fieldControlClasses,
          "inline-flex h-9 items-center gap-2 pl-3 pr-2.5 text-left",
          selected.length > 0 && "border-primary/60"
        )}
      >
        <span className="min-w-0 flex-1 truncate">{summary}</span>
        <ChevronDown className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={label}
          className="absolute left-0 top-full z-50 mt-1 w-72 max-w-[calc(100vw-2rem)] animate-zoom-in rounded-xl border border-border bg-popover p-1 text-popover-foreground shadow-lg"
        >
          {searchable && (
            <div className="relative p-1">
              <Search className="pointer-events-none absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
              <input
                autoFocus
                type="search"
                value={term}
                onChange={(e) => setTerm(e.target.value)}
                placeholder={searchPlaceholder}
                aria-label={searchPlaceholder}
                className={cn(fieldControlClasses, "h-8 pl-8 pr-2")}
              />
            </div>
          )}
          <div className="flex items-center justify-between px-2.5 py-1 text-[11px] text-muted-foreground">
            <span>{selected.length ? `${selected.length} marcado${selected.length > 1 ? "s" : ""}` : allLabel}</span>
            {selected.length > 0 && (
              <button type="button" onClick={() => onChange([])} className="font-medium hover:text-foreground">
                Limpar
              </button>
            )}
          </div>
          <ul id={listId} role="group" aria-label={label} className="scrollbar-thin max-h-72 overflow-y-auto">
            {visible.map((option) => {
              const checked = value.includes(option.value);
              return (
                <li key={option.value}>
                  <label className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-[13px] hover:bg-accent has-[:focus-visible]:bg-accent">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggle(option.value)}
                      className="peer sr-only"
                    />
                    <span
                      className={cn(
                        "flex size-4 shrink-0 items-center justify-center rounded border",
                        checked ? "border-primary bg-primary text-primary-foreground" : "border-input"
                      )}
                      aria-hidden
                    >
                      {checked && <Check className="size-3" />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{option.label}</span>
                      {option.hint && option.hint !== option.label && (
                        <span className="block truncate font-mono text-[11px] text-muted-foreground">{option.hint}</span>
                      )}
                    </span>
                  </label>
                </li>
              );
            })}
            {visible.length === 0 && <li className="px-2.5 py-2 text-[13px] text-muted-foreground">Nada encontrado</li>}
          </ul>
        </div>
      )}
    </div>
  );
}

// -- Agentes e status ------------------------------------------------------------

export function AgentMultiSelect({ value, onChange }: { value: string[]; onChange: (agents: string[]) => void }) {
  const { agents } = useWorkspace();
  const options = useMemo(() => {
    const known: Option[] = agents
      .map((agent) => ({ value: agent.agent_type, label: agent.name || agent.agent_type, hint: agent.agent_type }))
      .sort((a, b) => a.label.localeCompare(b.label, "pt-BR") || a.value.localeCompare(b.value));
    // Um agente removido ainda tem histórico: o link do Panorama não pode cair em "Todos".
    const removed = value
      .filter((v) => !agents.some((agent) => agent.agent_type === v))
      .map((v) => ({ value: v, label: v, hint: "removido" }));
    return [...known, ...removed];
  }, [agents, value]);
  return (
    <MultiSelect
      options={options}
      value={value}
      onChange={onChange}
      label="Filtrar por agente"
      allLabel="Todos os agentes"
      pluralLabel="agentes"
      searchable
      searchPlaceholder="Buscar agente…"
      className="w-56"
    />
  );
}

export function StatusMultiSelect({
  value,
  onChange,
}: {
  value: RunStatusValue[];
  onChange: (statuses: RunStatusValue[]) => void;
}) {
  return (
    <MultiSelect
      options={RUN_STATUS_OPTIONS}
      value={value}
      // Na ordem fixa (sucesso, erro, interrompida): a URL de um mesmo recorte é sempre a mesma.
      onChange={(next) => onChange(RUN_STATUS_OPTIONS.map((o) => o.value).filter((s) => next.includes(s)))}
      label="Filtrar por status"
      allLabel="Todos os status"
      pluralLabel="status"
      className="w-44"
    />
  );
}

/** Um filtro que veio de um link (usuário, sessão, versão, ordenação), removível. */
export function FixedFilter({ label, value, onRemove }: { label: string; value: string; onRemove: () => void }) {
  return (
    <span className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-border bg-surface px-3 text-sm">
      {label}: <span className="max-w-40 truncate font-mono text-xs">{value}</span>
      <button
        type="button"
        onClick={onRemove}
        className="text-muted-foreground hover:text-foreground"
        aria-label={`Remover filtro de ${label.toLowerCase()}`}
      >
        <X className="size-3.5" />
      </button>
    </span>
  );
}
