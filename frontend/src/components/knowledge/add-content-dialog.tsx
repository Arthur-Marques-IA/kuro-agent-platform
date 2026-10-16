"use client";

import { FormEvent, useId, useState } from "react";
import { FileText, FileUp, Globe, Upload, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { errorMessage, requestJson } from "@/lib/http";
import { formatBytes } from "@/lib/format";
import type { SourceMode, SourceRefresh } from "@/lib/types";
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
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Field, Spinner } from "@/components/ui/primitives";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useToast } from "@/components/ui/toast";

type Mode = "text" | "file" | "site";

export function AddContentDialog({
  open,
  onOpenChange,
  collection,
  onAdded,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  collection: string;
  onAdded: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="md">
      {open && <AddContentForm collection={collection} onClose={() => onOpenChange(false)} onAdded={onAdded} />}
    </Dialog>
  );
}

function isValidUrl(value: string) {
  try {
    const url = new URL(value.trim());
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

const SITE_MODE_HINT: Record<SourceMode, string> = {
  crawl: "Segue os links do mesmo site, só abaixo do caminho da URL (ex.: /docs/…), respeitando o robots.txt.",
  sitemap: "Usa o sitemap da URL — ou o anunciado no robots.txt, ou /sitemap.xml.",
  page: "Indexa só esta página.",
};

function AddContentForm({
  collection,
  onClose,
  onAdded,
}: {
  collection: string;
  onClose: () => void;
  onAdded: () => void;
}) {
  const id = useId();
  const toast = useToast();
  const [mode, setMode] = useState<Mode>("text");
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  // Site: o backend coleta, formata e indexa em background, em qualquer coleção
  // (`POST /collections/{nome}/sources`), com o embedder dela.
  const [siteMode, setSiteMode] = useState<SourceMode>("crawl");
  const [maxPages, setMaxPages] = useState("50");
  const [refresh, setRefresh] = useState<SourceRefresh>("weekly");
  const [formatPages, setFormatPages] = useState(true);
  const [includeFiles, setIncludeFiles] = useState(true);
  const [exclude, setExclude] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = mode === "text" ? Boolean(text.trim()) : mode === "file" ? Boolean(file) : isValidUrl(url);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      if (mode === "text") {
        await requestJson(`/api/collections/${encodeURIComponent(collection)}/documents`, {
          method: "POST",
          json: { text, name: name.trim() || undefined },
          fallbackError: "Falha ao enviar o texto",
        });
      } else if (mode === "file" && file) {
        const form = new FormData();
        form.set("file", file);
        if (name.trim()) form.set("name", name.trim());
        await requestJson(`/api/collections/${encodeURIComponent(collection)}/files`, {
          method: "POST",
          body: form,
          fallbackError: "Falha no upload do arquivo",
        });
      } else {
        await requestJson(`/api/collections/${encodeURIComponent(collection)}/sources`, {
          method: "POST",
          json: {
            url: url.trim(),
            mode: siteMode,
            refresh,
            format: formatPages,
            include_files: includeFiles,
            label: name.trim() || undefined,
            max_pages: siteMode === "page" ? undefined : Number(maxPages) || undefined,
            exclude: exclude
              .split(/[\n,]/)
              .map((pattern) => pattern.trim())
              .filter(Boolean),
          },
          fallbackError: "Falha ao cadastrar o site",
        });
      }
      toast({
        title: mode === "site" ? "Site cadastrado" : "Conteúdo enviado",
        description:
          mode === "site"
            ? "A coleta começou — acompanhe as páginas em “Sites”."
            : "A indexação continua em segundo plano — o status aparece na lista.",
        variant: "success",
      });
      onAdded();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col" noValidate>
      <DialogHeader>
        <DialogTitle>Adicionar conteúdo</DialogTitle>
        <DialogDescription>
          Vai para a coleção <span className="font-mono text-foreground">{collection}</span> e é indexado com
          embeddings para busca semântica.
        </DialogDescription>
      </DialogHeader>

      <DialogBody className="flex flex-col gap-5">
        <Tabs value={mode} onValueChange={(v) => setMode(v as Mode)} variant="pill">
          <TabsList className="grid w-full grid-cols-3">
            <TabsTrigger value="text">
              <FileText /> Texto
            </TabsTrigger>
            <TabsTrigger value="file">
              <FileUp /> Arquivo
            </TabsTrigger>
            <TabsTrigger value="site">
              <Globe /> Site
            </TabsTrigger>
          </TabsList>

          <TabsContent value="text" className="mt-5">
            <Field label="Conteúdo" htmlFor={`${id}-text`}>
              <Textarea
                id={`${id}-text`}
                data-autofocus
                rows={9}
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="Cole aqui o texto a ser indexado…"
                className="resize-y"
              />
            </Field>
          </TabsContent>

          <TabsContent value="file" className="mt-5 flex flex-col gap-3">
            <label
              htmlFor={`${id}-file`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                const dropped = e.dataTransfer.files?.[0];
                if (dropped) setFile(dropped);
              }}
              className={cn(
                "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors",
                dragging ? "border-primary bg-primary-soft/50" : "border-input hover:bg-accent/40"
              )}
            >
              <Upload className="size-6 text-muted-foreground" />
              <span className="text-sm font-medium">
                Arraste um arquivo ou <span className="text-primary">escolha no computador</span>
              </span>
              <span className="text-xs text-muted-foreground">
                Formatos lidos pelos readers do Agno: PDF, DOCX, TXT, CSV, Markdown, JSON…
              </span>
              <input
                id={`${id}-file`}
                type="file"
                className="sr-only"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </label>
            {file && (
              <div className="flex items-center gap-3 rounded-lg border border-border px-3 py-2">
                <FileText className="size-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{file.name}</p>
                  <p className="text-xs text-muted-foreground">{formatBytes(file.size)}</p>
                </div>
                <Button variant="ghost" size="icon-xs" onClick={() => setFile(null)} aria-label="Remover arquivo">
                  <X />
                </Button>
              </div>
            )}
          </TabsContent>

          <TabsContent value="site" className="mt-5 flex flex-col gap-4">
            <Field
              label="URL"
              htmlFor={`${id}-url`}
              hint={SITE_MODE_HINT[siteMode]}
              error={url && !isValidUrl(url) ? "Informe uma URL http(s) válida." : null}
            >
              <Input
                id={`${id}-url`}
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://docs.exemplo.com/guia"
              />
            </Field>
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="O que coletar" htmlFor={`${id}-site-mode`}>
                <Select
                  id={`${id}-site-mode`}
                  value={siteMode}
                  onChange={(e) => setSiteMode(e.target.value as SourceMode)}
                >
                  <option value="crawl">Site (seguir links)</option>
                  <option value="sitemap">Sitemap</option>
                  <option value="page">Só esta página</option>
                </Select>
              </Field>
              <Field label="Máx. de páginas" htmlFor={`${id}-max-pages`}>
                <Input
                  id={`${id}-max-pages`}
                  type="number"
                  min={1}
                  max={5000}
                  value={siteMode === "page" ? "1" : maxPages}
                  disabled={siteMode === "page"}
                  onChange={(e) => setMaxPages(e.target.value)}
                />
              </Field>
              <Field label="Atualizar" htmlFor={`${id}-refresh`}>
                <Select
                  id={`${id}-refresh`}
                  value={refresh}
                  onChange={(e) => setRefresh(e.target.value as SourceRefresh)}
                >
                  <option value="never">Só manualmente</option>
                  <option value="daily">Todo dia</option>
                  <option value="weekly">Toda semana</option>
                  <option value="monthly">Todo mês</option>
                </Select>
              </Field>
            </div>
            {siteMode !== "page" && (
              <Field
                label="Ignorar caminhos (opcional)"
                htmlFor={`${id}-exclude`}
                hint="Globs separados por vírgula, ex.: /blog/*"
              >
                <Input
                  id={`${id}-exclude`}
                  value={exclude}
                  onChange={(e) => setExclude(e.target.value)}
                  placeholder="/blog/*, /changelog/*"
                />
              </Field>
            )}
            <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-border px-3 py-2.5 text-sm">
              <input
                type="checkbox"
                className="mt-0.5 size-4 accent-primary"
                checked={formatPages}
                onChange={(e) => setFormatPages(e.target.checked)}
              />
              <span className="flex flex-col gap-0.5">
                <span className="font-medium">Formatar cada página com um agente</span>
                <span className="text-xs text-muted-foreground">
                  Organiza títulos, listas e tabelas sem reescrever o texto. Se o resultado não bater com a página, o
                  texto extraído é usado no lugar.
                </span>
              </span>
            </label>
            <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-border px-3 py-2.5 text-sm">
              <input
                type="checkbox"
                className="mt-0.5 size-4 accent-primary"
                checked={includeFiles}
                onChange={(e) => setIncludeFiles(e.target.checked)}
              />
              <span className="flex flex-col gap-0.5">
                <span className="font-medium">Indexar os arquivos linkados</span>
                <span className="text-xs text-muted-foreground">
                  PDF, DOCX, PPTX e XLSX do mesmo site e caminho, lidos direto (sem o agente), com o número da página.
                  Contam no limite de páginas.
                </span>
              </span>
            </label>
          </TabsContent>
        </Tabs>

        <Field
          label="Nome (opcional)"
          htmlFor={`${id}-name`}
          hint={mode === "site" ? "Como o site aparece na lista." : "Ajuda a identificar o documento na lista."}
        >
          <Input
            id={`${id}-name`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={
              mode === "file" && file ? file.name : mode === "site" ? "ex.: Central de ajuda" : "ex.: política-de-reembolso"
            }
          />
        </Field>
      </DialogBody>

      <DialogFooter className="items-center">
        {error && <p className="min-w-0 text-[13px] text-destructive sm:mr-auto">{error}</p>}
        <Button variant="outline" onClick={onClose} disabled={submitting}>
          Cancelar
        </Button>
        <Button type="submit" disabled={!canSubmit || submitting}>
          {submitting && <Spinner />} Adicionar
        </Button>
      </DialogFooter>
    </form>
  );
}
