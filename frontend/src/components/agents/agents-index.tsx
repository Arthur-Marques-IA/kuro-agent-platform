"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { Bot, CircleAlert, MessageSquare, Plus, Search } from "lucide-react";
import { MEMORY_BACKENDS, modelLabel } from "@/lib/agent-meta";
import { pluralize } from "@/lib/format";
import type { AgentDefinition } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardFooter } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { EmptyState, RelativeTime } from "@/components/ui/primitives";
import { AgentAvatar } from "@/components/agents/agent-avatar";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { useOverview } from "@/components/observability/overview-panel";

const OVERVIEW_PARAMS = { include_dry_run: "true" };

export function AgentsIndex() {
  const { agents, sessions, backendReachable } = useWorkspace();
  const [query, setQuery] = useState("");

  // `sessions` são só as conversas deste navegador; o tráfego da API, da CLI e do MCP
  // (ex. /analyze com session_id) só aparece no trace store. Sem `since`, o panorama
  // cobre desde o primeiro run. Enquanto carrega, vale a contagem local.
  const overview = useOverview(OVERVIEW_PARAMS);
  const conversationCounts = useMemo(() => {
    const counts = new Map<string, number>();
    if (overview.data) {
      for (const a of overview.data.agents) counts.set(a.agent_type, a.totals.sessions);
      return counts;
    }
    for (const s of sessions) if (s.agent_id) counts.set(s.agent_id, (counts.get(s.agent_id) ?? 0) + 1);
    return counts;
  }, [overview.data, sessions]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return agents;
    return agents.filter((a) =>
      `${a.name} ${a.agent_type} ${a.instructions.join(" ")}`.toLowerCase().includes(q)
    );
  }, [agents, query]);

  const newAgentLink = (
    <Link href="/agents/new" className={buttonVariants()}>
      <Plus /> Novo agente
    </Link>
  );

  return (
    <>
      <PageHeader
        title="Agentes"
        description="Crie e ajuste agentes sem deploy: prompt versionado, modelo, memória e tools — disponíveis na API assim que salvos."
        actions={backendReachable && agents.length > 0 ? newAgentLink : undefined}
      />
      <PageBody>
        {!backendReachable ? (
          <Card>
            <EmptyState
              icon={CircleAlert}
              title="agent-service indisponível"
              description="Não foi possível carregar os agentes. Verifique se o backend está rodando e recarregue a página."
            />
          </Card>
        ) : agents.length === 0 ? (
          <Card>
            <EmptyState
              icon={Bot}
              title="Nenhum agente ainda"
              description="Crie o primeiro agente definindo um nome e as instruções do prompt."
              action={newAgentLink}
            />
          </Card>
        ) : (
          <div className="flex flex-col gap-5">
            <div className="flex flex-wrap items-center gap-3">
              <div className="relative w-full max-w-xs">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Buscar por nome, slug ou prompt"
                  aria-label="Buscar agentes"
                  className="pl-9"
                />
              </div>
              <p className="ml-auto text-[13px] text-muted-foreground">
                {pluralize(filtered.length, "agente", "agentes")}
              </p>
            </div>

            {filtered.length === 0 ? (
              <Card>
                <EmptyState icon={Search} title="Nenhum agente encontrado" description={`Nada corresponde a “${query}”.`} />
              </Card>
            ) : (
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {filtered.map((agent) => (
                  <AgentCard
                    key={agent.agent_type}
                    agent={agent}
                    conversations={conversationCounts.get(agent.agent_type) ?? 0}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </PageBody>
    </>
  );
}

function AgentCard({ agent, conversations }: { agent: AgentDefinition; conversations: number }) {
  const slug = encodeURIComponent(agent.agent_type);
  return (
    <Card className="group relative flex flex-col transition-[border-color,box-shadow] hover:border-ring/40 hover:shadow-md">
      <div className="flex items-start gap-3 p-5 pb-3">
        <AgentAvatar agentType={agent.agent_type} name={agent.name} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold">
            <Link
              href={`/agents/${slug}`}
              className="outline-none after:absolute after:inset-0 after:rounded-xl focus-visible:after:ring-2 focus-visible:after:ring-ring"
            >
              {agent.name}
            </Link>
          </h3>
          <p className="truncate font-mono text-xs text-muted-foreground">{agent.agent_type}</p>
        </div>
        {agent.is_seed && <Badge variant="secondary">sistema</Badge>}
      </div>
      <p className="line-clamp-2 min-h-10 px-5 text-[13px] text-muted-foreground">{agent.instructions.join(" ")}</p>
      <div className="flex flex-wrap gap-1.5 px-5 pb-4 pt-3">
        <Badge variant="outline">{modelLabel(agent.model_id)}</Badge>
        <Badge variant="outline">{MEMORY_BACKENDS[agent.memory_backend]?.label ?? agent.memory_backend}</Badge>
        <Badge variant="outline">v{agent.prompt_version}</Badge>
      </div>
      <CardFooter className="mt-auto justify-between gap-2">
        <span className="min-w-0 truncate text-xs text-muted-foreground">
          {pluralize(conversations, "conversa", "conversas")} · <RelativeTime date={agent.updated_at} />
        </span>
        <Link
          href={`/chat?agent=${slug}`}
          className={buttonVariants({ variant: "ghost", size: "sm", className: "relative z-10 -mr-2" })}
        >
          <MessageSquare /> Conversar
        </Link>
      </CardFooter>
    </Card>
  );
}
