"use client";

import { FormEvent, useEffect, useId, useState } from "react";
import Link from "next/link";
import { CircleCheck, CircleX, FileUp, RotateCcw, Type } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import type { ChunkFeedback, DocumentRevision, ReplaceDocumentResult, RetestCase } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/toast";

export interface DocumentTarget {
  contentId: string;
  name?: string | null;
}

/**
 * Corrige um documento sem trocar o id: o texto novo vira a revisão seguinte, e as
 * marcações abertas dele (todas marcadas por padrão) ficam resolvidas nessa revisão.
 * Depois de salvar, "Testar de novo" busca as perguntas marcadas e mostra se o
 * documento voltou — sem chamar o modelo.
 */
export function DocumentFixDialog({
  collection,
  target,
  onOpenChange,
  onSaved,
}: {
  collection: string;
  target: DocumentTarget | null;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  return (
    <Dialog open={target !== null} onOpenChange={onOpenChange} size="lg">
      {target && (
        <DocumentFixForm
          key={target.contentId}
          collection={collection}
          target={target}
          onClose={() => onOpenChange(false)}
          onSaved={onSaved}
        />
      )}
    </Dialog>
  );
}

function docPath(collection: string, contentId: string) {
  return `/api/collections/${encodeURIComponent(collection)}/documents/${encodeURIComponent(contentId)}`;
}

function DocumentFixForm({
  collection,
  target,
  onClose,
  onSaved,
}: {
  collection: string;
  target: DocumentTarget;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const textId = useId();
  const noteId = useId();
  const [loading, setLoading] = useState(true);
  const [current, setCurrent] = useState<DocumentRevision | null>(null);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [marks, setMarks] = useState<ChunkFeedback[]>([]);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [mode, setMode] = useState<"text" | "file">("text");
  const [file, setFile] = useState<File | null>(null);
  const fileId = useId();
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<ReplaceDocumentResult | null>(null);
  const [retest, setRetest] = useState<RetestCase[] | null>(null);
  const [retesting, setRetesting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const base = docPath(collection, target.contentId);
    (async () => {
      try {
        const [revisions, open] = await Promise.all([
          requestJson<DocumentRevision[]>(`${base}/revisions`, { fallbackError: "Falha ao carregar as revisões" }),
          requestJson<ChunkFeedback[]>(
            `/api/observability/chunk-feedback?${new URLSearchParams({ content_id: target.contentId, status: "open" })}`,
            { fallbackError: "Falha ao carregar as marcações" }
          ),
        ]);
        const latest = revisions[0]
          ? await requestJson<DocumentRevision>(`${base}/revisions/${revisions[0].revision}`, {
              fallbackError: "Falha ao carregar o texto",
            })
          : null;
        if (cancelled) return;
        setCurrent(latest);
        setText(latest?.text ?? "");
        setMarks(open);
        setSelected(new Set(open.map((m) => m.id)));
      } catch (err) {
        if (!cancelled) toast({ title: "Não foi possível abrir o documento", description: errorMessage(err), variant: "error" });
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [collection, target.contentId, toast]);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function dismiss(mark: ChunkFeedback) {
    try {
      await requestJson(`/api/observability/chunk-feedback/${encodeURIComponent(mark.id)}/status`, {
        method: "POST",
        json: { status: "dismissed", note: "não procedia" },
        fallbackError: "Falha ao dispensar a marcação",
      });
      setMarks((prev) => prev.filter((m) => m.id !== mark.id));
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(mark.id);
        return next;
      });
      onSaved();
    } catch (err) {
      toast({ title: "Não foi possível dispensar", description: errorMessage(err), variant: "error" });
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (mode === "text" ? !text.trim() : !file) return;
    setSaving(true);
    try {
      let out: ReplaceDocumentResult;
      if (mode === "file" && file) {
        const form = new FormData();
        form.set("file", file);
        if (note.trim()) form.set("note", note.trim());
        form.set("resolves", [...selected].join(","));
        out = await requestJson<ReplaceDocumentResult>(`${docPath(collection, target.contentId)}/file`, {
          method: "PUT",
          body: form,
          fallbackError: "Falha ao enviar o arquivo corrigido",
        });
      } else {
        out = await requestJson<ReplaceDocumentResult>(docPath(collection, target.contentId), {
          method: "PUT",
          json: { text, note: note.trim() || undefined, resolves: [...selected] },
          fallbackError: "Falha ao salvar a correção",
        });
      }
      setResult(out);
      onSaved();
      toast({
        title: out.changed ? `Revisão ${out.revision} indexada` : "Nada mudou",
        description: out.changed ? `${out.resolved} marcação(ões) resolvida(s).` : "O texto é o mesmo da revisão atual.",
        variant: out.changed ? "success" : "default",
      });
    } catch (err) {
      toast({ title: "Não foi possível salvar", description: errorMessage(err), variant: "error" });
    } finally {
      setSaving(false);
    }
  }

  async function runRetest() {
    setRetesting(true);
    try {
      setRetest(
        await requestJson<RetestCase[]>(`${docPath(collection, target.contentId)}/retest?status=all`, {
          method: "POST",
          fallbackError: "Falha ao testar a busca",
        })
      );
    } catch (err) {
      toast({ title: "Não foi possível testar", description: errorMessage(err), variant: "error" });
    } finally {
      setRetesting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex min-h-0 flex-col">
      <DialogHeader>
        <DialogTitle>Corrigir documento</DialogTitle>
        <DialogDescription>
          {target.name ?? target.contentId} — o id continua o mesmo; o texto novo vira a revisão{" "}
          {current ? current.revision + 1 : "seguinte"}.
        </DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-4">
        {loading ? (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        ) : result ? (
          <RetestResult result={result} retest={retest} retesting={retesting} onRetest={runRetest} />
        ) : (
          <>
            <div role="radiogroup" aria-label="Como corrigir" className="flex gap-2">
              {(
                [
                  { value: "text", label: "Editar o texto", icon: Type },
                  { value: "file", label: "Enviar arquivo corrigido", icon: FileUp },
                ] as const
              ).map((option) => (
                <Button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={mode === option.value}
                  variant={mode === option.value ? "secondary" : "ghost"}
                  size="sm"
                  onClick={() => setMode(option.value)}
                >
                  <option.icon />
                  {option.label}
                </Button>
              ))}
            </div>
            {mode === "text" ? (
              <Field
                label="Texto corrigido"
                htmlFor={textId}
                hint={
                  current?.text == null
                    ? "Não há texto guardado nem trechos indexados deste documento: cole o texto inteiro corrigido."
                    : current.text_source === "chunks"
                      ? `Texto extraído do documento (remontado dos trechos da revisão ${current.revision}). Salvar transforma o documento em texto; para manter o arquivo, envie o arquivo corrigido.`
                      : `Revisão atual: ${current.revision}. Edite o texto inteiro — ele substitui o anterior.`
                }
              >
                <Textarea id={textId} value={text} onChange={(e) => setText(e.target.value)} rows={12} className="font-mono text-xs" />
              </Field>
            ) : (
              <Field
                label="Arquivo corrigido"
                htmlFor={fileId}
                hint="PDF, DOCX, PPTX, XLSX, CSV, TXT ou MD. Substitui o documento inteiro, no mesmo id."
              >
                <input
                  id={fileId}
                  type="file"
                  accept=".pdf,.docx,.pptx,.xlsx,.csv,.txt,.md,.json"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                  className="text-sm"
                />
              </Field>
            )}
            <Field label="O que mudou" htmlFor={noteId} hint="Fica no histórico da revisão e nas marcações resolvidas.">
              <Input id={noteId} value={note} onChange={(e) => setNote(e.target.value)} placeholder="ex.: o link de presença saiu" />
            </Field>
            <MarksPicker marks={marks} selected={selected} onToggle={toggle} onDismiss={dismiss} />
          </>
        )}
      </DialogBody>
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onClose}>
          {result ? "Fechar" : "Cancelar"}
        </Button>
        {!result && (
          <Button type="submit" disabled={loading || saving || (mode === "text" ? !text.trim() : !file)}>
            {saving && <Spinner />}
            Salvar revisão
          </Button>
        )}
      </DialogFooter>
    </form>
  );
}

function MarksPicker({
  marks,
  selected,
  onToggle,
  onDismiss,
}: {
  marks: ChunkFeedback[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onDismiss: (mark: ChunkFeedback) => void;
}) {
  if (marks.length === 0) {
    return <p className="text-xs text-muted-foreground">Nenhuma marcação aberta neste documento.</p>;
  }
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-sm font-medium">Marcações que esta revisão resolve</legend>
      {marks.map((mark) => (
        <div key={mark.id} className="flex items-start gap-2 rounded-md border border-border px-3 py-2 text-[13px]">
          <input
            type="checkbox"
            className="mt-1"
            checked={selected.has(mark.id)}
            onChange={() => onToggle(mark.id)}
            aria-label={`Resolver a marcação da pergunta ${mark.query}`}
          />
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="truncate">“{mark.query}”</span>
            <span className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Badge variant={mark.label === "wrong" ? "warning" : "secondary"}>{mark.label}</Badge>
              {mark.document_revision != null && <span>vista na revisão {mark.document_revision}</span>}
              {mark.comment && <span className="truncate">“{mark.comment}”</span>}
              <Link href={`/runs/${encodeURIComponent(mark.run_id)}`} className="font-mono text-primary hover:underline">
                {mark.run_id.slice(0, 12)}
              </Link>
            </span>
          </div>
          <Button type="button" variant="ghost" size="sm" onClick={() => onDismiss(mark)}>
            Não procede
          </Button>
        </div>
      ))}
    </fieldset>
  );
}

function RetestResult({
  result,
  retest,
  retesting,
  onRetest,
}: {
  result: ReplaceDocumentResult;
  retest: RetestCase[] | null;
  retesting: boolean;
  onRetest: () => void;
}) {
  return (
    <div className="flex flex-col gap-3 text-[13px]">
      <p>
        {result.changed
          ? `Revisão ${result.revision} indexada. ${result.resolved} marcação(ões) resolvida(s).`
          : `O texto é o mesmo da revisão ${result.revision}: nada foi reindexado.`}
      </p>
      <div className="flex items-center gap-2">
        <Button type="button" variant="outline" size="sm" onClick={onRetest} disabled={retesting}>
          {retesting ? <Spinner /> : <RotateCcw />}
          Testar de novo
        </Button>
        <span className="text-xs text-muted-foreground">Busca as perguntas marcadas, sem chamar o modelo.</span>
      </div>
      {retest && retest.length === 0 && <p className="text-xs text-muted-foreground">Nenhuma pergunta marcada para testar.</p>}
      {retest?.map((c) => (
        <div key={c.query} className="flex items-start gap-2">
          {c.found ? (
            <CircleCheck className="mt-0.5 size-4 shrink-0 text-success" aria-label="voltou" />
          ) : (
            <CircleX className="mt-0.5 size-4 shrink-0 text-destructive" aria-label="não voltou" />
          )}
          <div className="min-w-0">
            <p className="truncate">“{c.query}”</p>
            <p className="text-xs text-muted-foreground">
              {c.found
                ? c.hits.map((h) => `#${h.rank} (revisão ${h.document_revision ?? "?"})`).join(", ")
                : "o documento não voltou nos trechos desta busca"}
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}
