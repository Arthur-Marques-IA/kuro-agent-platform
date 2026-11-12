"use client";

import { useRouter } from "next/navigation";
import { requestJson } from "@/lib/http";
import type { AgentDefinition } from "@/lib/types";
import { useToast } from "@/components/ui/toast";
import { AgentForm, type AgentFormPayload } from "@/components/agents/agent-form";
import { useReserveMascotSpace } from "@/components/brand/kuro-mascot";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";

export function AgentCreate() {
  const router = useRouter();
  const toast = useToast();
  const { availableTools } = useWorkspace();
  // A barra de criar fica fixa no rodapé, no canto do Kuro.
  useReserveMascotSpace();

  async function handleCreate(payload: AgentFormPayload) {
    const created = await requestJson<AgentDefinition>("/api/agents", {
      method: "POST",
      json: payload,
      fallbackError: "Falha ao criar agente",
    });
    toast({
      title: "Agente criado",
      description: `${created.name} já responde na API com agent_type "${created.agent_type}".`,
      variant: "success",
    });
    router.push(`/agents/${encodeURIComponent(created.agent_type)}`);
    router.refresh();
  }

  return (
    <>
      <PageHeader
        breadcrumbs={[{ label: "Agentes", href: "/agents" }, { label: "Novo agente" }]}
        title="Novo agente"
        description="Defina identidade, prompt e comportamento. Ele fica disponível no playground e na API assim que for criado."
      />
      <PageBody>
        <AgentForm
          mode="create"
          availableTools={availableTools}
          onSubmit={handleCreate}
          onCancel={() => router.push("/agents")}
        />
      </PageBody>
    </>
  );
}
