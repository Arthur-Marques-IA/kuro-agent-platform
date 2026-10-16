"use client";

import { FormEvent, useEffect, useId, useState } from "react";
import { errorMessage, requestJson } from "@/lib/http";
import type { Collection, EmbedderOption, SearchMode } from "@/lib/types";
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
import { useToast } from "@/components/ui/toast";

const SLUG_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;

/** Sugere `manuais-do-produto` a partir de "Manuais do produto". */
function slugify(label: string) {
  return label
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

export function NewCollectionDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (name: string) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="sm">
      {open && <NewCollectionForm onClose={() => onOpenChange(false)} onCreated={onCreated} />}
    </Dialog>
  );
}

function NewCollectionForm({ onClose, onCreated }: { onClose: () => void; onCreated: (name: string) => void }) {
  const id = useId();
  const toast = useToast();
  const [label, setLabel] = useState("");
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [embedders, setEmbedders] = useState<EmbedderOption[] | null>(null);
  const [embedder, setEmbedder] = useState("google");
  const [searchMode, setSearchMode] = useState<SearchMode>("vector");
  const [language, setLanguage] = useState("portuguese");

  useEffect(() => {
    let vivo = true;
    requestJson<EmbedderOption[]>("/api/collections/embedders", { fallbackError: "" })
      .then((data) => vivo && setEmbedders(data))
      .catch(() => vivo && setEmbedders([]));
    return () => {
      vivo = false;
    };
  }, []);

  const escolhido = embedders?.find((e) => e.provider === embedder);

  const slug = nameTouched ? name : slugify(label);
  const slugError = slug && !SLUG_PATTERN.test(slug) ? "Use minúsculas, números, '-' ou '_'." : null;

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!label.trim() || !slug || slugError) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await requestJson<Collection>("/api/collections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: slug,
          label: label.trim(),
          description: description.trim() || null,
          embedder_provider: embedder,
          search_mode: searchMode,
          language,
        }),
        fallbackError: "Falha ao criar coleção",
      });
      toast({ title: "Coleção criada", description: "Agora adicione documentos e ligue-a a um agente.", variant: "success" });
      onCreated(created.name);
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <DialogHeader>
        <DialogTitle>Nova coleção</DialogTitle>
        <DialogDescription>
          Uma base de conhecimento separada. Um agente consulta uma coleção quando você a escolhe na aba Modelo dele.
        </DialogDescription>
      </DialogHeader>

      <DialogBody className="flex flex-col gap-4">
        <Field label="Nome" htmlFor={`${id}-label`}>
          <Input
            id={`${id}-label`}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Manuais do produto"
            autoFocus
          />
        </Field>

        <Field
          label="Identificador"
          htmlFor={`${id}-name`}
          hint="Usado na tabela de vetores e na API. Não muda depois de criado."
          error={slugError}
        >
          <Input
            id={`${id}-name`}
            value={slug}
            onChange={(e) => {
              setNameTouched(true);
              setName(e.target.value);
            }}
            placeholder="manuais"
            className="font-mono"
          />
        </Field>

        <Field
          label="Quem gera os vetores"
          htmlFor={`${id}-embedder`}
          hint={
            escolhido
              ? `${escolhido.description} Não muda depois: a tabela de vetores é de um embedder só.`
              : "Não muda depois: a tabela de vetores é de um embedder só."
          }
          error={escolhido && !escolhido.configured ? "Este provedor ainda não tem credencial cadastrada." : null}
        >
          <Select id={`${id}-embedder`} value={embedder} onChange={(e) => setEmbedder(e.target.value)}>
            {(embedders ?? []).map((e) => (
              <option key={e.provider} value={e.provider}>
                {e.label} · {e.default_model_id}
                {e.configured ? "" : " (sem credencial)"}
              </option>
            ))}
          </Select>
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Busca"
            htmlFor={`${id}-mode`}
            hint="Híbrida soma o full-text ao vetor: ajuda com códigos, siglas e nomes exatos. Muda depois."
          >
            <Select id={`${id}-mode`} value={searchMode} onChange={(e) => setSearchMode(e.target.value as SearchMode)}>
              <option value="vector">Vetorial</option>
              <option value="hybrid">Híbrida (vetor + texto)</option>
            </Select>
          </Field>
          <Field label="Idioma do texto" htmlFor={`${id}-language`} hint="Dicionário do full-text. Não muda depois.">
            <Select id={`${id}-language`} value={language} onChange={(e) => setLanguage(e.target.value)}>
              <option value="portuguese">Português</option>
              <option value="english">Inglês</option>
              <option value="spanish">Espanhol</option>
              <option value="simple">Sem idioma (simple)</option>
            </Select>
          </Field>
        </div>

        <Field label="Descrição" htmlFor={`${id}-description`} hint="Opcional.">
          <Textarea
            id={`${id}-description`}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            placeholder="O que vive nesta coleção."
          />
        </Field>

        {error && <p className="text-sm text-destructive">{error}</p>}
      </DialogBody>

      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onClose} disabled={submitting}>
          Cancelar
        </Button>
        <Button type="submit" disabled={submitting || !label.trim() || !slug || Boolean(slugError)}>
          {submitting && <Spinner />} Criar coleção
        </Button>
      </DialogFooter>
    </form>
  );
}
