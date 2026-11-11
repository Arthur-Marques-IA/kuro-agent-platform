"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { CircleAlert, MessagesSquare, RotateCcw, SquarePen } from "lucide-react";
import { ApiError, errorMessage, requestJson } from "@/lib/http";
import { runsToMessages } from "@/lib/sessions";
import { clearTranscript, peekTranscript, stashTranscript } from "@/lib/transcript-cache";
import { useLocalStorage } from "@/lib/use-local-storage";
import { applyTimeline } from "@/lib/chat-request";
import type { ChatMessage, SessionRun, SessionTimeline } from "@/lib/types";
import { Button, buttonVariants } from "@/components/ui/button";
import { EmptyState, Skeleton } from "@/components/ui/primitives";
import { SidebarTrigger } from "@/components/workspace/app-shell";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { Conversation } from "@/components/chat/conversation";

/**
 * Ponto de entrada do playground. `/chat` = conversa nova (o id da sessão
 * nasce no primeiro envio); `/chat/[sessionId]` = conversa existente,
 * reidratada a partir dos runs do AgentOS.
 */
export function ChatView({ sessionId, requestedAgent }: { sessionId: string | null; requestedAgent?: string }) {
  const { userId } = useWorkspace();
  // `userId` vem do localStorage: vazio no servidor e durante a hidratação.
  if (!userId) return <ConversationSkeleton />;
  if (sessionId) return <ExistingConversation key={sessionId} sessionId={sessionId} />;
  return <NewConversation key={requestedAgent ?? ""} requestedAgent={requestedAgent} />;
}

function NewConversation({ requestedAgent }: { requestedAgent?: string }) {
  const router = useRouter();
  const { agents, refreshSessions } = useWorkspace();
  const [lastAgent, setLastAgent] = useLocalStorage<string>("agent-service:last-agent", "");
  const [picked, setPicked] = useState<string | null>(null);
  const [createdSessionId, setCreatedSessionId] = useState<string | null>(null);

  // Analista não conversa: nem como escolha guardada, nem como padrão da tela.
  const conversacionais = agents.filter((a) => a.kind !== "analysis");
  const exists = (slug?: string | null) =>
    Boolean(slug) && conversacionais.some((a) => a.agent_type === slug);
  const agentType =
    [picked, requestedAgent, lastAgent].find(exists) ?? conversacionais[0]?.agent_type ?? "conversational";

  return (
    <Conversation
      sessionId={null}
      agentType={agentType}
      onAgentChange={(slug) => {
        setPicked(slug);
        setLastAgent(slug);
      }}
      onSessionCreated={setCreatedSessionId}
      createdSessionId={createdSessionId}
      onFirstExchangeComplete={(id, messages: ChatMessage[]) => {
        // Entrega o histórico para a rota da sessão sem piscar nem refazer fetch.
        stashTranscript(id, { agentType, messages });
        void refreshSessions();
        router.replace(`/chat/${encodeURIComponent(id)}`);
      }}
    />
  );
}

type LoadState =
  | { status: "loading" }
  | { status: "ready"; messages: ChatMessage[]; agentType: string | null }
  | { status: "not-found" }
  | { status: "error"; message: string };

function ExistingConversation({ sessionId }: { sessionId: string }) {
  const { userId, sessions, agents } = useWorkspace();
  const [cached] = useState(() => peekTranscript(sessionId));
  const [state, setState] = useState<LoadState>(() =>
    cached ? { status: "ready", messages: cached.messages, agentType: cached.agentType } : { status: "loading" }
  );
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (cached) {
      clearTranscript(sessionId);
      return;
    }
    let cancelled = false;
    const query = new URLSearchParams({ user_id: userId });
    // Turno estruturado: o `run_input` do AgentOS é o texto montado pelo servidor. A linha do
    // tempo devolve a fala e os eventos separados; sem ela (404, 501) o histórico fica como vem.
    const timeline = requestJson<SessionTimeline>(
      `/api/sessions/${encodeURIComponent(sessionId)}/timeline?${query}`,
      { fallbackError: "Falha ao ler a linha do tempo" }
    ).catch(() => null);
    Promise.all([
      requestJson<SessionRun[]>(`/api/sessions/${encodeURIComponent(sessionId)}/runs?${query}`, {
        fallbackError: "Falha ao carregar a conversa",
      }),
      timeline,
    ])
      .then(([runs, turns]) => {
        if (cancelled) return;
        setState(
          runs.length === 0
            ? { status: "not-found" }
            : {
                status: "ready",
                messages: applyTimeline(runsToMessages(runs), turns?.items ?? []),
                agentType: runs.find((run) => run.agent_id)?.agent_id ?? null,
              }
        );
      })
      .catch((err) => {
        if (cancelled) return;
        setState(
          err instanceof ApiError && err.status === 404
            ? { status: "not-found" }
            : { status: "error", message: errorMessage(err) }
        );
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, userId, cached, attempt]);

  if (state.status === "loading") return <ConversationSkeleton />;

  if (state.status === "not-found" || state.status === "error") {
    const notFound = state.status === "not-found";
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex h-14 shrink-0 items-center border-b border-border px-3 lg:hidden">
          <SidebarTrigger />
        </div>
        <EmptyState
          className="flex-1"
          icon={notFound ? MessagesSquare : CircleAlert}
          title={notFound ? "Conversa não encontrada" : "Não foi possível abrir a conversa"}
          description={
            notFound
              ? "O Playground só abre conversas deste navegador. Ela pode pertencer a outro usuário (CLI, API, outro navegador), ser de um agente analista, que não guarda conversa, ou ter sido excluída — em Logs dá para ver qualquer sessão."
              : state.message
          }
          action={
            <div className="flex gap-2">
              {!notFound && (
                <Button
                  variant="outline"
                  onClick={() => {
                    setState({ status: "loading" });
                    setAttempt((n) => n + 1);
                  }}
                >
                  <RotateCcw /> Tentar de novo
                </Button>
              )}
              {notFound && (
                <Link href={`/logs/sessions/${encodeURIComponent(sessionId)}`} className={buttonVariants({ variant: "outline" })}>
                  Ver em Logs
                </Link>
              )}
              <Link href="/chat" className={buttonVariants({ variant: notFound ? "default" : "ghost" })}>
                <SquarePen /> Nova conversa
              </Link>
            </div>
          }
        />
      </div>
    );
  }

  const session = sessions.find((s) => s.session_id === sessionId);
  const agentType =
    state.agentType ??
    session?.agent_id ??
    agents.find((a) => a.kind !== "analysis")?.agent_type ??
    "conversational";

  return <Conversation sessionId={sessionId} agentType={agentType} initialMessages={state.messages} />;
}

function ConversationSkeleton() {
  return (
    <div className="flex min-h-0 flex-1 flex-col" aria-busy="true" aria-label="Carregando conversa">
      <div className="flex h-14 shrink-0 items-center gap-3 border-b border-border px-3 sm:px-4">
        <SidebarTrigger />
        <Skeleton className="size-5 rounded-md" />
        <Skeleton className="h-4 w-36" />
      </div>
      <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-4 py-8 sm:px-6">
        <Skeleton className="ml-auto h-10 w-2/5 rounded-2xl" />
        <div className="flex gap-3">
          <Skeleton className="size-7 rounded-lg" />
          <div className="flex flex-1 flex-col gap-2">
            <Skeleton className="h-4 w-4/5" />
            <Skeleton className="h-4 w-3/5" />
          </div>
        </div>
      </div>
    </div>
  );
}
