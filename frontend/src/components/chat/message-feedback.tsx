"use client";

import { useState } from "react";
import Link from "next/link";
import { Activity, ThumbsDown, ThumbsUp } from "lucide-react";
import { cn } from "@/lib/cn";
import { errorMessage, requestJson } from "@/lib/http";
import type { FeedbackNote } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import { useWorkspace } from "@/components/workspace/workspace-provider";

type Vote = 0 | 1;

/**
 * Avaliação da resposta, em duas camadas independentes:
 *
 * - o voto vira o score `feedback` do trace no Langfuse, e o link abre o trace
 *   no console. Isso depende do Langfuse e some quando ele está desligado;
 * - o polegar para baixo pergunta o que deveria ter sido diferente, e o texto
 *   vira uma regra de comportamento do agente (`/agents/{tipo}/feedback`).
 *   Isso vive no Postgres e funciona com o Langfuse desligado.
 *
 * Antes o componente inteiro sumia sem Langfuse — e, como ele vem desligado por
 * padrão, ensinar o agente pela tela simplesmente não existia.
 */
export function MessageFeedback({
  runId,
  agentType,
  sessionId,
  canTeach = true,
  className,
}: {
  runId: string;
  agentType: string;
  sessionId: string | null;
  /** `false` em agente analista: a nota de feedback não se aplica a ele. */
  canTeach?: boolean;
  className?: string;
}) {
  const { observability, userId } = useWorkspace();
  const toast = useToast();
  const [vote, setVote] = useState<Vote | null>(null);
  const [sending, setSending] = useState(false);
  const [ensinando, setEnsinando] = useState(false);
  const [texto, setTexto] = useState("");

  async function sendVote(value: Vote) {
    if (value === 0 && sessionId && canTeach) setEnsinando(true);
    const previous = vote;
    setVote(value);
    if (!observability.enabled) return; // sem Langfuse não há score para registrar
    setSending(true);
    try {
      await requestJson("/api/observability/scores", {
        method: "POST",
        json: { run_id: runId, name: "feedback", value, user_id: userId },
        fallbackError: "Falha ao registrar o feedback",
      });
    } catch (err) {
      setVote(previous);
      toast({
        title: "Não foi possível registrar o feedback",
        description: errorMessage(err),
        variant: "error",
      });
    } finally {
      setSending(false);
    }
  }

  async function ensinar() {
    const feedback = texto.trim();
    if (!feedback || !sessionId) return;
    setSending(true);
    try {
      const note = await requestJson<FeedbackNote>(`/api/agents/${encodeURIComponent(agentType)}/feedback`, {
        method: "POST",
        // `run_id` marca esta resposta na transcrição que o merge lê: o feedback
        // é sobre ela, com o resto da conversa como contexto.
        json: { session_id: sessionId, feedback, run_id: runId },
        fallbackError: "Falha ao ensinar o agente",
      });
      const mudou = Object.entries(note.diff ?? {}).flatMap(([tipo, itens]) =>
        (itens ?? []).map((item) => `${tipo}: ${item}`)
      );
      toast({
        title: `Agente ajustado (v${note.version})`,
        description: mudou.length > 0 ? mudou.join(" · ") : "Nenhuma regra mudou — o feedback já estava coberto.",
        variant: "success",
      });
      setEnsinando(false);
      setTexto("");
    } catch (err) {
      toast({ title: "Não deu para ensinar", description: errorMessage(err), variant: "error" });
    } finally {
      setSending(false);
    }
  }

  return (
    // Ensinando, o bloco ocupa a linha inteira do rodapé: o campo de texto precisa
    // de espaço, e na altura de uma linha de ícones ele ficava cortado.
    <div className={cn("flex flex-col gap-1.5", ensinando && "basis-full")}>
      <div className={cn("flex items-center", className)} data-voted={vote === null ? undefined : true}>
        {([1, 0] as const).map((value) => {
          const active = vote === value;
          const label = value === 1 ? "Resposta útil" : "Resposta ruim — ensinar o agente";
          return (
            <Button
              key={value}
              variant="ghost"
              size="icon-xs"
              disabled={sending}
              aria-pressed={active}
              aria-label={label}
              title={label}
              onClick={() => void sendVote(value)}
              className={cn(
                "text-muted-foreground hover:text-foreground",
                active && (value === 1 ? "text-success" : "text-destructive")
              )}
            >
              {value === 1 ? <ThumbsUp /> : <ThumbsDown />}
            </Button>
          );
        })}
        {observability.enabled && (
          <Link
            href={`/runs/${encodeURIComponent(runId)}`}
            title="Ver o trace desta resposta: spans, tokens, custo e avaliações"
            className="touch-target relative inline-flex h-6 items-center gap-1 rounded-md px-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <Activity className="size-3" />
            Trace
          </Link>
        )}
      </div>

      {ensinando && (
        <div className="flex items-center gap-2">
          <Input
            autoFocus
            value={texto}
            onChange={(e) => setTexto(e.target.value)}
            placeholder="O que deveria ter sido diferente?"
            className="h-8 max-w-xl flex-1 text-[13px]"
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void ensinar();
              }
              if (e.key === "Escape") setEnsinando(false);
            }}
          />
          <Button size="sm" disabled={!texto.trim() || sending} onClick={() => void ensinar()}>
            Ensinar
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setEnsinando(false)}>
            Agora não
          </Button>
        </div>
      )}
    </div>
  );
}
