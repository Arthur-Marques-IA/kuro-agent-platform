"use client";

import { FormEvent, useCallback, useEffect, useId, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowUpRight, Layers, RefreshCw } from "lucide-react";
import { cn } from "@/lib/cn";
import { errorMessage, requestJson } from "@/lib/http";
import { formatDateTime } from "@/lib/format";
import type { AgentRevision, PromoteResult } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { CodeBlock } from "@/components/ui/code-block";
import { Dialog, DialogBody, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { EmptyState, Field, RelativeTime, SectionHeading, Skeleton, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/toast";

/**
 * Versões da configuração inteira (`GET /agents/{t}/revisions`): o que muda o
 * comportamento — prompt, modelo e parâmetros, tools, schema, regras de
 * feedback. É o `agent_version` que cada execução grava, então é o número que
 * responde "com que configuração esta decisão foi tomada?".
 */
export function RevisionsPanel({ agentType }: { agentType: string }) {
  const [revisions, setRevisions] = useState<AgentRevision[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [promoteOpen, setPromoteOpen] = useState(false);
  const slug = encodeURIComponent(agentType);

  const [reloadKey, setReloadKey] = useState(0);
  const load = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    let cancelled = false;
    requestJson<AgentRevision[]>(`/api/agents/${slug}/revisions`, { fallbackError: "Falha ao carregar versões" })
      .then((result) => {
        if (cancelled) return;
        setRevisions(result);
        setError(null);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [slug, reloadKey]);

  const heading = (
    <SectionHeading
      title="Configuração completa"
      description="Cada mudança em prompt, modelo, parâmetros, tools, schema ou regras vira uma versão — é o agent_version de cada execução."
      action={
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={load}>
            <RefreshCw /> Atualizar
          </Button>
          <Button size="sm" onClick={() => setPromoteOpen(true)}>
            <ArrowUpRight /> Promover
          </Button>
        </div>
      }
    />
  );

  const dialog = (
    <PromoteDialog agentType={agentType} open={promoteOpen} onOpenChange={setPromoteOpen} onDone={load} />
  );

  if (error) {
    return (
      <div className="flex flex-col gap-3">
        {heading}
        <Card className="p-4 text-[13px] text-destructive">{error}</Card>
        {dialog}
      </div>
    );
  }
  if (revisions === null) {
    return (
      <div className="flex flex-col gap-3">
        {heading}
        <Skeleton className="h-24" />
      </div>
    );
  }
  if (revisions.length === 0) {
    return (
      <div className="flex flex-col gap-3">
        {heading}
        <Card>
          <EmptyState icon={Layers} title="Sem versões ainda" description="A primeira aparece na primeira execução." />
        </Card>
        {dialog}
      </div>
    );
  }

  const current = revisions.find((r) => r.version === selected) ?? revisions.find((r) => r.current) ?? revisions[0];

  return (
    <div className="flex flex-col gap-3">
      {heading}
      <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
        <Card className="self-start p-1.5">
          <ol className="flex flex-col gap-0.5" aria-label="Versões da configuração">
            {revisions.map((revision) => {
              const active = revision.version === current.version;
              const c = revision.config;
              return (
                <li key={revision.version}>
                  <button
                    type="button"
                    onClick={() => setSelected(revision.version)}
                    aria-current={active}
                    className={cn(
                      "flex w-full flex-col gap-0.5 rounded-md px-3 py-2 text-left transition-colors",
                      active ? "bg-accent" : "hover:bg-accent/50"
                    )}
                  >
                    <span className="flex items-center gap-2 text-[13px] font-medium">
                      v{revision.version}
                      {revision.current && <Badge variant="success">em uso</Badge>}
                      <span className="ml-auto font-mono text-[11px] text-muted-foreground">{revision.config_hash}</span>
                    </span>
                    <span className="truncate text-xs text-muted-foreground">
                      {c.model_id ?? "modelo padrão"}
                      {c.model_params?.temperature !== undefined && ` · temp ${c.model_params.temperature}`}
                      {c.tools?.length ? ` · ${c.tools.length} tool(s)` : ""}
                    </span>
                    <RelativeTime date={revision.created_at} className="text-[11px] text-muted-foreground" />
                  </button>
                </li>
              );
            })}
          </ol>
        </Card>
        <div className="flex min-w-0 flex-col gap-3">
          <RevisionSummary revision={current} />
          <CodeBlock
            title={`configuração v${current.version} · ${formatDateTime(current.created_at)}`}
            code={JSON.stringify(current.config, null, 2)}
          />
        </div>
      </div>
      {dialog}
    </div>
  );
}

function RevisionSummary({ revision }: { revision: AgentRevision }) {
  const c = revision.config;
  const params = Object.entries(c.model_params ?? {});
  const items: [string, string][] = [
    ["Modelo", `${c.model_provider ?? "—"} / ${c.model_id ?? "—"}`],
    ["Parâmetros", params.length ? params.map(([k, v]) => `${k}=${v}`).join(", ") : "padrão do provedor"],
    ["Tempo limite", c.timeout_seconds ? `${c.timeout_seconds}s` : "padrão do serviço"],
    ["Tools no histórico", c.history_tool_calls != null ? `as ${c.history_tool_calls} mais recentes` : "todas"],
    ["Instruções", `${c.instructions?.length ?? 0}`],
    ["Tools", c.tools?.map((t) => t.tool_name).join(", ") || "—"],
    ["Campos da saída", `${c.response_schema?.length ?? 0}`],
    ["Regras de feedback", `${c.feedback_rules?.length ?? 0}`],
    ["Base de conhecimento", c.knowledge_collection ?? "—"],
  ];
  return (
    <Card className="grid gap-x-6 gap-y-2 p-4 text-[13px] sm:grid-cols-2">
      {items.map(([label, value]) => (
        <div key={label} className="flex min-w-0 justify-between gap-3">
          <span className="text-muted-foreground">{label}</span>
          <span className="truncate text-right font-medium" title={value}>
            {value}
          </span>
        </div>
      ))}
    </Card>
  );
}

/**
 * Promover = copiar a configuração deste agente para outro (`POST /agents/{t}/promote`).
 * É o fluxo draft → prod: quem integra chama sempre `r8`; as mudanças vão em
 * `r8-draft`, são avaliadas com `kuro eval` e só então promovidas. O nome do
 * destino fica; se ele não existir, é criado.
 */
export function PromoteDialog({
  agentType,
  open,
  onOpenChange,
  onDone,
}: {
  agentType: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone?: () => void;
}) {
  const id = useId();
  const router = useRouter();
  const toast = useToast();
  const suggested = agentType.endsWith("-draft") ? agentType.slice(0, -"-draft".length) : `${agentType}-draft`;
  const [target, setTarget] = useState(suggested);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!target.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await requestJson<PromoteResult>(`/api/agents/${encodeURIComponent(agentType)}/promote`, {
        method: "POST",
        json: { to: target.trim() },
        fallbackError: "Falha ao promover",
      });
      toast({
        title: result.unchanged ? "Nada a promover" : `${target} atualizado`,
        description: result.unchanged
          ? `${target} já está na mesma configuração (v${result.agent_version}).`
          : `${result.previous_version ? `v${result.previous_version}` : "novo"} → v${result.agent_version}, com a configuração de ${agentType} v${result.source_version}.`,
        variant: "success",
      });
      onOpenChange(false);
      onDone?.();
      router.refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange} size="sm">
      <form onSubmit={submit} noValidate>
        <DialogHeader>
          <DialogTitle>Promover configuração</DialogTitle>
          <DialogDescription>
            Copia prompt, modelo, parâmetros, tools, schema, dependências e regras de <b>{agentType}</b> para o agente
            de destino. Rode o <code>kuro eval</code> antes de promover para produção.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field label="Agente de destino" htmlFor={`${id}-target`} hint="Criado se não existir. O nome dele não muda." error={error}>
            <Input
              id={`${id}-target`}
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              className="font-mono"
              data-autofocus
            />
          </Field>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancelar
          </Button>
          <Button type="submit" disabled={submitting || !target.trim()}>
            {submitting && <Spinner />}
            Promover para {target || "…"}
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
