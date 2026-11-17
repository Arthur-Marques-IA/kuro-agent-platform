"use client";

import { useState } from "react";
import { CircleCheck, CircleX, ExternalLink } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import type {
  ModelCredential,
  ModelCredentialTestResult,
  ModelProviderSummary,
} from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Field, Spinner } from "@/components/ui/primitives";
import { Select } from "@/components/ui/select";
import { useToast } from "@/components/ui/toast";

/**
 * Cria ou edita uma credencial. Em criação, o provedor é escolhido aqui —
 * pode ser o mesmo de outra credencial já existente (times/clientes
 * diferentes, cada um com sua chave). Em edição, o provedor é fixo.
 */
export function CredentialDialog({
  providers,
  credential,
  initialProvider,
  onOpenChange,
  onSaved,
}: {
  providers: ModelProviderSummary[];
  /** `undefined` = criar; uma credencial existente = editar. */
  credential?: ModelCredential;
  /** Provedor pré-selecionado ao criar (ex.: clicou em "+ Chave" dentro da seção de um provedor). */
  initialProvider?: string;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const isEdit = Boolean(credential);
  const [providerName, setProviderName] = useState(credential?.provider ?? initialProvider ?? providers[0]?.provider ?? "");
  const provider = providers.find((p) => p.provider === providerName);
  const [label, setLabel] = useState(credential?.label ?? "");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(credential?.base_url ?? "");
  const [enabled, setEnabled] = useState(credential?.enabled ?? true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ModelCredentialTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const requiresApiKey = provider?.requires_api_key ?? true;
  const canTest = requiresApiKey ? Boolean(apiKey || credential?.configured) : true;

  async function handleTest() {
    setTesting(true);
    setTestResult(null);
    setError(null);
    try {
      const body = {
        ...(apiKey ? { api_key: apiKey } : {}),
        ...(provider?.supports_custom_base_url && baseUrl ? { base_url: baseUrl } : {}),
      };
      const result = isEdit
        ? await requestJson<ModelCredentialTestResult>(`/api/model-credentials/${encodeURIComponent(credential!.id)}/test`, {
            method: "POST",
            json: body,
            fallbackError: "Falha ao testar",
          })
        : await requestJson<ModelCredentialTestResult>("/api/model-credentials/test", {
            method: "POST",
            json: { provider: providerName, ...body },
            fallbackError: "Falha ao testar",
          });
      setTestResult(result);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setTesting(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      if (isEdit) {
        await requestJson(`/api/model-credentials/${encodeURIComponent(credential!.id)}`, {
          method: "PUT",
          json: {
            label,
            enabled,
            ...(apiKey ? { api_key: apiKey } : {}),
            ...(provider?.supports_custom_base_url ? { base_url: baseUrl } : {}),
          },
          fallbackError: "Falha ao salvar",
        });
      } else {
        await requestJson("/api/model-credentials", {
          method: "POST",
          json: {
            provider: providerName,
            label: label.trim() || provider?.label || providerName,
            enabled,
            ...(apiKey ? { api_key: apiKey } : {}),
            ...(provider?.supports_custom_base_url && baseUrl ? { base_url: baseUrl } : {}),
          },
          fallbackError: "Falha ao criar a chave",
        });
      }
      toast({ title: isEdit ? "Chave atualizada" : "Chave criada", variant: "success" });
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onOpenChange={onOpenChange} size="sm">
      <DialogHeader>
        <DialogTitle>{isEdit ? `Editar ${credential!.label}` : "Nova chave de API"}</DialogTitle>
        <DialogDescription>
          {requiresApiKey
            ? "A chave é cifrada em repouso e nunca volta numa resposta da API."
            : "Não exige chave — só o endereço do servidor."}{" "}
          {provider && (
            <a
              href={provider.docs_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-0.5 text-primary hover:underline"
            >
              Onde conseguir <ExternalLink className="size-3" />
            </a>
          )}
        </DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-4">
        {!isEdit && (
          <Field label="Provedor" htmlFor="credential-provider">
            <Select
              id="credential-provider"
              value={providerName}
              onChange={(e) => {
                setProviderName(e.target.value);
                setTestResult(null);
              }}
              data-autofocus
            >
              {providers.map((p) => (
                <option key={p.provider} value={p.provider}>
                  {p.label}
                </option>
              ))}
            </Select>
          </Field>
        )}
        <Field
          label="Nome da chave"
          htmlFor="credential-label"
          hint='Pra reconhecer entre várias do mesmo provedor — ex.: "Cliente A", "Time de suporte".'
        >
          <Input
            id="credential-label"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder={provider ? `ex.: ${provider.label} — produção` : "ex.: Cliente A"}
            data-autofocus={isEdit || undefined}
          />
        </Field>
        {requiresApiKey && (
          <Field
            label="Chave de API"
            htmlFor="credential-api-key"
            hint={credential?.configured ? `Chave salva termina em ${credential.key_hint}. Deixe em branco para manter.` : undefined}
          >
            <Input
              id="credential-api-key"
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={credential?.configured ? "••••••••••••" : "sk-..."}
            />
          </Field>
        )}
        {provider?.supports_custom_base_url && (
          <Field label="Endereço customizado" htmlFor="credential-base-url" hint="Opcional — para um endpoint próprio ou self-hosted.">
            <Input
              id="credential-base-url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={providerName === "ollama" ? "http://localhost:11434" : "https://..."}
            />
          </Field>
        )}
        <label className="flex w-fit cursor-pointer items-center gap-2 text-[13px]">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="accent-primary" />
          Habilitada (pode ser atribuída a agentes)
        </label>

        <div className="flex items-center gap-2 border-t border-border pt-4">
          <Button type="button" variant="outline" size="sm" onClick={() => void handleTest()} disabled={testing || !canTest}>
            {testing && <Spinner />}
            Testar
          </Button>
          {testResult && (
            <span className={`inline-flex items-center gap-1 text-xs ${testResult.ok ? "text-success" : "text-destructive"}`}>
              {testResult.ok ? <CircleCheck className="size-3.5" /> : <CircleX className="size-3.5" />}
              {testResult.ok ? "Chave válida" : testResult.message}
            </span>
          )}
        </div>
        {error && (
          // role="alert": num diálogo modal, a região viva de fora pode ser ignorada pelo leitor de tela.
          <Badge role="alert" variant="destructive" className="h-auto w-fit whitespace-normal px-2 py-1 text-left">
            {error}
          </Badge>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)}>
          Cancelar
        </Button>
        <Button onClick={() => void handleSave()} disabled={saving || (!isEdit && !label.trim())}>
          {saving && <Spinner />}
          Salvar
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
