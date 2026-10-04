"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Ellipsis, Play, Plus, Search, Wrench } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import { pluralize } from "@/lib/format";
import { TOOL_KIND_META } from "@/lib/agent-meta";
import type { ToolSummary } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DropdownMenu, DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { EmptyState, RelativeTime } from "@/components/ui/primitives";
import { useConfirm } from "@/components/ui/confirm";
import { useToast } from "@/components/ui/toast";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { ToolEditorDialog } from "@/components/tools/tool-editor-dialog";
import { ToolInvokeDialog } from "@/components/tools/tool-invoke-dialog";

export function ToolsIndex() {
  const router = useRouter();
  const { availableTools, backendReachable } = useWorkspace();
  const toast = useToast();
  const confirm = useConfirm();

  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<ToolSummary | "new" | null>(null);
  const [invoking, setInvoking] = useState<ToolSummary | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    // As do negócio (API, Python) antes das toolkits padrão; a ordem de dentro é a do servidor.
    const ordered = [...availableTools].sort((a, b) => Number(a.kind === "builtin") - Number(b.kind === "builtin"));
    if (!q) return ordered;
    return ordered.filter((t) => `${t.tool_name} ${t.label} ${t.description ?? ""}`.toLowerCase().includes(q));
  }, [availableTools, query]);

  function refresh() {
    router.refresh();
  }

  async function handleDelete(tool: ToolSummary) {
    const confirmed = await confirm({
      title: `Excluir "${tool.label}"?`,
      description: "Agentes que usam esta tool deixam de poder ser salvos com ela referenciada.",
      confirmLabel: "Excluir tool",
      destructive: true,
    });
    if (!confirmed) return;
    try {
      await requestJson(`/api/tools/${encodeURIComponent(tool.tool_name)}`, {
        method: "DELETE",
        fallbackError: "Falha ao excluir a tool",
      });
      toast({ title: "Tool excluída", variant: "success" });
      refresh();
    } catch (err) {
      toast({ title: "Não foi possível excluir", description: errorMessage(err), variant: "error" });
    }
  }

  const newToolButton = (
    <Button onClick={() => setEditing("new")}>
      <Plus /> Nova tool
    </Button>
  );

  return (
    <>
      <PageHeader
        title="Tools"
        description="Funções que os agentes podem chamar: toolkits padrão do Agno, chamadas de API descritas em JSON ou funções Python — todas criadas aqui, sem editar código."
        actions={backendReachable && availableTools.length > 0 ? newToolButton : undefined}
      />
      <PageBody>
        {!backendReachable ? (
          <Card>
            <EmptyState
              icon={Wrench}
              title="agent-service indisponível"
              description="Não foi possível carregar as tools. Verifique se o backend está rodando e recarregue a página."
            />
          </Card>
        ) : availableTools.length === 0 ? (
          <Card>
            <EmptyState
              icon={Wrench}
              title="Nenhuma tool ainda"
              description="Crie a primeira: uma toolkit padrão do Agno, uma chamada de API ou uma função Python."
              action={newToolButton}
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
                  placeholder="Buscar por nome ou descrição"
                  aria-label="Buscar tools"
                  className="pl-9"
                />
              </div>
              <p className="ml-auto text-[13px] text-muted-foreground">{pluralize(filtered.length, "tool", "tools")}</p>
            </div>

            {filtered.length === 0 ? (
              <Card>
                <EmptyState icon={Search} title="Nenhuma tool encontrada" description={`Nada corresponde a “${query}”.`} />
              </Card>
            ) : (
              <Card className="divide-y divide-border overflow-hidden">
                {filtered.map((tool) => (
                  <div key={tool.tool_name} className="flex items-center gap-3 px-4 py-3">
                    <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() => setEditing(tool)}
                          className="truncate text-left text-sm font-medium hover:underline"
                        >
                          {tool.label}
                        </button>
                        <Badge variant="outline">{TOOL_KIND_META[tool.kind].label}</Badge>
                        {tool.is_seed && <Badge variant="secondary">sistema</Badge>}
                        {!tool.enabled && <Badge variant="warning">desativada</Badge>}
                        {tool.side_effect === true && <Badge variant="outline">efeito colateral</Badge>}
                        {(tool.side_effect === null || tool.side_effect === undefined) && (
                          <Badge variant="warning" title="Edite a tool e diga se ela grava, cobra ou envia algo — a fila de revisão dos Logs usa isso.">
                            efeito colateral?
                          </Badge>
                        )}
                      </div>
                      <p className="truncate font-mono text-xs text-muted-foreground">
                        {tool.tool_name}
                        {tool.description ? ` — ${tool.description}` : ""}
                      </p>
                    </div>
                    <span className="hidden shrink-0 text-xs text-muted-foreground sm:inline">
                      <RelativeTime date={tool.updated_at} />
                    </span>
                    <Button variant="ghost" size="icon-sm" onClick={() => setInvoking(tool)} aria-label="Testar tool" title="Testar">
                      <Play />
                    </Button>
                    <DropdownMenu
                      align="end"
                      trigger={(props) => (
                        <Button {...props} variant="ghost" size="icon-sm" aria-label="Mais ações">
                          <Ellipsis />
                        </Button>
                      )}
                    >
                      <DropdownMenuItem onSelect={() => setEditing(tool)}>Editar</DropdownMenuItem>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        destructive
                        disabled={tool.is_seed}
                        hint={tool.is_seed ? "sistema" : undefined}
                        onSelect={() => void handleDelete(tool)}
                      >
                        Excluir
                      </DropdownMenuItem>
                    </DropdownMenu>
                  </div>
                ))}
              </Card>
            )}
          </div>
        )}
      </PageBody>

      {editing && (
        <ToolEditorDialog
          tool={editing === "new" ? null : editing}
          existingNames={availableTools.map((t) => t.tool_name)}
          onOpenChange={(open) => !open && setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
      {invoking && <ToolInvokeDialog tool={invoking} onOpenChange={(open) => !open && setInvoking(null)} />}
    </>
  );
}
