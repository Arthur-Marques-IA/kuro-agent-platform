"use client";

import { FormEvent, useId, useState } from "react";
import { ArrowRight, Search } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import type { SearchResult } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { EmptyState, Spinner } from "@/components/ui/primitives";
import { ChunkList } from "@/components/knowledge/chunk-list";

type SearchState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "done"; query: string; results: SearchResult[] }
  | { status: "error"; message: string };

export function SearchPanel({ collection }: { collection: string }) {
  const id = useId();
  const [query, setQuery] = useState("");
  // Vazio = o k da coleção, o mesmo que o agente recebe.
  const [limit, setLimit] = useState("");
  const [state, setState] = useState<SearchState>({ status: "idle" });

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setState({ status: "loading" });
    try {
      const results = await requestJson<SearchResult[]>(
        `/api/collections/${encodeURIComponent(collection)}/search?${new URLSearchParams({ query: q, ...(limit ? { limit } : {}) })}`,
        { fallbackError: "Falha na busca" }
      );
      setState({ status: "done", query: q, results });
    } catch (err) {
      setState({ status: "error", message: errorMessage(err) });
    }
  }

  return (
    <Card className="xl:sticky xl:top-0">
      <CardHeader className="pb-4">
        <CardTitle className="flex items-center gap-2">
          <Search className="size-4 text-muted-foreground" /> Testar busca
        </CardTitle>
        <CardDescription>
          Veja quais trechos o agente receberia para uma pergunta — com o modo, o idioma e o k desta coleção.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <form onSubmit={handleSubmit} className="flex gap-2">
          <label htmlFor={`${id}-query`} className="sr-only">
            Pergunta
          </label>
          <Input
            id={`${id}-query`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Ex.: prazo de reembolso"
          />
          <Select
            aria-label="Quantidade de resultados"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
            wrapperClassName="w-[4.5rem] shrink-0"
          >
            <option value="">k</option>
            {["3", "5", "10"].map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </Select>
          <Button
            type="submit"
            size="icon"
            disabled={!query.trim() || state.status === "loading"}
            aria-label="Buscar"
          >
            {state.status === "loading" ? <Spinner /> : <ArrowRight />}
          </Button>
        </form>

        {state.status === "idle" && (
          <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-[13px] text-muted-foreground">
            Os trechos aparecem aqui, ordenados por relevância.
          </p>
        )}

        {state.status === "error" && <p className="text-[13px] text-destructive">{state.message}</p>}

        {state.status === "done" && state.results.length === 0 && (
          <EmptyState
            className="py-6"
            icon={Search}
            title="Nenhum trecho encontrado"
            description={`Nada relevante para “${state.query}”.`}
          />
        )}

        {state.status === "done" && state.results.length > 0 && (
          <ChunkList chunks={state.results} label="Resultados da busca" />
        )}
      </CardContent>
    </Card>
  );
}
