"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { parseSseStream } from "@/lib/sse";
import { apiUrl, readErrorMessage } from "@/lib/http";
import { createId } from "@/lib/id";
import type { Attachment, ChatMessage, ChatSource, ProcedureState, UsageMetrics } from "@/lib/types";

interface SendOptions {
  text: string;
  sessionId: string;
  dependencies?: Record<string, unknown> | null;
  /** Imagem, áudio, vídeo ou arquivo — o modelo do agente precisa suportar o tipo. */
  attachments?: Attachment[];
  /** Execução de teste: as tools recebem `X-Kuro-Dry-Run: true` e decidem o que simular. */
  dryRun?: boolean;
}

/**
 * Estado de uma conversa + streaming via `/api/chat/stream`. O `sessionId`
 * vai por chamada (não no hook) porque uma conversa nova só ganha id no
 * primeiro envio. `send` resolve `true` quando o backend chegou a executar
 * (resposta completa ou parcial) — ou seja, a sessão existe no AgentOS.
 */
export function useChat({
  agentType,
  userId,
  initialMessages,
}: {
  agentType: string;
  userId: string;
  initialMessages?: ChatMessage[];
}) {
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages ?? []);
  const [isStreaming, setIsStreaming] = useState(false);
  /** Só em agente procedural: a etapa e os dados, atualizados a cada resposta (evento `state`). */
  const [procedureState, setProcedureState] = useState<ProcedureState | null>(null);
  const streamingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const patchMessage = useCallback((id: string, patch: (message: ChatMessage) => ChatMessage) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? patch(m) : m)));
  }, []);

  const send = useCallback(
    async ({ text, sessionId, dependencies, attachments, dryRun }: SendOptions): Promise<boolean> => {
      const message = text.trim();
      if ((!message && !attachments?.length) || streamingRef.current) return false;

      streamingRef.current = true;
      setIsStreaming(true);
      const controller = new AbortController();
      abortRef.current = controller;

      const assistantId = createId();
      const now = Date.now();
      setMessages((prev) => [
        ...prev,
        {
          id: createId(),
          role: "user",
          content: message,
          createdAt: now,
          attachments: attachments?.map((a) => ({
            filename: a.filename,
            mime_type: a.mime_type,
            kind: a.mime_type?.split("/")[0] === "application" ? "file" : a.mime_type?.split("/")[0] ?? "file",
          })),
        },
        { id: assistantId, role: "assistant", content: "", pending: true, createdAt: now },
      ]);

      let receivedContent = false;
      try {
        const response = await fetch(apiUrl("/api/chat/stream"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            agent_type: agentType,
            user_id: userId,
            session_id: sessionId,
            message,
            dependencies: dependencies ?? undefined,
            attachments: attachments?.length ? attachments : undefined,
            dry_run: dryRun || undefined,
          }),
          signal: controller.signal,
        });

        if (!response.ok || !response.body) {
          throw new Error(await readErrorMessage(response, "Falha ao conversar com o agente"));
        }

        let streamError: string | null = null;
        for await (const sseEvent of parseSseStream(response.body)) {
          if (sseEvent.event === "message") {
            const { content } = JSON.parse(sseEvent.data) as { content?: string };
            if (!content) continue;
            receivedContent = true;
            patchMessage(assistantId, (m) => ({ ...m, content: m.content + content }));
          } else if (sseEvent.event === "usage") {
            const usage = JSON.parse(sseEvent.data) as UsageMetrics;
            patchMessage(assistantId, (m) => ({ ...m, usage }));
          } else if (sseEvent.event === "run") {
            const { run_id } = JSON.parse(sseEvent.data) as { run_id?: string };
            if (run_id) patchMessage(assistantId, (m) => ({ ...m, runId: run_id }));
          } else if (sseEvent.event === "sources") {
            const { sources } = JSON.parse(sseEvent.data) as { sources?: ChatSource[] };
            if (sources?.length) patchMessage(assistantId, (m) => ({ ...m, sources }));
          } else if (sseEvent.event === "state") {
            setProcedureState(JSON.parse(sseEvent.data) as ProcedureState);
          } else if (sseEvent.event === "error") {
            const { message: detail } = JSON.parse(sseEvent.data) as { message?: string };
            streamError = detail || "Falha ao executar o agente.";
          }
        }
        const finalError = streamError;
        patchMessage(assistantId, (m) => ({ ...m, pending: false, error: finalError ?? undefined }));
        return true;
      } catch (err) {
        if (controller.signal.aborted) {
          patchMessage(assistantId, (m) => ({ ...m, pending: false, stopped: true }));
          return receivedContent;
        }
        const errorText = err instanceof Error ? err.message : "Erro desconhecido";
        patchMessage(assistantId, (m) => ({ ...m, pending: false, error: errorText }));
        return receivedContent;
      } finally {
        streamingRef.current = false;
        abortRef.current = null;
        setIsStreaming(false);
      }
    },
    [agentType, userId, patchMessage]
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  /** Remove a mensagem `id` e tudo depois dela (usado no "tentar novamente"). */
  const truncateFrom = useCallback((id: string) => {
    setMessages((prev) => {
      const index = prev.findIndex((m) => m.id === id);
      return index === -1 ? prev : prev.slice(0, index);
    });
  }, []);

  return { messages, send, stop, truncateFrom, isStreaming, procedureState, setProcedureState };
}
