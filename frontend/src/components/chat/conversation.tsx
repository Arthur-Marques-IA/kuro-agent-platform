"use client";

import { useDryRun } from "@/components/tools/dry-run-toggle";
import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowDown,
  Brain,
  Braces,
  Check,
  ChevronDown,
  CircleAlert,
  CodeXml,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  Settings2,
  Sparkles,
  SquarePen,
  Zap,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";
import { apiUrl } from "@/lib/http";
import { createId } from "@/lib/id";
import { MEMORY_BACKENDS, modelLabel, parseDependencies } from "@/lib/agent-meta";
import { sessionTitle } from "@/lib/sessions";
import { useChat } from "@/lib/use-chat";
import {
  contextToSend,
  eventsBefore,
  humanAgentEvent,
  parseContextBlocks,
  parseSentContext,
  serializeContextBlocks,
  sessionNameFromSpeech,
  validateContextBlocks,
  type ChatEventPayload,
  type ContextBlockDraft,
} from "@/lib/chat-request";
import { testDependenciesText } from "@/lib/test-context";
import { useLocalStorage, writeLocalStorage } from "@/lib/use-local-storage";
import type { Attachment, ChatMessage, ProcedureState } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogBody, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import { useToast } from "@/components/ui/toast";
import { AgentAvatar } from "@/components/agents/agent-avatar";
import { IntegrationPanel } from "@/components/integration/integration-panel";
import { SidebarTrigger } from "@/components/workspace/app-shell";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { AgentEditSheet } from "@/components/chat/agent-edit-sheet";
import { ChatInspector } from "@/components/chat/chat-inspector";
import { Composer } from "@/components/chat/composer";
import { MASCOT_GUTTER } from "@/components/brand/kuro-mascot";
import { HumanAgentBubble, MessageBubble } from "@/components/chat/message-bubble";
import { ProcedureStrip } from "@/components/chat/procedure-progress";

const SUGGESTIONS = [
  { icon: Sparkles, label: "O que você consegue fazer?", prompt: "O que você consegue fazer por mim?" },
  {
    icon: Brain,
    label: "Testar a memória",
    prompt: "Guarde esta preferência: prefiro respostas curtas e diretas.",
  },
  {
    icon: Braces,
    label: "Usar o contexto enviado",
    prompt: "Com base no contexto que você recebeu sobre mim, como pode me ajudar?",
  },
];

const INSPECTOR_BREAKPOINT = "(min-width: 1280px)";

interface ConversationProps {
  /** `null` = conversa nova, ainda sem sessão no backend. */
  sessionId: string | null;
  agentType: string;
  initialMessages?: ChatMessage[];
  onAgentChange?: (agentType: string) => void;
  createdSessionId?: string | null;
  onSessionCreated?: (sessionId: string) => void;
  onFirstExchangeComplete?: (sessionId: string, messages: ChatMessage[]) => void;
}

export function Conversation({
  sessionId,
  agentType,
  initialMessages,
  onAgentChange,
  createdSessionId,
  onSessionCreated,
  onFirstExchangeComplete,
}: ConversationProps) {
  const toast = useToast();
  const { userId, getAgent, sessions, refreshSessions, renameSession, backendReachable, agents } = useWorkspace();
  const agent = getAgent(agentType);
  const { messages, send, stop, truncateFrom, isStreaming, procedureState, setProcedureState } = useChat({
    agentType,
    agentName: agent?.name,
    userId,
    initialMessages,
  });
  const procedural = agent?.kind === "procedural";

  const [inspectorPref, setInspectorPref] = useLocalStorage<"open" | "closed">("agent-service:inspector", "closed");
  const [mobileInspectorOpen, setMobileInspectorOpen] = useState(false);
  const [codeOpen, setCodeOpen] = useState(false);
  const [editAgentOpen, setEditAgentOpen] = useState(false);
  // Id da conversa antes do 1º envio: vira o session_id e semeia os ids fictícios do contexto de teste.
  const [draftId] = useState(createId);
  const seedId = sessionId ?? createdSessionId ?? draftId;
  // Sem edição manual vale o preset de teste (todos os campos declarados, com valores fictícios).
  // A edição é guardada por agente: o contexto de um não vaza para o outro.
  const presetText = useMemo(() => testDependenciesText(agent?.dependency_fields, seedId), [agent?.dependency_fields, seedId]);
  const dependenciesKey = `agent-service:dependencies:${agentType}`;
  const [dependenciesText, setDependenciesText] = useLocalStorage<string>(dependenciesKey, presetText);
  const dependencies = useMemo(() => parseDependencies(dependenciesText), [dependenciesText]);
  const [dryRun, setDryRun] = useDryRun();
  // Blocos de `context`: guardados por agente, como as dependencies. Procedural recusa (422).
  const blocksKey = `agent-service:context-blocks:${agentType}`;
  const [blocksText, setBlocksText] = useLocalStorage<string>(blocksKey, "[]");
  const blocks = useMemo(() => parseContextBlocks(blocksText), [blocksText]);
  const blocksValidation = useMemo(() => validateContextBlocks(blocks), [blocks]);
  const [contextTab, setContextTab] = useState("dependencies");
  // Falas de atendente registradas e ainda não enviadas: vão em `events` com a próxima mensagem.
  const [pendingEvents, setPendingEvents] = useState<ChatEventPayload[]>([]);

  // Conversa nova: depois que a primeira troca termina, sobe para a rota da sessão.
  const [completedSessionId, setCompletedSessionId] = useState<string | null>(null);
  const handedOffRef = useRef(false);
  useEffect(() => {
    if (!completedSessionId || isStreaming || handedOffRef.current) return;
    handedOffRef.current = true;
    onFirstExchangeComplete?.(completedSessionId, messages);
  }, [completedSessionId, isStreaming, messages, onFirstExchangeComplete]);

  const activeSessionId = sessionId ?? createdSessionId ?? null;
  // O servidor lembra o contexto da sessão: os blocos só vão quando mudaram desde o último
  // envio desta conversa (`contextToSend`). O último envio fica guardado por conversa.
  const sentContextKey = `agent-service:context-sent:${activeSessionId ?? draftId}`;
  const [sentContextText] = useLocalStorage<string>(sentContextKey, "");
  const nextContext = procedural
    ? undefined
    : contextToSend(blocksValidation.blocks, parseSentContext(sentContextText));

  // Conversa reaberta com um agente procedural: as etapas vêm do backend, não das mensagens.
  const [restoredFor, setRestoredFor] = useState<string | null>(null);
  useEffect(() => {
    if (!procedural || !sessionId) return;
    const controller = new AbortController();
    fetch(apiUrl(`/api/agents/${encodeURIComponent(agentType)}/procedures/${encodeURIComponent(sessionId)}`), {
      signal: controller.signal,
    })
      .then((res) => (res.ok ? (res.json() as Promise<ProcedureState>) : null))
      .then((state) => {
        if (state) setProcedureState(state);
      })
      .catch(() => {
        // Sem o estado a conversa continua funcionando; a faixa só começa da etapa 1.
      })
      .finally(() => {
        if (!controller.signal.aborted) setRestoredFor(sessionId);
      });
    return () => controller.abort();
  }, [procedural, agentType, sessionId, setProcedureState]);
  const restoring = procedural && Boolean(sessionId) && restoredFor !== sessionId;
  const session = sessionId ? sessions.find((s) => s.session_id === sessionId) : undefined;
  // No "Código": as falas de atendente pendentes ou, sem elas, as que foram com a última mensagem.
  const lastUserIndex = messages.findLastIndex((m) => m.role === "user");
  const lastSentEvents = lastUserIndex === -1 ? [] : eventsBefore(messages, lastUserIndex);
  const totalTokens = messages.reduce((sum, m) => sum + (m.usage?.total_tokens ?? 0), 0);
  const inspectorOpen = inspectorPref === "open";

  const blockedReason = !backendReachable
    ? "O agent-service está indisponível."
    : agents.length === 0
      ? "Nenhum agente cadastrado."
      : !agent
        ? `O agente "${agentType}" não existe mais.`
        : null;

  async function runSend(text: string, attachments: Attachment[], events: ChatEventPayload[]) {
    const id = activeSessionId ?? draftId;
    if (!activeSessionId) onSessionCreated?.(id);
    const context = nextContext;
    const sentEvents = procedural ? [] : events;
    const firstTurn = messages.length === 0;
    const executed = await send({
      text,
      sessionId: id,
      dependencies: dependencies.value,
      attachments,
      dryRun,
      context,
      events: sentEvents,
    });
    if (!executed) return;
    if (context !== undefined) writeLocalStorage(`agent-service:context-sent:${id}`, JSON.stringify(context));
    // 1º turno estruturado: sem isto a conversa se chamaria "# Mensagem nova do contato …" na lista.
    if (firstTurn && ((context?.length ?? 0) > 0 || sentEvents.length > 0) && text.trim()) {
      await renameSession(id, sessionNameFromSpeech(text)).catch(() => {
        // Sem o nome a conversa continua; só o título na lista fica com o texto montado.
      });
    }
    if (sessionId) void refreshSessions();
    else setCompletedSessionId(id);
  }

  /**
   * Validação síncrona para o composer saber se limpa o campo. `retryEvents`: os eventos
   * de um turno reenviado ("tentar novamente"); sem ele vão as falas de atendente pendentes.
   */
  function handleSend(text: string, attachments: Attachment[] = [], retryEvents?: ChatEventPayload[]): boolean {
    if (isStreaming || blockedReason) return false;
    if (dependencies.error) {
      toast({ title: "Contexto inválido", description: dependencies.error, variant: "error" });
      openInspector("dependencies");
      return false;
    }
    if (!procedural && blocksValidation.error) {
      toast({ title: "Blocos de contexto inválidos", description: blocksValidation.error, variant: "error" });
      openInspector("blocks");
      return false;
    }
    void runSend(text, attachments, retryEvents ?? pendingEvents);
    if (!retryEvents) setPendingEvents([]);
    return true;
  }

  function handleRetry(assistantMessageId: string) {
    const index = messages.findIndex((m) => m.id === assistantMessageId);
    let userIndex = -1;
    for (let i = index - 1; i >= 0; i--) {
      if (messages[i].role === "user") {
        userIndex = i;
        break;
      }
    }
    if (userIndex === -1) return;
    // As falas de atendente que foram junto daquela mensagem voltam a ir junto.
    const events = eventsBefore(messages, userIndex);
    truncateFrom(messages[userIndex - events.length].id);
    handleSend(messages[userIndex].content, [], events);
  }

  function addHumanAgent(text: string) {
    if (!text.trim()) return;
    setPendingEvents((prev) => [...prev, humanAgentEvent(text)]);
  }

  /** `tab`: abre o painel na aba de contexto indicada e foca o primeiro campo dela. */
  function openInspector(tab?: "dependencies" | "blocks") {
    if (window.matchMedia(INSPECTOR_BREAKPOINT).matches) setInspectorPref("open");
    else setMobileInspectorOpen(true);
    if (!tab) return;
    setContextTab(tab);
    const target = tab === "blocks" ? "chat-context-blocks" : "chat-dependencies";
    window.setTimeout(() => {
      const el = document.getElementById(target);
      if (tab === "dependencies") el?.focus();
      else (el?.querySelector<HTMLElement>("[aria-invalid=true], input, textarea") ?? el)?.focus();
    }, 50);
  }

  function toggleInspector() {
    if (window.matchMedia(INSPECTOR_BREAKPOINT).matches) setInspectorPref(inspectorOpen ? "closed" : "open");
    else setMobileInspectorOpen(true);
  }

  const inspectorProps = {
    agent,
    agentType,
    sessionId: activeSessionId,
    userId,
    messageCount: messages.length,
    totalTokens,
    dependenciesText,
    presetText,
    onDependenciesChange: setDependenciesText,
    onDependenciesReset: () => writeLocalStorage(dependenciesKey, null),
    dependenciesError: dependencies.error,
    dependenciesCount: dependencies.count,
    contextTab,
    onContextTabChange: setContextTab,
    blocks,
    blocksValidation,
    onBlocksChange: (next: ContextBlockDraft[]) =>
      next.length === 0 ? writeLocalStorage(blocksKey, null) : setBlocksText(serializeContextBlocks(next)),
    dryRun,
    onDryRunChange: setDryRun,
    procedureState,
    onEditAgent: () => {
      setMobileInspectorOpen(false);
      setEditAgentOpen(true);
    },
    onShowCode: () => {
      setMobileInspectorOpen(false);
      setCodeOpen(true);
    },
  };

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-1.5 border-b border-border px-3 sm:px-4">
          <SidebarTrigger />
          <AgentPicker
            value={agentType}
            onChange={onAgentChange}
            locked={!onAgentChange || messages.length > 0}
          />
          {session && (
            <>
              <span className="hidden text-muted-foreground/50 md:inline">/</span>
              <span className="hidden min-w-0 truncate text-sm text-muted-foreground md:inline">
                {sessionTitle(session)}
              </span>
            </>
          )}
          <div className="ml-auto flex shrink-0 items-center gap-1">
            {totalTokens > 0 && (
              <Badge variant="secondary" className="mr-1 hidden sm:inline-flex" title="Tokens consumidos nesta conversa">
                <Zap /> {formatNumber(totalTokens)}
              </Badge>
            )}
            {messages.length > 0 && (
              <Link
                href={`/chat?agent=${encodeURIComponent(agentType)}`}
                className={buttonVariants({ variant: "ghost", size: "icon-sm" })}
                title="Nova conversa com este agente"
                aria-label="Nova conversa com este agente"
              >
                <SquarePen />
              </Link>
            )}
            <Button variant="ghost" size="sm" onClick={() => setCodeOpen(true)} title="Código de integração">
              <CodeXml />
              <span className="hidden md:inline">Código</span>
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={toggleInspector}
              aria-label={inspectorOpen ? "Fechar detalhes" : "Abrir detalhes"}
              aria-pressed={inspectorOpen}
              title="Detalhes da conversa"
            >
              {inspectorOpen ? <PanelRightClose className="xl:block" /> : <PanelRightOpen />}
            </Button>
          </div>
        </header>

        {procedural && agent && (
          <ProcedureStrip
            state={procedureState}
            stages={agent.stages ?? []}
            onOpen={() => openInspector()}
            loading={restoring}
          />
        )}

        {blockedReason && messages.length > 0 && (
          <div className="flex items-center gap-2 border-b border-warning/30 bg-warning/10 px-4 py-2 text-[13px] text-warning">
            <CircleAlert className="size-4 shrink-0" />
            <span className="min-w-0 flex-1">{blockedReason} Inicie uma nova conversa com outro agente.</span>
          </div>
        )}

        <MessageList
          messages={messages}
          agentType={agentType}
          agentName={agent?.name}
          sessionId={activeSessionId}
          onRetry={handleRetry}
          pending={pendingEvents}
          onRemovePending={(index) => setPendingEvents((prev) => prev.filter((_, i) => i !== index))}
          empty={
            <ChatEmptyState
              agentType={agentType}
              blockedReason={blockedReason}
              onSuggestion={(prompt) => handleSend(prompt)}
            />
          }
        />

        {/* Com os detalhes abertos o composer ocupa a largura toda: recua do canto do Kuro. */}
        <div className={cn("shrink-0 px-3 pb-3 sm:px-6 sm:pb-5", inspectorOpen && MASCOT_GUTTER)}>
          <div className="mx-auto w-full max-w-3xl">
            <Composer
              onSend={handleSend}
              onAttachError={(description) => toast({ title: "Anexo recusado", description, variant: "error" })}
              onStop={stop}
              isStreaming={isStreaming}
              disabled={Boolean(blockedReason)}
              placeholder={
                blockedReason ?? (agent ? `Mensagem para ${agent.name}…` : "Escreva uma mensagem…")
              }
              contextCount={dependencies.count + (procedural ? 0 : blocksValidation.blocks.length)}
              contextInvalid={Boolean(dependencies.error) || (!procedural && Boolean(blocksValidation.error))}
              onOpenContext={() =>
                openInspector(
                  !procedural && !dependencies.error && (blocksValidation.error || contextTab === "blocks")
                    ? "blocks"
                    : "dependencies"
                )
              }
              onAddHumanAgent={procedural ? undefined : addHumanAgent}
            />
          </div>
        </div>
      </div>

      {inspectorOpen && (
        <aside className="hidden w-80 shrink-0 border-l border-border bg-surface xl:flex xl:flex-col">
          <ChatInspector {...inspectorProps} onClose={() => setInspectorPref("closed")} />
        </aside>
      )}
      <Dialog open={mobileInspectorOpen} onOpenChange={setMobileInspectorOpen} side="right" hideClose className="bg-surface">
        <ChatInspector {...inspectorProps} onClose={() => setMobileInspectorOpen(false)} />
      </Dialog>

      <Dialog open={codeOpen} onOpenChange={setCodeOpen} size="lg">
        <DialogHeader>
          <DialogTitle>Código de integração</DialogTitle>
          <DialogDescription>
            A chamada equivalente a esta conversa, direto no agent-service — como outro módulo da plataforma faria.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <IntegrationPanel
            agentType={agentType}
            userId={userId}
            sessionId={activeSessionId ?? "nova-sessao-uuid"}
            dependencies={dependencies.value}
            context={nextContext}
            contextInherited={!procedural && nextContext === undefined && sentContextText !== ""}
            events={procedural ? undefined : pendingEvents.length ? pendingEvents : lastSentEvents}
          />
        </DialogBody>
      </Dialog>

      {agent && <AgentEditSheet open={editAgentOpen} onOpenChange={setEditAgentOpen} agent={agent} />}
    </div>
  );
}

function AgentPicker({
  value,
  onChange,
  locked,
}: {
  value: string;
  onChange?: (agentType: string) => void;
  locked: boolean;
}) {
  const router = useRouter();
  const { agents, getAgent } = useWorkspace();
  const agent = getAgent(value);
  const label = agent?.name ?? value;
  // Um analista não atende no /chat: ele é one-shot e devolve objeto, não texto.
  // Oferecê-lo aqui seria oferecer uma conversa que não acontece.
  const conversacionais = agents.filter((a) => a.kind !== "analysis");

  if (locked || !onChange) {
    return (
      <div className="flex h-9 min-w-0 items-center gap-2 px-1.5 text-sm font-medium" title="O agente é fixo durante a conversa">
        <AgentAvatar agentType={value} name={label} size="xs" />
        {agent ? (
          <Link href={`/agents/${encodeURIComponent(value)}`} className="truncate hover:underline">
            {label}
          </Link>
        ) : (
          <span className="truncate">{label}</span>
        )}
      </div>
    );
  }

  return (
    <DropdownMenu
      align="start"
      className="w-80"
      trigger={(props) => (
        <button
          {...props}
          type="button"
          className="flex h-9 min-w-0 items-center gap-2 rounded-lg px-1.5 text-sm font-medium transition-colors hover:bg-accent"
        >
          <AgentAvatar agentType={value} name={label} size="xs" />
          <span className="truncate">{label}</span>
          <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
        </button>
      )}
    >
      <DropdownMenuLabel>Conversar com</DropdownMenuLabel>
      {conversacionais.map((a) => (
        <DropdownMenuItem key={a.agent_type} onSelect={() => onChange(a.agent_type)} className="py-2">
          <span className="flex items-center gap-2.5">
            <AgentAvatar agentType={a.agent_type} name={a.name} size="sm" />
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate font-medium">{a.name}</span>
              <span className="truncate text-xs text-muted-foreground">
                {modelLabel(a.model_id)} · memória {MEMORY_BACKENDS[a.memory_backend]?.label.toLowerCase()}
              </span>
            </span>
            {a.agent_type === value && <Check className="size-4 shrink-0 text-primary" />}
          </span>
        </DropdownMenuItem>
      ))}
      <DropdownMenuSeparator />
      <DropdownMenuItem icon={Plus} onSelect={() => router.push("/agents/new")}>
        Criar agente
      </DropdownMenuItem>
      <DropdownMenuItem icon={Settings2} onSelect={() => router.push("/agents")}>
        Gerenciar agentes
      </DropdownMenuItem>
    </DropdownMenu>
  );
}

function MessageList({
  messages,
  agentType,
  agentName,
  sessionId,
  onRetry,
  pending,
  onRemovePending,
  empty,
}: {
  messages: ChatMessage[];
  agentType: string;
  agentName?: string;
  sessionId: string | null;
  onRetry: (assistantMessageId: string) => void;
  /** Falas de atendente registradas que vão com a próxima mensagem. */
  pending: ChatEventPayload[];
  onRemovePending: (index: number) => void;
  empty: ReactNode;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const [showJump, setShowJump] = useState(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [messages, pending]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    stickToBottomRef.current = atBottom;
    if (showJump === atBottom) setShowJump(!atBottom);
  }

  function jumpToBottom() {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }

  const lastMessageId = messages.at(-1)?.id;

  return (
    <div className="relative min-h-0 flex-1">
      <div ref={scrollRef} onScroll={handleScroll} className="scrollbar-thin h-full overflow-y-auto">
        {messages.length === 0 && pending.length === 0 ? (
          empty
        ) : (
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-4 py-6 sm:px-6">
            {messages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                agentType={agentType}
                agentName={agentName}
                sessionId={sessionId}
                onRetry={message.id === lastMessageId ? () => onRetry(message.id) : undefined}
              />
            ))}
            {pending.map((event, index) => (
              <HumanAgentBubble
                key={`${event.at}-${index}`}
                content={event.content}
                createdAt={event.at ? Date.parse(event.at) : undefined}
                pending
                onRemove={() => onRemovePending(index)}
              />
            ))}
          </div>
        )}
      </div>
      {showJump && (
        <Button
          variant="outline"
          size="icon-sm"
          onClick={jumpToBottom}
          aria-label="Ir para a mensagem mais recente"
          className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full shadow-md"
        >
          <ArrowDown />
        </Button>
      )}
    </div>
  );
}

function ChatEmptyState({
  agentType,
  blockedReason,
  onSuggestion,
}: {
  agentType: string;
  blockedReason: string | null;
  onSuggestion: (prompt: string) => void;
}) {
  const { getAgent } = useWorkspace();
  const agent = getAgent(agentType);

  return (
    <div className="mx-auto flex min-h-full w-full max-w-2xl flex-col items-center justify-center gap-6 px-4 py-10 text-center">
      {agent ? (
        <>
          <AgentAvatar agentType={agent.agent_type} name={agent.name} size="lg" />
          <div className="flex flex-col gap-1.5">
            <h2 className="text-xl font-semibold tracking-tight">Converse com {agent.name}</h2>
            <p className="line-clamp-2 text-sm text-muted-foreground">{agent.instructions.join(" ")}</p>
          </div>
          <div className="flex flex-wrap justify-center gap-1.5">
            <Badge variant="outline">{modelLabel(agent.model_id)}</Badge>
            <Badge variant="outline">Memória {MEMORY_BACKENDS[agent.memory_backend]?.label.toLowerCase()}</Badge>
            <Badge variant="outline">Prompt v{agent.prompt_version}</Badge>
          </div>
          <div className="grid w-full gap-2 sm:grid-cols-3">
            {SUGGESTIONS.map((suggestion) => (
              <button
                key={suggestion.label}
                type="button"
                onClick={() => onSuggestion(suggestion.prompt)}
                className={cn(
                  "flex flex-col items-start gap-2 rounded-xl border border-border bg-card p-3.5 text-left text-[13px] shadow-xs transition-colors",
                  "hover:border-ring/40 hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                )}
              >
                <suggestion.icon className="size-4 text-muted-foreground" />
                {suggestion.label}
              </button>
            ))}
          </div>
        </>
      ) : (
        <>
          <div className="flex size-12 items-center justify-center rounded-xl border border-border bg-surface text-muted-foreground">
            <CircleAlert className="size-5" />
          </div>
          <div className="flex flex-col gap-1.5">
            <h2 className="text-lg font-semibold">Playground indisponível</h2>
            <p className="text-sm text-muted-foreground">{blockedReason}</p>
          </div>
          <Link href="/agents/new" className={buttonVariants({ variant: "outline" })}>
            <Plus /> Criar agente
          </Link>
        </>
      )}
    </div>
  );
}
