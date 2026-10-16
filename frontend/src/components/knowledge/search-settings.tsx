"use client";

import { FormEvent, useEffect, useId, useState } from "react";
import { History, Pencil, SlidersHorizontal } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import type { Collection, CollectionVersion, SearchMode } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Field, RelativeTime, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/toast";
import { SCORE_KIND_LABEL } from "@/components/knowledge/chunk-list";

/** Como a coleção busca (modo, idioma, k, peso do vetor) e o histórico dessa configuração.
 *  Cada mudança vira uma versão nova, e cada run grava a versão com que buscou. */
export function SearchSettings({ collection, onSaved }: { collection: Collection; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [showVersions, setShowVersions] = useState(false);
  const { search } = collection;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <SlidersHorizontal className="size-4 text-muted-foreground" /> Como a busca funciona
          {collection.version != null && (
            <Badge variant="outline" className="ml-auto font-mono" title={`config_hash ${collection.config_hash ?? "—"}`}>
              v{collection.version}
            </Badge>
          )}
        </CardTitle>
        <CardDescription>Mudar a busca gera uma versão nova; cada run grava a versão que usou.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {editing ? (
          <SearchForm
            key={collection.config_hash ?? collection.name}
            collection={collection}
            onDone={(saved) => {
              setEditing(false);
              if (saved) onSaved();
            }}
          />
        ) : (
          <>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[13px]">
              <dt className="text-muted-foreground">Modo</dt>
              <dd>
                {search.search_mode === "hybrid"
                  ? `Híbrida — vetor ${search.vector_weight.toFixed(2)} + texto ${(1 - search.vector_weight).toFixed(2)}`
                  : "Vetorial"}
              </dd>
              <dt className="text-muted-foreground">Trechos (k)</dt>
              <dd className="tabular-nums">{search.max_results}</dd>
              <dt className="text-muted-foreground">Idioma</dt>
              <dd className="font-mono text-[12px]">{search.language}</dd>
              <dt className="text-muted-foreground">Score</dt>
              <dd title={SCORE_KIND_LABEL[search.score_kind].long}>{SCORE_KIND_LABEL[search.score_kind].short}</dd>
            </dl>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
                <Pencil /> Editar
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setShowVersions((v) => !v)}>
                <History /> {showVersions ? "Esconder versões" : "Versões"}
              </Button>
            </div>
          </>
        )}
        {showVersions && !editing && <VersionList collection={collection.name} current={collection.version} />}
      </CardContent>
    </Card>
  );
}

function SearchForm({ collection, onDone }: { collection: Collection; onDone: (saved: boolean) => void }) {
  const id = useId();
  const toast = useToast();
  const { search } = collection;
  const [mode, setMode] = useState<SearchMode>(search.search_mode);
  const [k, setK] = useState(String(search.max_results));
  const [weight, setWeight] = useState(String(search.vector_weight));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const kNumber = Number(k);
  const weightNumber = Number(weight);
  const kError = !Number.isInteger(kNumber) || kNumber < 1 || kNumber > 50 ? "Entre 1 e 50." : null;
  const weightError = Number.isNaN(weightNumber) || weightNumber < 0 || weightNumber > 1 ? "Entre 0 e 1." : null;

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (kError || (mode === "hybrid" && weightError)) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await requestJson<Collection>(`/api/collections/${encodeURIComponent(collection.name)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          search_mode: mode,
          max_results: kNumber,
          ...(mode === "hybrid" ? { vector_weight: weightNumber } : {}),
        }),
        fallbackError: "Falha ao salvar a busca",
      });
      toast({
        title: saved.version === collection.version ? "Nada mudou" : `Busca salva — versão ${saved.version}`,
        variant: "success",
      });
      onDone(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <Field label="Modo" htmlFor={`${id}-mode`}>
        <Select id={`${id}-mode`} value={mode} onChange={(e) => setMode(e.target.value as SearchMode)}>
          <option value="vector">Vetorial</option>
          <option value="hybrid">Híbrida (vetor + texto)</option>
        </Select>
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Trechos (k)" htmlFor={`${id}-k`} error={kError}>
          <Input id={`${id}-k`} type="number" min={1} max={50} value={k} onChange={(e) => setK(e.target.value)} />
        </Field>
        {mode === "hybrid" && (
          <Field label="Peso do vetor" htmlFor={`${id}-weight`} error={weightError} hint="O resto é do texto.">
            <Input
              id={`${id}-weight`}
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={weight}
              onChange={(e) => setWeight(e.target.value)}
            />
          </Field>
        )}
      </div>
      <p className="text-[12px] text-muted-foreground">
        Idioma <span className="font-mono">{search.language}</span> e embedder são fixos: para trocar, crie outra coleção.
      </p>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={saving || Boolean(kError) || (mode === "hybrid" && Boolean(weightError))}>
          {saving && <Spinner />} Salvar
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={() => onDone(false)} disabled={saving}>
          Cancelar
        </Button>
      </div>
    </form>
  );
}

function describe(config: Record<string, unknown>): string {
  const mode = config.search_mode === "hybrid" ? `híbrida (vetor ${config.vector_weight})` : "vetorial";
  return `${mode} · k=${config.max_results} · ${config.language ?? "english"}`;
}

function VersionList({ collection, current }: { collection: string; current?: number | null }) {
  const [state, setState] = useState<{ key: string; versions: CollectionVersion[] | null; error: string | null }>();
  const key = `${collection}|${current}`;

  // Carrega ao abrir e de novo quando a versão atual muda (depois de salvar).
  useEffect(() => {
    let vivo = true;
    requestJson<CollectionVersion[]>(`/api/collections/${encodeURIComponent(collection)}/versions`, {
      fallbackError: "Falha ao listar versões",
    })
      .then((versions) => vivo && setState({ key, versions, error: null }))
      .catch((err) => vivo && setState({ key, versions: null, error: errorMessage(err) }));
    return () => {
      vivo = false;
    };
  }, [collection, key]);

  if (state?.key === key && state.error) return <p className="text-[13px] text-destructive">{state.error}</p>;
  if (state?.key !== key || !state.versions) return <Spinner />;
  return (
    <ol className="flex flex-col divide-y divide-border rounded-lg border border-border text-[12px]" aria-label="Versões da busca">
      {state.versions.map((v) => (
        <li key={v.version} className="flex items-center gap-2 px-3 py-2">
          <span className="font-mono font-medium">v{v.version}</span>
          <span className="min-w-0 flex-1 truncate text-muted-foreground" title={v.config_hash}>
            {describe(v.config)}
          </span>
          <RelativeTime date={v.created_at} className="shrink-0 text-muted-foreground" />
        </li>
      ))}
    </ol>
  );
}
