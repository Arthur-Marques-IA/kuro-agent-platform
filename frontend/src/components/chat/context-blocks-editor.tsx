"use client";

import { useId } from "react";
import { Plus, Trash2 } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  MAX_CONTEXT_BLOCKS,
  emptyBlock,
  type BlocksValidation,
  type ContextBlockDraft,
} from "@/lib/chat-request";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

const EXAMPLE: ContextBlockDraft[] = [
  { key: "dados_contato", title: "Dados do contato", content: "Nome: Maria\nCurso: Pedagogia", volatile: false },
  { key: "contexto_temporal", title: "Contexto temporal", content: "Janela de 24h aberta", volatile: true },
];

/**
 * Os blocos de `context` do `/chat`: texto para o modelo ler, cada um com `key` estável,
 * `title` (o rótulo no prompt), `content` e a marca `volatile`. Não confundir com
 * `dependencies`, que são valores tipados para as tools.
 */
export function ContextBlocksEditor({
  blocks,
  validation,
  onChange,
}: {
  blocks: ContextBlockDraft[];
  validation: BlocksValidation;
  onChange: (blocks: ContextBlockDraft[]) => void;
}) {
  const id = useId();

  function update(index: number, patch: Partial<ContextBlockDraft>) {
    onChange(blocks.map((block, i) => (i === index ? { ...block, ...patch } : block)));
  }

  const count = validation.blocks.length;

  return (
    <div className="flex flex-col gap-3" id="chat-context-blocks">
      {blocks.length === 0 && (
        <p className="rounded-lg border border-dashed border-border px-3 py-3 text-xs text-muted-foreground">
          Nenhum bloco. Cada bloco vira uma seção do prompt, sob o seu título, como uma integração mandaria em{" "}
          <code className="font-mono">context</code>.
        </p>
      )}
      {blocks.map((block, index) => {
        const errors = validation.errors[index];
        const prefix = `${id}-${index}`;
        return (
          // `relative`: os rótulos `sr-only` (absolutos) ficam presos ao bloco, não esticam a página.
          <fieldset key={index} className="relative flex flex-col gap-2 rounded-lg border border-border p-2.5">
            <legend className="sr-only">Bloco {index + 1}</legend>
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] font-medium text-muted-foreground" aria-hidden>
                Bloco {index + 1}
              </span>
              <Button
                variant="ghost"
                size="icon-sm"
                className="size-6"
                aria-label={`Remover bloco ${index + 1}`}
                onClick={() => onChange(blocks.filter((_, i) => i !== index))}
              >
                <Trash2 className="size-3.5" />
              </Button>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <label className="flex min-w-0 flex-col gap-1 text-[11px] text-muted-foreground" htmlFor={`${prefix}-key`}>
                key
                <Input
                  id={`${prefix}-key`}
                  value={block.key}
                  onChange={(e) => update(index, { key: e.target.value })}
                  placeholder="dados_contato"
                  spellCheck={false}
                  aria-invalid={Boolean(errors?.key) || undefined}
                  aria-describedby={errors?.key ? `${prefix}-key-error` : undefined}
                  className="h-8 font-mono text-xs"
                />
              </label>
              <label className="flex min-w-0 flex-col gap-1 text-[11px] text-muted-foreground" htmlFor={`${prefix}-title`}>
                Título
                <Input
                  id={`${prefix}-title`}
                  value={block.title}
                  onChange={(e) => update(index, { title: e.target.value })}
                  placeholder="Dados do contato"
                  aria-invalid={Boolean(errors?.title) || undefined}
                  aria-describedby={errors?.title ? `${prefix}-title-error` : undefined}
                  className="h-8 text-xs"
                />
              </label>
            </div>
            {(errors?.key || errors?.title) && (
              <div className="flex flex-col text-xs text-destructive">
                {errors.key && <span id={`${prefix}-key-error`}>key: {errors.key}</span>}
                {errors.title && <span id={`${prefix}-title-error`}>Título: {errors.title}</span>}
              </div>
            )}
            <label className="sr-only" htmlFor={`${prefix}-content`}>
              Conteúdo do bloco {index + 1}
            </label>
            <Textarea
              id={`${prefix}-content`}
              value={block.content}
              onChange={(e) => update(index, { content: e.target.value })}
              placeholder="O texto que o modelo lê"
              rows={3}
              className="text-xs"
            />
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={block.volatile}
                onChange={(e) => update(index, { volatile: e.target.checked })}
                className="accent-primary"
              />
              <span>
                <code className="font-mono text-foreground">volatile</code>: muda todo turno e não conta como
                &quot;contexto mudou&quot;
              </span>
            </label>
          </fieldset>
        );
      })}

      <div className="flex items-start justify-between gap-2">
        <p
          className={cn(
            "min-w-0 text-xs",
            validation.error ? "text-destructive" : count > 0 ? "text-success" : "text-muted-foreground"
          )}
        >
          {validation.error ??
            (count > 0
              ? `${count} ${count === 1 ? "bloco será enviado" : "blocos serão enviados"} em context`
              : "Vazio — nada é enviado.")}
        </p>
        <div className="flex shrink-0 flex-wrap justify-end gap-1">
          {blocks.length === 0 && (
            <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => onChange(EXAMPLE)}>
              Exemplo
            </Button>
          )}
          {blocks.length > 0 && (
            <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => onChange([])}>
              Limpar
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            className="h-6 px-2 text-xs"
            disabled={blocks.length >= MAX_CONTEXT_BLOCKS}
            onClick={() => onChange([...blocks, emptyBlock()])}
          >
            <Plus className="size-3" /> Bloco
          </Button>
        </div>
      </div>
    </div>
  );
}
