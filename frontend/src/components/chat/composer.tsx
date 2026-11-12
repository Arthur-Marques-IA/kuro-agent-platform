"use client";

import { FormEvent, useId, useRef, useState } from "react";
import { ArrowUp, Braces, Headset, Paperclip, Square, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { readAttachment } from "@/lib/attachments";
import type { Attachment } from "@/lib/types";

export function Composer({
  onSend,
  onAttachError,
  onStop,
  isStreaming,
  disabled,
  placeholder,
  contextCount,
  contextInvalid,
  onOpenContext,
  onAddHumanAgent,
}: {
  /** Retorna `false` se a mensagem não foi aceita (o texto fica no campo). */
  onSend: (text: string, attachments: Attachment[]) => boolean;
  /** Erro ao ler um arquivo — mostrado por quem hospeda o composer. */
  onAttachError?: (message: string) => void;
  onStop: () => void;
  isStreaming: boolean;
  disabled?: boolean;
  placeholder?: string;
  contextCount: number;
  contextInvalid: boolean;
  onOpenContext: () => void;
  /**
   * Modo "simular atendente": o texto vira um evento `human_agent`, que vai em `events`
   * junto da próxima fala do contato. Ausente (ex.: agente procedural), o botão some.
   */
  onAddHumanAgent?: (text: string) => void;
}) {
  const id = useId();
  const [value, setValue] = useState("");
  const [anexos, setAnexos] = useState<{ attachment: Attachment; name: string }[]>([]);
  const [humanMode, setHumanMode] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const asHuman = humanMode && Boolean(onAddHumanAgent);
  // Só anexo já é uma mensagem válida ("o que tem nessa foto?" pode vir depois).
  // O evento do atendente é só texto. Nada é aceito com a resposta saindo: numa conversa nova,
  // o fim da 1ª troca troca de rota e uma fala registrada nesse meio-tempo se perderia.
  const canSend = asHuman
    ? Boolean(value.trim()) && !isStreaming && !disabled
    : (Boolean(value.trim()) || anexos.length > 0) && !isStreaming && !disabled;

  async function anexar(files: FileList | null) {
    if (!files) return;
    for (const file of Array.from(files)) {
      try {
        const attachment = await readAttachment(file);
        setAnexos((prev) => [...prev, { attachment, name: file.name }]);
      } catch (err) {
        onAttachError?.(err instanceof Error ? err.message : String(err));
      }
    }
  }

  function autoResize(el: HTMLTextAreaElement) {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }

  function submit(e?: FormEvent) {
    e?.preventDefault();
    if (!canSend) return;
    if (asHuman) {
      onAddHumanAgent?.(value);
      setValue("");
      if (textareaRef.current) textareaRef.current.style.height = "auto";
      return;
    }
    if (onSend(value, anexos.map((a) => a.attachment))) {
      setValue("");
      setAnexos([]);
      if (textareaRef.current) textareaRef.current.style.height = "auto";
    }
  }

  return (
    <form
      onSubmit={submit}
      className={cn(
        "rounded-2xl border border-input bg-card shadow-sm transition-[border-color,box-shadow]",
        "focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/15",
        asHuman && "border-violet-500/50 bg-violet-500/5 focus-within:border-violet-500/70 focus-within:ring-violet-500/15",
        disabled && "opacity-70"
      )}
    >
      <label htmlFor={id} className="sr-only">
        {asHuman ? "Fala do atendente humano" : "Mensagem"}
      </label>
      {asHuman && (
        <p className="px-4 pt-2.5 text-[11px] font-medium text-violet-700 dark:text-violet-300">
          Simulando atendente humano: o texto vai em <code className="font-mono">events</code> junto da próxima
          mensagem do contato.
        </p>
      )}
      <textarea
        ref={textareaRef}
        id={id}
        value={value}
        rows={1}
        autoFocus
        disabled={disabled}
        placeholder={asHuman ? "O que o atendente humano disse ao contato…" : placeholder}
        onChange={(e) => {
          setValue(e.target.value);
          autoResize(e.target);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
        className="scrollbar-thin block max-h-60 min-h-[52px] w-full resize-none bg-transparent px-4 pb-1 pt-3.5 text-sm leading-relaxed outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed"
      />
      {anexos.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-3 pb-1">
          {anexos.map((a, i) => (
            <Badge key={i} variant="secondary" className="gap-1">
              {a.name}
              <button
                type="button"
                aria-label={`Remover ${a.name}`}
                onClick={() => setAnexos((prev) => prev.filter((_, idx) => idx !== i))}
              >
                <X className="size-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}
      <div className="flex items-center gap-2 px-2.5 pb-2.5">
        <label
          title="Anexar imagem, áudio, vídeo ou arquivo — o modelo do agente precisa suportar o tipo"
          className="inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-lg border border-border px-2 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <Paperclip className="size-3.5" />
          <span className="sr-only">Anexar arquivo</span>
          <input type="file" multiple className="hidden" disabled={disabled} onChange={(e) => void anexar(e.target.files)} />
        </label>
        <button
          type="button"
          onClick={onOpenContext}
          title="Contexto (dependencies e blocos de contexto) enviado com cada mensagem"
          className={cn(
            "inline-flex h-7 items-center gap-1.5 rounded-lg border px-2 text-xs transition-colors",
            contextInvalid
              ? "border-destructive/40 bg-destructive/5 text-destructive"
              : contextCount > 0
                ? "border-primary/30 bg-primary-soft text-primary"
                : "border-border text-muted-foreground hover:bg-accent hover:text-foreground"
          )}
        >
          <Braces className="size-3.5" />
          {contextInvalid ? "Contexto inválido" : contextCount > 0 ? `Contexto · ${contextCount}` : "Contexto"}
        </button>
        {onAddHumanAgent && (
          <button
            type="button"
            onClick={() => {
              setHumanMode((on) => !on);
              textareaRef.current?.focus();
            }}
            aria-pressed={asHuman}
            title="Simular atendente: registrar uma fala do atendente humano antes da próxima mensagem do contato"
            className={cn(
              "inline-flex h-7 items-center gap-1.5 rounded-lg border px-2 text-xs transition-colors",
              asHuman
                ? "border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-300"
                : "border-border text-muted-foreground hover:bg-accent hover:text-foreground"
            )}
          >
            <Headset className="size-3.5" />
            <span className="hidden sm:inline">Atendente</span>
          </button>
        )}
        <div className="ml-auto flex items-center gap-3">
          <span className="hidden text-[11px] text-muted-foreground md:inline">
            Enter envia · Shift+Enter quebra linha
          </span>
          {isStreaming && !asHuman ? (
            <Button size="icon-sm" variant="secondary" onClick={onStop} aria-label="Parar resposta" title="Parar resposta">
              <Square className="fill-current" />
            </Button>
          ) : (
            <Button
              type="submit"
              size="icon-sm"
              disabled={!canSend}
              aria-label={asHuman ? "Registrar fala do atendente" : "Enviar mensagem"}
              title={asHuman ? "Registrar fala do atendente" : "Enviar"}
            >
              <ArrowUp />
            </Button>
          )}
        </div>
      </div>
    </form>
  );
}
