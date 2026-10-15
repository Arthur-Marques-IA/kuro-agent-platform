"use client";

import { DryRunToggle } from "@/components/tools/dry-run-toggle";
import { ReactNode, useId } from "react";
import Link from "next/link";
import { Activity, CodeXml, ExternalLink, Pencil, Users, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";
import { MEMORY_BACKENDS, modelLabel } from "@/lib/agent-meta";
import type { AgentDefinition, ProcedureState } from "@/lib/types";
import { Button, buttonVariants } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { DependenciesForm } from "@/components/schema/dependencies-form";
import { AgentAvatar } from "@/components/agents/agent-avatar";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { ProcedureSteps } from "@/components/chat/procedure-progress";

const DEPENDENCIES_EXAMPLE = JSON.stringify({ nome: "Maria", cpf: "000.000.000-00", plano: "premium" }, null, 2);
const NO_FIELDS: never[] = [];

export function ChatInspector({
  agent,
  agentType,
  sessionId,
  userId,
  messageCount,
  totalTokens,
  dependenciesText,
  presetText,
  onDependenciesChange,
  onDependenciesReset,
  dependenciesError,
  dependenciesCount,
  dryRun,
  onDryRunChange,
  procedureState,
  onEditAgent,
  onShowCode,
  onClose,
}: {
  agent?: AgentDefinition;
  agentType: string;
  sessionId: string | null;
  userId: string;
  messageCount: number;
  totalTokens: number;
  dependenciesText: string;
  /** Valores fictícios de teste gerados dos campos declarados; `""` se o agente não declara nenhum. */
  presetText: string;
  onDependenciesChange: (value: string) => void;
  onDependenciesReset: () => void;
  dependenciesError: string | null;
  dependenciesCount: number;
  dryRun: boolean;
  onDryRunChange: (value: boolean) => void;
  /** Só em agente procedural: o estado da conversa (`null` antes da 1ª resposta). */
  procedureState?: ProcedureState | null;
  onEditAgent: () => void;
  onShowCode: () => void;
  onClose: () => void;
}) {
  const id = useId();
  const slug = encodeURIComponent(agentType);
  const { observability, modelCredentials } = useWorkspace();
  const pinnedCredential = agent?.model_credential_id
    ? modelCredentials.find((c) => c.id === agent.model_credential_id)
    : undefined;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
        <h2 className="text-sm font-semibold">Detalhes da conversa</h2>
        <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Fechar painel">
          <X />
        </Button>
      </div>

      <div className="scrollbar-thin flex-1 divide-y divide-border overflow-y-auto">
        <InspectorSection
          title="Agente"
          action={
            agent && (
              <Button variant="ghost" size="sm" onClick={onEditAgent} className="-mr-2">
                <Pencil /> Editar
              </Button>
            )
          }
        >
          {agent ? (
            <>
              <div className="flex items-center gap-3">
                <AgentAvatar agentType={agent.agent_type} name={agent.name} size="sm" />
                <div className="min-w-0 flex-1">
                  <Link href={`/agents/${slug}`} className="block truncate text-sm font-medium hover:underline">
                    {agent.name}
                  </Link>
                  <p className="truncate font-mono text-xs text-muted-foreground">{agent.agent_type}</p>
                </div>
              </div>
              <dl className="mt-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-[13px]">
                <Detail label="Modelo">{modelLabel(agent.model_id)}</Detail>
                {pinnedCredential && (
                  <Detail label="Chave">
                    <Link href={`/models`} className="hover:underline">
                      {pinnedCredential.label}
                    </Link>
                  </Detail>
                )}
                <Detail label="Memória">{MEMORY_BACKENDS[agent.memory_backend]?.label ?? agent.memory_backend}</Detail>
                <Detail label="Prompt">
                  <Link href={`/agents/${slug}?tab=versions`} className="hover:underline">
                    v{agent.prompt_version}
                  </Link>
                </Detail>
                <Detail label="Histórico">{agent.num_history_runs} execuções</Detail>
                <Detail label="Tools">{agent.tools.length > 0 ? agent.tools.join(", ") : "nenhuma"}</Detail>
              </dl>
            </>
          ) : (
            <p className="text-[13px] text-muted-foreground">
              O agente <code className="font-mono text-xs">{agentType}</code> não foi encontrado.
            </p>
          )}
        </InspectorSection>

        {agent?.kind === "procedural" && (
          <InspectorSection
            title="Procedimento"
            description="As etapas são conduzidas pelo servidor: o modelo só extrai os dados e redige a resposta."
          >
            <ProcedureSteps state={procedureState ?? null} stages={agent.stages ?? []} />
            {procedureState?.done && (
              <p className="mt-3 rounded-md bg-success/10 px-3 py-2 text-xs text-success">
                Concluído. Quem integra recebe o resultado em <code className="font-mono">state.result</code>.
              </p>
            )}
          </InspectorSection>
        )}

        <InspectorSection title="Sessão">
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-1.5 text-[13px]">
            <Detail label="session_id">
              {sessionId ? (
                <CopyableId value={sessionId} />
              ) : (
                <span className="text-muted-foreground">gerado no 1º envio</span>
              )}
            </Detail>
            <Detail label="user_id">
              <CopyableId value={userId} />
            </Detail>
            <Detail label="Mensagens">{formatNumber(messageCount)}</Detail>
            <Detail label="Tokens">{formatNumber(totalTokens)}</Detail>
          </dl>
        </InspectorSection>

        <InspectorSection
          title="Contexto"
          description={
            <>
              Objeto <code className="font-mono">dependencies</code> enviado com cada mensagem — o agente o recebe
              como contexto estruturado (ex.: dados do cliente).
              {presetText && " Pré-preenchido com valores fictícios — não atingem clientes reais."}
            </>
          }
        >
          <DependenciesForm
            fields={agent?.dependency_fields ?? NO_FIELDS}
            value={dependenciesText}
            onChange={onDependenciesChange}
            invalid={Boolean(dependenciesError)}
            placeholder={DEPENDENCIES_EXAMPLE}
          />
          <div className="mt-2 flex items-start justify-between gap-2">
            <p
              id={`${id}-deps-status`}
              className={cn(
                "min-w-0 text-xs",
                dependenciesError ? "text-destructive" : dependenciesCount > 0 ? "text-success" : "text-muted-foreground"
              )}
            >
              {dependenciesError ??
                (dependenciesCount > 0
                  ? `${dependenciesCount} ${dependenciesCount === 1 ? "campo será enviado" : "campos serão enviados"}`
                  : "Vazio — nada é enviado.")}
            </p>
            <div className="flex shrink-0 flex-wrap justify-end gap-1">
              {presetText && dependenciesText !== presetText && (
                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={onDependenciesReset}>
                  Valores de teste
                </Button>
              )}
              {!presetText && !dependenciesText && (
                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => onDependenciesChange(DEPENDENCIES_EXAMPLE)}>
                  Exemplo
                </Button>
              )}
              {dependenciesText && (
                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => onDependenciesChange("")}>
                  Limpar
                </Button>
              )}
            </div>
          </div>
          <div className="mt-3">
            <DryRunToggle checked={dryRun} onChange={onDryRunChange} />
          </div>
        </InspectorSection>

        {observability.enabled && (
          <InspectorSection
            title="Logs"
            description="Cada resposta vira um trace — prompt, chamadas ao modelo, tools, tokens e custo."
          >
            <div className="flex flex-col gap-2">
              {sessionId ? (
                <Link
                  href={`/logs/sessions/${encodeURIComponent(sessionId)}`}
                  className={buttonVariants({ variant: "outline", size: "sm", className: "w-full" })}
                >
                  <Activity /> Execuções desta conversa
                </Link>
              ) : (
                <p className="text-[13px] text-muted-foreground">
                  As execuções aparecem aqui depois da primeira mensagem.
                </p>
              )}
              <Link
                href={`/logs/sessions?user_id=${encodeURIComponent(userId)}`}
                className="inline-flex items-center justify-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
              >
                <Users className="size-3" /> Meu histórico de uso
              </Link>
            </div>
          </InspectorSection>
        )}

        <InspectorSection title="Integração" description="Reproduza esta conversa via API em outro módulo.">
          <div className="flex flex-col gap-2">
            <Button variant="outline" size="sm" onClick={onShowCode} className="w-full">
              <CodeXml /> Ver código da chamada
            </Button>
            <Link
              href={`/agents/${slug}?tab=integration`}
              className="inline-flex items-center justify-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
            >
              Referência completa <ExternalLink className="size-3" />
            </Link>
          </div>
        </InspectorSection>
      </div>
    </div>
  );
}

function InspectorSection({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="px-4 py-4">
      <div className="mb-3 flex min-h-7 items-center justify-between gap-2">
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">{title}</h3>
        {action}
      </div>
      {description && <p className="-mt-2 mb-3 text-xs text-muted-foreground">{description}</p>}
      {children}
    </section>
  );
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-right">{children}</dd>
    </>
  );
}

function CopyableId({ value }: { value: string }) {
  return (
    <span className="flex min-w-0 items-center justify-end gap-0.5">
      <span className="truncate font-mono text-xs" title={value}>
        {value}
      </span>
      <CopyButton value={value} label="Copiar" className="size-6" />
    </span>
  );
}
