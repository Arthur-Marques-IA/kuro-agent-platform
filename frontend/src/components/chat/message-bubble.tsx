import Link from "next/link";
import { CircleAlert, FileText, Film, Globe, Image as ImageIcon, Music, RotateCcw, Waypoints } from "lucide-react";
import { formatDuration, formatNumber } from "@/lib/format";
import type { AttachmentInfo, ChatMessage, ChatSource } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { AgentAvatar } from "@/components/agents/agent-avatar";
import { MessageContent } from "@/components/chat/message-content";
import { MessageFeedback } from "@/components/chat/message-feedback";

function TypingIndicator() {
  return (
    <span className="inline-flex h-6 items-center gap-1 text-muted-foreground" aria-label="Gerando resposta">
      <span className="size-1.5 animate-bounce rounded-full bg-current [animation-delay:-0.3s]" />
      <span className="size-1.5 animate-bounce rounded-full bg-current [animation-delay:-0.15s]" />
      <span className="size-1.5 animate-bounce rounded-full bg-current" />
    </span>
  );
}

function UsageSummary({ usage }: { usage: NonNullable<ChatMessage["usage"]> }) {
  const breakdown = [
    usage.input_tokens != null && `${formatNumber(usage.input_tokens)} entrada`,
    usage.output_tokens != null && `${formatNumber(usage.output_tokens)} saída`,
    usage.reasoning_tokens ? `${formatNumber(usage.reasoning_tokens)} raciocínio` : null,
  ].filter(Boolean);

  return (
    <span
      className="text-xs tabular-nums text-muted-foreground"
      title={`Total ao final da resposta${breakdown.length ? `: ${breakdown.join(" · ")}` : ""}`}
    >
      {usage.total_tokens != null ? `${formatNumber(usage.total_tokens)} tokens` : "tokens: ?"}
      {typeof usage.duration === "number" && ` · ${formatDuration(usage.duration)}`}
    </span>
  );
}

const ATTACHMENT_ICONS = { image: ImageIcon, audio: Music, video: Film, file: FileText } as const;

function formatBytes(bytes?: number | null): string | null {
  if (!bytes) return null;
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1).replace(".", ",")} MB`;
}

/** Os anexos que foram junto com a mensagem — nome, tipo e tamanho. */
export function AttachmentChips({ attachments }: { attachments: AttachmentInfo[] }) {
  const labels: Record<string, string> = { image: "imagem", audio: "áudio", video: "vídeo", file: "arquivo" };
  return (
    <div className="flex max-w-[85%] flex-wrap justify-end gap-1.5">
      {attachments.map((attachment, index) => {
        const kind = (attachment.kind ?? "file") as keyof typeof ATTACHMENT_ICONS;
        const Icon = ATTACHMENT_ICONS[kind] ?? FileText;
        const size = formatBytes(attachment.size_bytes);
        return (
          <span
            key={`${attachment.filename ?? kind}-${index}`}
            title={[attachment.filename, attachment.mime_type, size].filter(Boolean).join(" · ")}
            className="inline-flex max-w-64 items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-xs"
          >
            <Icon className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="truncate">{attachment.filename || labels[kind] || "anexo"}</span>
            {size && <span className="shrink-0 text-muted-foreground">{size}</span>}
          </span>
        );
      })}
    </div>
  );
}

/** As fontes da resposta: um chip por documento ou página consultada, do mais relevante
 *  para o menos. Página de site abre a URL; o trace mostra o texto exato de cada trecho. */
function SourceChips({ sources, runId }: { sources: ChatSource[]; runId?: string }) {
  const chip =
    "inline-flex max-w-64 items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 py-1 text-xs";
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5" aria-label="Fontes consultadas">
      <span className="text-xs text-muted-foreground">Fontes:</span>
      {sources.map((source, index) => {
        const label = source.title || source.document || source.content_id || "documento";
        const Icon = source.source_url ? Globe : FileText;
        const tooltip = [
          label,
          source.source_url,
          source.collection && `base ${source.collection}`,
          source.score != null && `score ${source.score.toFixed(2)}`,
          `${source.chunk_ids.length} ${source.chunk_ids.length === 1 ? "trecho" : "trechos"}`,
        ]
          .filter(Boolean)
          .join(" · ");
        const body = (
          <>
            <span className="shrink-0 tabular-nums text-muted-foreground">{index + 1}</span>
            <Icon className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="truncate">{label}</span>
          </>
        );
        const key = source.content_id ?? source.source_url ?? `${label}-${index}`;
        return source.source_url ? (
          <a
            key={key}
            href={source.source_url}
            target="_blank"
            rel="noopener noreferrer"
            title={tooltip}
            className={`${chip} transition-colors hover:bg-muted`}
          >
            {body}
          </a>
        ) : (
          <span key={key} title={tooltip} className={chip}>
            {body}
          </span>
        );
      })}
      {runId && (
        <Link
          href={`/runs/${encodeURIComponent(runId)}`}
          className="inline-flex items-center gap-1 text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
          title="Ver no trace o texto exato de cada trecho"
        >
          <Waypoints className="size-3.5" /> trechos no trace
        </Link>
      )}
    </div>
  );
}

export function MessageBubble({
  message,
  agentType,
  agentName,
  sessionId,
  canTeach = true,
  onRetry,
}: {
  message: ChatMessage;
  agentType: string;
  agentName?: string;
  /** Necessário para ensinar o agente a partir desta conversa. */
  sessionId?: string | null;
  /** `false` em agente analista (a nota de feedback não se aplica). */
  canTeach?: boolean;
  onRetry?: () => void;
}) {
  if (message.role === "user") {
    return (
      <div className="group flex flex-col items-end gap-1">
        {message.attachments && message.attachments.length > 0 && <AttachmentChips attachments={message.attachments} />}
        {message.content && (
          <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-muted px-4 py-2.5 text-sm leading-relaxed">
            {message.content}
          </div>
        )}
        <div className="flex h-6 items-center opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100">
          <CopyButton value={message.content} label="Copiar mensagem" />
        </div>
      </div>
    );
  }

  const showFooter =
    !message.pending && (Boolean(message.content) || Boolean(message.usage) || Boolean(message.runId));

  return (
    <div className="group flex gap-3">
      <AgentAvatar agentType={agentType} name={agentName} size="sm" className="mt-0.5" />
      <div className="min-w-0 flex-1">
        <p className="mb-1 text-[13px] font-medium">{agentName ?? agentType}</p>

        {message.pending && !message.content ? <TypingIndicator /> : message.content ? <MessageContent content={message.content} /> : null}

        {!message.pending && message.sources && message.sources.length > 0 && (
          <SourceChips sources={message.sources} runId={message.runId} />
        )}

        {message.stopped && <p className="mt-1.5 text-xs text-muted-foreground">Resposta interrompida.</p>}

        {message.error && (
          <div className="mt-2 flex flex-wrap items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-[13px]">
            <CircleAlert className="mt-0.5 size-4 shrink-0 text-destructive" />
            <div className="min-w-0 flex-1">
              <p className="font-medium text-destructive">Não foi possível gerar a resposta</p>
              <p className="break-words text-muted-foreground">{message.error}</p>
            </div>
            {onRetry && (
              <Button size="sm" variant="outline" onClick={onRetry}>
                <RotateCcw /> Tentar novamente
              </Button>
            )}
          </div>
        )}

        {showFooter && (
          <div className="mt-1.5 flex min-h-6 flex-wrap items-center gap-1.5">
            {message.usage && <UsageSummary usage={message.usage} />}
            {/* O feedback fica sempre visível: é por ele que se ensina o agente, e
                escondido no hover ninguém achava. Copiar aparece só no hover. */}
            {message.runId && (
              <MessageFeedback
                runId={message.runId}
                agentType={agentType}
                sessionId={sessionId ?? null}
                canTeach={canTeach}
              />
            )}
            <div className="flex items-center opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100">
              {message.content && <CopyButton value={message.content} label="Copiar resposta" />}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
