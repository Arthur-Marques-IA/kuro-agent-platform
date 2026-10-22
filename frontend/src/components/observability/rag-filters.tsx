"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";
import { requestJson } from "@/lib/http";
import { RAG_FILTERS, type RagFilterValue } from "@/lib/logs-query";
import type { Collection } from "@/lib/types";
import { Select } from "@/components/ui/select";
import { useStats } from "@/components/observability/stats-panel";

/**
 * O recorte da aba RAG: as execuções que buscaram na base, as que tiveram uma busca sem
 * trecho nenhum (a base não cobre a pergunta) e as com trecho marcado errado ainda aberto
 * (o documento precisa de correção). Cada botão mostra quantas há no período.
 */
export function RagFilterBar({
  value,
  collection,
  params,
  onChange,
  onCollectionChange,
}: {
  value: RagFilterValue;
  collection: string;
  /** Os filtros da página (período, agentes, status, testes), sem o recorte RAG. */
  params: Record<string, string | string[]>;
  onChange: (value: RagFilterValue) => void;
  onCollectionChange: (collection: string) => void;
}) {
  const collections = useCollectionNames();
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div role="radiogroup" aria-label="Recorte RAG" className="flex flex-wrap gap-2">
        {RAG_FILTERS.map((filter) => (
          <RagFilterButton
            key={filter.value}
            label={filter.label}
            active={filter.value === value}
            params={{ ...params, rag: filter.value }}
            onSelect={() => onChange(filter.value)}
          />
        ))}
      </div>
      <Select
        aria-label="Filtrar por collection"
        value={collection}
        onChange={(e) => onCollectionChange(e.target.value)}
        wrapperClassName="w-48"
      >
        <option value="">Todas as collections</option>
        {collections.map((c) => (
          <option key={c.name} value={c.name}>
            {c.label || c.name}
          </option>
        ))}
      </Select>
    </div>
  );
}

function RagFilterButton({
  label,
  active,
  params,
  onSelect,
}: {
  label: string;
  active: boolean;
  params: Record<string, string | string[]>;
  onSelect: () => void;
}) {
  const { stats } = useStats(params);
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onSelect}
      className={cn(
        "inline-flex h-8 items-center gap-2 rounded-full border px-3 text-xs font-medium transition-colors",
        "outline-none focus-visible:ring-2 focus-visible:ring-ring",
        active ? "border-primary bg-primary/10 text-foreground" : "border-border text-muted-foreground hover:text-foreground"
      )}
    >
      {label}
      <span className="tabular-nums text-muted-foreground">{stats ? formatNumber(stats.total_runs) : "…"}</span>
    </button>
  );
}

function useCollectionNames(): Collection[] {
  const [collections, setCollections] = useState<Collection[]>([]);
  useEffect(() => {
    let cancelled = false;
    requestJson<Collection[]>("/api/collections", { fallbackError: "Falha ao carregar as collections" })
      .then((rows) => {
        if (!cancelled) setCollections(rows);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);
  return collections;
}
