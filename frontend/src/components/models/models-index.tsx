"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { CircleCheck, CircleX, Cpu, Ellipsis, Info, Play, Plus, Settings2 } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import { MODEL_CREDENTIALS_TITLE } from "@/lib/model-credentials";
import type { ModelCredential } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DropdownMenu, DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { EmptyState, RelativeTime, SectionHeading, Spinner } from "@/components/ui/primitives";
import { useConfirm } from "@/components/ui/confirm";
import { useToast } from "@/components/ui/toast";
import { PageBody, PageHeader } from "@/components/workspace/page-header";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { CredentialDialog } from "@/components/models/credential-dialog";

/**
 * A chave que o serviço lê do próprio ambiente (ex.: `GOOGLE_API_KEY`). Somente
 * leitura: não dá para testar, editar nem excluir daqui, e o valor nunca chega
 * ao console — só o nome da variável.
 */
function EnvCredentialRow({ variable, inUse }: { variable: string; inUse: boolean }) {
  return (
    <Card className="overflow-hidden">
      <div className="flex items-center gap-3 px-4 py-3">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-sm font-medium">{variable}</span>
            <Badge variant="secondary">variável de ambiente</Badge>
            {inUse ? <Badge variant="success">em uso</Badge> : <Badge variant="outline">reserva</Badge>}
          </div>
          <p className="text-xs text-muted-foreground">
            Definida no ambiente do serviço (.env / docker compose); somente leitura aqui.{" "}
            {inUse
              ? "Nenhuma chave cadastrada resolve este provedor, então é esta que os agentes sem credencial fixa usam."
              : "Usada só quando nenhuma chave cadastrada habilitada resolve este provedor."}
          </p>
        </div>
      </div>
    </Card>
  );
}

export function ModelsIndex() {
  const router = useRouter();
  const { modelProviders, modelCredentials, backendReachable } = useWorkspace();
  const toast = useToast();
  const confirm = useConfirm();
  const [editing, setEditing] = useState<ModelCredential | { newForProvider: string } | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);

  function refresh() {
    router.refresh();
  }

  async function handleTest(credential: ModelCredential) {
    setTestingId(credential.id);
    try {
      const result = await requestJson<{ ok: boolean; message?: string | null }>(
        `/api/model-credentials/${encodeURIComponent(credential.id)}/test`,
        { method: "POST", json: {}, fallbackError: "Falha ao testar" }
      );
      toast({
        title: result.ok ? "Chave válida" : "Teste falhou",
        description: result.ok ? undefined : (result.message ?? undefined),
        variant: result.ok ? "success" : "error",
      });
      refresh();
    } catch (err) {
      toast({ title: "Não foi possível testar", description: errorMessage(err), variant: "error" });
    } finally {
      setTestingId(null);
    }
  }

  async function handleDelete(credential: ModelCredential) {
    const inUse = credential.agents_using.length > 0;
    const confirmed = await confirm({
      title: `Excluir "${credential.label}"?`,
      description: inUse
        ? `${credential.agents_using.join(", ")} ${credential.agents_using.length === 1 ? "usa" : "usam"} esta chave e voltam a usar a credencial padrão do provedor (ou param de funcionar, se não houver outra).`
        : "Nenhum agente usa esta chave hoje.",
      confirmLabel: "Excluir chave",
      destructive: true,
    });
    if (!confirmed) return;
    try {
      await requestJson(`/api/model-credentials/${encodeURIComponent(credential.id)}`, {
        method: "DELETE",
        fallbackError: "Falha ao excluir",
      });
      toast({ title: "Chave removida", variant: "success" });
      refresh();
    } catch (err) {
      toast({ title: "Não foi possível remover", description: errorMessage(err), variant: "error" });
    }
  }

  const newKeyButton = (
    <Button
      onClick={() => setEditing({ newForProvider: modelProviders[0]?.provider ?? "" })}
      disabled={modelProviders.length === 0}
    >
      <Plus /> Nova chave
    </Button>
  );

  // A credencial do ambiente (ex.: GOOGLE_API_KEY) também responde pelos agentes:
  // com ela, a tela não pode dizer que não há nada configurado.
  const hasEnvCredential = modelProviders.some((p) => p.env_credential);
  const hasAnything = modelCredentials.length > 0 || hasEnvCredential;

  return (
    <>
      <PageHeader
        title={MODEL_CREDENTIALS_TITLE}
        description="Chaves dos provedores de LLM (Google, OpenAI, Anthropic, Ollama) que os agentes usam para chamar o modelo. O mesmo provedor pode ter várias; na configuração de cada agente você define qual ele usa."
        actions={backendReachable && hasAnything ? newKeyButton : undefined}
      />
      <PageBody className="flex flex-col gap-6">
        <p className="flex items-start gap-2 rounded-lg border border-border bg-muted/40 px-4 py-3 text-[13px] text-muted-foreground">
          <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>
            Estas não são as chaves de acesso ao Kuro. Quem chama a API do serviço (console, CLI, outros sistemas) usa a{" "}
            <code className="font-mono text-xs">ADMIN_API_KEY</code> ou a{" "}
            <code className="font-mono text-xs">RUNTIME_API_KEY</code>, definidas no ambiente do serviço e que não aparecem aqui.
          </span>
        </p>
        {!backendReachable ? (
          <Card>
            <EmptyState
              icon={Cpu}
              title="agent-service indisponível"
              description="Não foi possível carregar as credenciais. Verifique se o backend está rodando e recarregue a página."
            />
          </Card>
        ) : !hasAnything ? (
          <Card>
            <EmptyState
              icon={Cpu}
              title="Nenhuma credencial de modelo ainda"
              description="Cadastre a primeira chave de um provedor (Google, OpenAI, Anthropic ou Ollama) para os agentes poderem usá-la."
              action={newKeyButton}
            />
          </Card>
        ) : (
          modelProviders.map((provider) => {
            const credentials = modelCredentials.filter((c) => c.provider === provider.provider);
            const env = provider.env_credential;
            const counts = [
              credentials.length > 0 && `${credentials.length} ${credentials.length === 1 ? "chave cadastrada" : "chaves cadastradas"}`,
              env && "variável de ambiente",
            ].filter(Boolean);
            return (
              <div key={provider.provider} className="flex flex-col gap-3">
                <SectionHeading
                  title={provider.label}
                  description={counts.length === 0 ? "Nenhuma chave cadastrada" : counts.join(" · ")}
                  action={
                    <Button variant="outline" size="sm" onClick={() => setEditing({ newForProvider: provider.provider })}>
                      <Plus /> Chave para {provider.label}
                    </Button>
                  }
                />
                {env && <EnvCredentialRow variable={env.variable} inUse={env.in_use} />}
                {credentials.length === 0 ? (
                  env ? null : <p className="rounded-lg border border-dashed border-border px-4 py-3 text-[13px] text-muted-foreground">
                    Nenhuma chave para {provider.label} ainda.{" "}
                    <button
                      type="button"
                      className="text-primary hover:underline"
                      onClick={() => setEditing({ newForProvider: provider.provider })}
                    >
                      Adicionar
                    </button>
                  </p>
                ) : (
                  <Card className="divide-y divide-border overflow-hidden">
                    {credentials.map((credential) => (
                      <div key={credential.id} className="flex items-center gap-3 px-4 py-3">
                        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="text-sm font-medium">{credential.label}</span>
                            {credential.configured ? (
                              <Badge variant="success">
                                <CircleCheck /> configurada
                              </Badge>
                            ) : (
                              <Badge variant="secondary">não configurada</Badge>
                            )}
                            {credential.configured && !credential.enabled && <Badge variant="warning">desabilitada</Badge>}
                            {credential.last_test_ok === true && (
                              <Badge variant="success">
                                <CircleCheck /> testada ok
                              </Badge>
                            )}
                            {credential.last_test_ok === false && (
                              <Badge variant="destructive" title={credential.last_test_message ?? undefined}>
                                <CircleX /> falhou no teste
                              </Badge>
                            )}
                          </div>
                          <p className="truncate text-xs text-muted-foreground">
                            {credential.key_hint ? `Chave termina em ${credential.key_hint}` : "Sem chave cadastrada"}
                            {credential.base_url ? ` · ${credential.base_url}` : ""}
                            {" · "}
                            {credential.agents_using.length === 0
                              ? "nenhum agente usa"
                              : `usada por ${credential.agents_using.join(", ")}`}
                            {credential.last_tested_at && (
                              <>
                                {" · testado "}
                                <RelativeTime date={credential.last_tested_at} />
                              </>
                            )}
                          </p>
                        </div>
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          onClick={() => void handleTest(credential)}
                          disabled={testingId === credential.id}
                          aria-label="Testar chave"
                          title="Testar"
                        >
                          {testingId === credential.id ? <Spinner /> : <Play />}
                        </Button>
                        <DropdownMenu
                          align="end"
                          trigger={(props) => (
                            <Button {...props} variant="ghost" size="icon-sm" aria-label="Mais ações">
                              <Ellipsis />
                            </Button>
                          )}
                        >
                          <DropdownMenuItem icon={Settings2} onSelect={() => setEditing(credential)}>
                            Editar
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem destructive onSelect={() => void handleDelete(credential)}>
                            Excluir
                          </DropdownMenuItem>
                        </DropdownMenu>
                      </div>
                    ))}
                  </Card>
                )}
              </div>
            );
          })
        )}
      </PageBody>

      {editing && (
        <CredentialDialog
          providers={modelProviders}
          credential={"newForProvider" in editing ? undefined : editing}
          initialProvider={"newForProvider" in editing ? editing.newForProvider : undefined}
          onOpenChange={(open) => !open && setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
    </>
  );
}
