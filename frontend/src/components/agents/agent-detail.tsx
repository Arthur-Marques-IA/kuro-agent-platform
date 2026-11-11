"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowUpRight, Copy, Ellipsis, FileSearch, MessageSquare, MessagesSquare, Trash } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import { formatNumber } from "@/lib/format";
import { MEMORY_BACKENDS, modelLabel } from "@/lib/agent-meta";
import { sessionTitle } from "@/lib/sessions";
import type { AgentDefinition, PromptVersion } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { DropdownMenu, DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { EmptyState, RelativeTime, SectionHeading, Skeleton } from "@/components/ui/primitives";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useConfirm } from "@/components/ui/confirm";
import { useToast } from "@/components/ui/toast";
import { useReserveMascotSpace } from "@/components/brand/kuro-mascot";
import { AgentAvatar } from "@/components/agents/agent-avatar";
import { AgentForm, type AgentFormPayload } from "@/components/agents/agent-form";
import { VersionHistory } from "@/components/agents/version-history";
import { PromoteDialog, RevisionsPanel } from "@/components/agents/revisions-panel";
import { ProcedureFunnelPanel } from "@/components/agents/procedure-funnel";
import { AgreementPanel } from "@/components/observability/agreement-panel";
import { FeedbackPanel } from "@/components/agents/feedback-panel";
import { IntegrationPanel } from "@/components/integration/integration-panel";
import { AgentRuns } from "@/components/observability/agent-runs";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";

const TABS = ["config", "versions", "feedback", "runs", "conversations", "integration"] as const;
type TabValue = (typeof TABS)[number];

function isTab(value: string | undefined): value is TabValue {
  return TABS.includes(value as TabValue);
}

export function AgentDetail({
  agent,
  versions,
  initialTab,
}: {
  agent: AgentDefinition;
  versions: PromptVersion[];
  initialTab?: string;
}) {
  const router = useRouter();
  const toast = useToast();
  const confirm = useConfirm();
  const { availableTools, sessions } = useWorkspace();
  const [tab, setTab] = useState<TabValue>(isTab(initialTab) ? initialTab : "config");
  // `key` remonta o formulário quando uma versão antiga é carregada no editor.
  const [draft, setDraft] = useState<{ key: number; instructions?: string[] }>({ key: 0 });
  const [promoteOpen, setPromoteOpen] = useState(false);
  // A aba Configuração tem a barra de salvar fixa no rodapé, no canto do Kuro.
  useReserveMascotSpace(tab === "config");
  const agentSessions = sessions.filter((s) => s.agent_id === agent.agent_type);
  const slug = encodeURIComponent(agent.agent_type);

  function changeTab(value: string) {
    if (!isTab(value)) return;
    setTab(value);
    const url = new URL(window.location.href);
    if (value === "config") url.searchParams.delete("tab");
    else url.searchParams.set("tab", value);
    window.history.replaceState(null, "", url);
  }

  async function handleSave(payload: AgentFormPayload) {
    const updated = await requestJson<AgentDefinition>(`/api/agents/${slug}`, {
      method: "PUT",
      json: payload,
      fallbackError: "Falha ao salvar agente",
    });
    toast({
      title: "Agente atualizado",
      description: payload.instructions
        ? `Prompt publicado como v${updated.prompt_version} — vale para as próximas mensagens.`
        : "As alterações já valem para as próximas mensagens.",
      variant: "success",
    });
    router.refresh();
  }

  function handleRestore(version: PromptVersion) {
    setDraft((prev) => ({ key: prev.key + 1, instructions: version.instructions }));
    changeTab("config");
    toast({
      title: `Versão ${version.version} carregada no editor`,
      description: "Revise e salve para publicá-la como uma nova versão.",
    });
  }

  async function handleDelete() {
    const confirmed = await confirm({
      title: `Excluir "${agent.name}"?`,
      description:
        "O agente deixa de responder na API imediatamente. As conversas já existentes continuam no histórico.",
      confirmLabel: "Excluir agente",
      destructive: true,
    });
    if (!confirmed) return;
    try {
      await requestJson(`/api/agents/${slug}`, { method: "DELETE", fallbackError: "Falha ao excluir agente" });
      toast({ title: "Agente excluído", variant: "success" });
      router.push("/agents");
      router.refresh();
    } catch (err) {
      toast({ title: "Não foi possível excluir", description: errorMessage(err), variant: "error" });
    }
  }

  return (
    <Tabs value={tab} onValueChange={changeTab} className="flex min-h-0 flex-1 flex-col">
      <PageHeader
        breadcrumbs={[{ label: "Agentes", href: "/agents" }, { label: agent.name }]}
        title={
          <span className="flex min-w-0 items-center gap-3">
            <AgentAvatar agentType={agent.agent_type} name={agent.name} size="sm" />
            <span className="truncate">{agent.name}</span>
          </span>
        }
        description={
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <span className="inline-flex items-center rounded-md bg-muted pl-2 font-mono text-xs text-foreground/80">
              {agent.agent_type}
              <CopyButton value={agent.agent_type} label="Copiar slug" className="size-6" />
            </span>
            <Badge variant="outline">{modelLabel(agent.model_id)}</Badge>
            {/* Um analista é one-shot: anunciar a memória dele seria anunciar
                algo que `agents/base.py` nem monta. */}
            {agent.kind === "procedural" && (
              <Badge variant="outline">Procedural · {agent.stages?.length ?? 0} etapas</Badge>
            )}
            {agent.kind === "analysis" ? (
              <Badge variant="outline">Analista</Badge>
            ) : (
              <Badge variant="outline">
                {agent.memory_backend === "none"
                  ? "Sem memória longa"
                  : `Memória ${MEMORY_BACKENDS[agent.memory_backend]?.label ?? agent.memory_backend}`}
              </Badge>
            )}
            <Badge variant="outline">Prompt v{agent.prompt_version}</Badge>
            {agent.is_seed && <Badge variant="secondary">sistema</Badge>}
            <span className="ml-1 text-xs">
              Atualizado <RelativeTime date={agent.updated_at} />
            </span>
          </div>
        }
        actions={
          <>
            {/* Analista não atende no /chat — o lugar de testá-lo é a Análise. */}
            {agent.kind === "analysis" ? (
              <Link href="/analyze" className={buttonVariants()}>
                <FileSearch />
                <span className="hidden sm:inline">Analisar</span>
              </Link>
            ) : (
              <Link href={`/chat?agent=${slug}`} className={buttonVariants()}>
                <MessageSquare />
                <span className="hidden sm:inline">Conversar</span>
              </Link>
            )}
            <DropdownMenu
              align="end"
              trigger={(props) => (
                <Button {...props} variant="outline" size="icon" aria-label="Mais ações">
                  <Ellipsis />
                </Button>
              )}
            >
              <DropdownMenuItem icon={Copy} onSelect={() => void navigator.clipboard?.writeText(agent.agent_type)}>
                Copiar slug
              </DropdownMenuItem>
              <DropdownMenuItem icon={ArrowUpRight} onSelect={() => setPromoteOpen(true)}>
                Promover configuração…
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                icon={Trash}
                destructive
                disabled={agent.is_seed}
                onSelect={handleDelete}
                hint={agent.is_seed ? "sistema" : undefined}
              >
                Excluir agente
              </DropdownMenuItem>
            </DropdownMenu>
          </>
        }
        tabs={
          <TabsList className="shadow-none">
            <TabsTrigger value="config">Configuração</TabsTrigger>
            <TabsTrigger value="versions" count={versions.length}>
              Versões
            </TabsTrigger>
            {agent.kind !== "analysis" && <TabsTrigger value="feedback">Aprendizado</TabsTrigger>}
            <TabsTrigger value="runs">Execuções</TabsTrigger>
            {agent.kind !== "analysis" && (
              <TabsTrigger value="conversations" count={agentSessions.length}>
                Conversas
              </TabsTrigger>
            )}
            <TabsTrigger value="integration">Integração</TabsTrigger>
          </TabsList>
        }
      />

      <PageBody>
        <TabsContent value="config" forceMount>
          <AgentForm
            key={draft.key}
            mode="edit"
            agent={agent}
            initialInstructions={draft.instructions}
            availableTools={availableTools}
            onSubmit={handleSave}
          />
        </TabsContent>

        <TabsContent value="versions" className="flex flex-col gap-8">
          <RevisionsPanel agentType={agent.agent_type} />
          <div className="flex flex-col gap-3">
            <SectionHeading
              title="Prompt"
              description="Só as instruções, com comparação entre versões e restauração no editor."
            />
            <VersionHistory
              versions={versions}
              currentVersion={agent.prompt_version}
              currentInstructions={agent.instructions}
              onRestore={handleRestore}
            />
          </div>
        </TabsContent>

        <TabsContent value="feedback">
          {agent.kind === "analysis" ? (
            <p className="text-[13px] text-muted-foreground">
              A nota de feedback só orienta agentes conversacionais — num analista ela nunca seria aplicada. Ajuste as
              instructions.
            </p>
          ) : (
            <FeedbackPanel agentType={agent.agent_type} />
          )}
        </TabsContent>

        <TabsContent value="runs" className="flex flex-col gap-8">
          {agent.kind === "analysis" && <AgreementPanel agentType={agent.agent_type} />}
          {agent.kind === "procedural" && <ProcedureFunnelPanel agentType={agent.agent_type} />}
          <AgentRuns agentType={agent.agent_type} versions={versions} />
        </TabsContent>

        <TabsContent value="conversations">
          <AgentConversations agent={agent} />
        </TabsContent>

        <TabsContent value="integration" className="flex flex-col gap-4">
          <SectionHeading
            title="Integrar este agente"
            description="Outros módulos da plataforma chamam o agent-service diretamente com o slug deste agente."
          />
          <IntegrationPanel agentType={agent.agent_type} showReference />
        </TabsContent>
      </PageBody>
      <PromoteDialog agentType={agent.agent_type} open={promoteOpen} onOpenChange={setPromoteOpen} />
    </Tabs>
  );
}

function AgentConversations({ agent }: { agent: AgentDefinition }) {
  const { sessions, sessionsLoading } = useWorkspace();
  const items = sessions.filter((s) => s.agent_id === agent.agent_type);
  const newChatHref = `/chat?agent=${encodeURIComponent(agent.agent_type)}`;

  if (sessionsLoading) {
    return (
      <Card className="flex flex-col gap-3 p-4">
        {[1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-10" />
        ))}
      </Card>
    );
  }

  if (items.length === 0) {
    return (
      <Card>
        <EmptyState
          icon={MessagesSquare}
          title="Nenhuma conversa com este agente"
          description="As conversas que você iniciar no playground com ele aparecem aqui."
          action={
            <Link href={newChatHref} className={buttonVariants({ variant: "outline", size: "sm" })}>
              <MessageSquare /> Iniciar conversa
            </Link>
          }
        />
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <SectionHeading
        title={`${items.length} ${items.length === 1 ? "conversa" : "conversas"}`}
        description="Conversas deste navegador (seu user_id) com o agente."
        action={
          <Link href={newChatHref} className={buttonVariants({ variant: "outline", size: "sm" })}>
            <MessageSquare /> Nova conversa
          </Link>
        }
      />
      <Card className="divide-y divide-border overflow-hidden">
        {items.map((session) => (
          <Link
            key={session.session_id}
            href={`/chat/${encodeURIComponent(session.session_id)}`}
            className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-accent/50"
          >
            <MessageSquare className="size-4 shrink-0 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{sessionTitle(session)}</p>
              <p className="truncate font-mono text-xs text-muted-foreground">{session.session_id}</p>
            </div>
            {session.total_tokens ? (
              <span className="hidden text-xs tabular-nums text-muted-foreground sm:inline">
                {formatNumber(session.total_tokens)} tokens
              </span>
            ) : null}
            <RelativeTime
              date={session.updated_at ?? session.created_at}
              className="shrink-0 text-xs text-muted-foreground"
            />
          </Link>
        ))}
      </Card>
    </div>
  );
}
