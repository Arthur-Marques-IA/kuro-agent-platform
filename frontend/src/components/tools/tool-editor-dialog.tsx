"use client";

import { useEffect, useState } from "react";
import { Plus, Trash } from "lucide-react";
import { cn } from "@/lib/cn";
import { errorMessage, requestJson } from "@/lib/http";
import { slugify } from "@/lib/format";
import { TOOL_KIND_META } from "@/lib/agent-meta";
import type {
  ApiAuth,
  ApiParamLocation,
  ApiParamSource,
  ApiParamType,
  ApiToolConfig,
  ApiToolParam,
  BuiltinCatalogEntry,
  ItemType,
  PythonToolConfig,
  ToolInput,
  ToolKind,
  ToolSummary,
  ToolUpdateInput,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, Spinner } from "@/components/ui/primitives";
import { Select } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { SchemaFieldsEditor, schemaProblem } from "@/components/schema/schema-fields-editor";

const SLUG_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;
const API_METHODS: ApiToolConfig["method"][] = ["GET", "POST", "PUT", "PATCH", "DELETE"];
const API_PARAM_TYPES: ApiParamType[] = ["string", "integer", "number", "boolean", "object", "array"];
const ITEM_TYPES: ItemType[] = ["string", "integer", "number", "boolean", "object"];
const API_PARAM_LOCATIONS: ApiParamLocation[] = ["query", "path", "header", "body"];
const PYTHON_TEMPLATE = `def handler(x: int) -> int:\n    """Descreva o que a tool faz — vira a descrição que o modelo vê."""\n    return x * 2\n`;

interface ApiDraft extends Omit<ApiToolConfig, "auth" | "headers"> {
  authType: ApiAuth["type"];
  authFields: Record<string, string>;
  headerRows: { key: string; value: string }[];
}

function emptyApiDraft(): ApiDraft {
  return { method: "GET", url: "", timeout_seconds: 15, parameters: [], authType: "none", authFields: {}, headerRows: [] };
}

function apiDraftFrom(config: Record<string, unknown>): ApiDraft {
  const c = config as unknown as ApiToolConfig;
  const auth = c.auth ?? { type: "none" as const };
  return {
    method: c.method ?? "GET",
    url: c.url ?? "",
    timeout_seconds: c.timeout_seconds ?? 15,
    parameters: c.parameters ?? [],
    authType: auth.type,
    authFields:
      auth.type === "bearer"
        ? { token: auth.token }
        : auth.type === "api_key"
          ? { header: auth.header, value: auth.value }
          : auth.type === "basic"
            ? { username: auth.username, password: auth.password }
            : {},
    headerRows: Object.entries(c.headers ?? {}).map(([key, value]) => ({ key, value })),
  };
}

function buildApiConfig(draft: ApiDraft): ApiToolConfig {
  let auth: ApiAuth = { type: "none" };
  if (draft.authType === "bearer") auth = { type: "bearer", token: draft.authFields.token ?? "" };
  else if (draft.authType === "api_key")
    auth = { type: "api_key", header: draft.authFields.header ?? "", value: draft.authFields.value ?? "" };
  else if (draft.authType === "basic")
    auth = { type: "basic", username: draft.authFields.username ?? "", password: draft.authFields.password ?? "" };

  const headers = Object.fromEntries(draft.headerRows.filter((h) => h.key.trim()).map((h) => [h.key.trim(), h.value]));

  return {
    method: draft.method,
    url: draft.url.trim(),
    timeout_seconds: draft.timeout_seconds,
    auth,
    headers,
    parameters: draft.parameters,
  };
}

function pythonDraftFrom(config: Record<string, unknown>): PythonToolConfig {
  const c = config as unknown as PythonToolConfig;
  return { code: c.code ?? PYTHON_TEMPLATE, entrypoint: c.entrypoint ?? "handler", timeout_seconds: c.timeout_seconds ?? 10 };
}

/**
 * Cria ou edita uma tool. O tipo (`kind`) só é escolhido na criação — depois
 * disso o formulário muda de forma (builtin/api/python), então trocar de tipo
 * numa tool existente é "exclua e crie de novo", não uma edição.
 */
export function ToolEditorDialog({
  tool,
  existingNames,
  onOpenChange,
  onSaved,
}: {
  tool: ToolSummary | null;
  existingNames: string[];
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const isEdit = tool !== null;

  const [toolName, setToolName] = useState(tool?.tool_name ?? "");
  const [nameTouched, setNameTouched] = useState(isEdit);
  // Tool nova começa em API: a do próprio negócio é o caso comum; a toolkit padrão é o complemento.
  const [kind, setKind] = useState<ToolKind>(tool?.kind ?? "api");
  const [label, setLabel] = useState(tool?.label ?? "");
  const [description, setDescription] = useState(tool?.description ?? "");
  const [enabled, setEnabled] = useState(tool?.enabled ?? true);
  const [sideEffect, setSideEffect] = useState<"" | "yes" | "no">(
    tool?.side_effect === true ? "yes" : tool?.side_effect === false ? "no" : ""
  );
  const sideEffectValue = sideEffect === "" ? null : sideEffect === "yes";

  const [catalog, setCatalog] = useState<BuiltinCatalogEntry[] | null>(null);
  const [pythonEnabled, setPythonEnabled] = useState<boolean | null>(null);
  const [builtinId, setBuiltinId] = useState<string>((tool?.config as { builtin_id?: string } | undefined)?.builtin_id ?? "");
  const [builtinParams, setBuiltinParams] = useState<Record<string, unknown>>(
    (tool?.config as { params?: Record<string, unknown> } | undefined)?.params ?? {}
  );
  const [apiDraft, setApiDraft] = useState<ApiDraft>(tool && tool.kind === "api" ? apiDraftFrom(tool.config) : emptyApiDraft());
  const [pythonDraft, setPythonDraft] = useState<PythonToolConfig>(
    tool && tool.kind === "python" ? pythonDraftFrom(tool.config) : { code: PYTHON_TEMPLATE, entrypoint: "handler", timeout_seconds: 10 }
  );

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    requestJson<BuiltinCatalogEntry[]>("/api/tools/catalog", { fallbackError: "Falha ao carregar o catálogo" })
      .then((data) => {
        setCatalog(data);
        const first = data.find((entry) => entry.available);
        if (!isEdit && !builtinId && first) setBuiltinId(first.builtin_id);
      })
      .catch(() => setCatalog([]));
    requestJson<{ enabled: boolean }>("/api/tools/python-config", { fallbackError: "" })
      .then((data) => setPythonEnabled(data.enabled))
      .catch(() => setPythonEnabled(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const builtinSpec = catalog?.find((c) => c.builtin_id === builtinId) ?? null;

  const nameError = !toolName.trim()
    ? "Dê um nome estável à tool."
    : !SLUG_PATTERN.test(toolName)
      ? "Use letras minúsculas, números, '-' ou '_'."
      : !isEdit && existingNames.includes(toolName)
        ? "Já existe uma tool com este nome."
        : null;
  const labelError = label.trim() ? null : "Dê um rótulo pra exibir na lista.";
  const builtinError =
    kind !== "builtin"
      ? null
      : !builtinId
        ? "Escolha uma toolkit."
        : !isEdit && builtinSpec && !builtinSpec.available
          ? "Esta toolkit não está instalada no serviço."
          : null;
  const apiUrlError = kind === "api" && !/^https?:\/\//.test(apiDraft.url.trim()) ? "Informe uma URL http(s)://..." : null;
  const pythonError =
    kind === "python" && (!pythonDraft.code.trim() || !pythonDraft.entrypoint.trim())
      ? "Escreva o código e o nome da função de entrada."
      : null;
  // Mesma regra do backend: um parâmetro composto que o modelo preenche só vira
  // schema válido com a forma de dentro declarada.
  const apiParamsError =
    kind === "api"
      ? (apiDraft.parameters
          .filter((p) => (p.source ?? "model") === "model")
          .map((p) => {
            if (p.type === "object") {
              return p.fields?.length
                ? schemaProblem(p.fields, 2)
                : `o parâmetro ${p.name || "(sem nome)"} é object e precisa de ao menos um campo`;
            }
            if (p.type === "array") {
              if (!p.items) return `o parâmetro ${p.name || "(sem nome)"} é array e precisa do tipo do item`;
              if (p.items.type === "object") {
                return p.items.fields?.length
                  ? schemaProblem(p.items.fields, 2)
                  : `os itens de ${p.name || "(sem nome)"} são object e precisam de ao menos um campo`;
              }
            }
            return null;
          })
          .find(Boolean) ?? null)
      : null;
  const hasErrors = Boolean(nameError || labelError || builtinError || apiUrlError || pythonError || apiParamsError);

  function buildConfig(): Record<string, unknown> {
    if (kind === "builtin") return { builtin_id: builtinId, params: builtinParams };
    if (kind === "api") return buildApiConfig(apiDraft) as unknown as Record<string, unknown>;
    return pythonDraft as unknown as Record<string, unknown>;
  }

  async function handleSubmit() {
    if (hasErrors || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      if (isEdit) {
        const payload: ToolUpdateInput = {
          label: label.trim(),
          description: description.trim() || null,
          enabled,
          side_effect: sideEffectValue,
          config: buildConfig(),
        };
        await requestJson(`/api/tools/${encodeURIComponent(tool.tool_name)}`, {
          method: "PUT",
          json: payload,
          fallbackError: "Falha ao salvar a tool",
        });
      } else {
        const payload: ToolInput = {
          tool_name: toolName,
          kind,
          label: label.trim(),
          description: description.trim() || null,
          enabled,
          side_effect: sideEffectValue,
          config: buildConfig(),
        };
        await requestJson("/api/tools", { method: "POST", json: payload, fallbackError: "Falha ao criar a tool" });
      }
      onSaved();
    } catch (err) {
      setSubmitError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open onOpenChange={onOpenChange} size="lg" className="sm:max-h-[85vh]">
      <DialogHeader>
        <DialogTitle>{isEdit ? `Editar ${tool.label}` : "Nova tool"}</DialogTitle>
        <DialogDescription>
          {isEdit
            ? "O tipo não muda depois de criada — exclua e crie de novo se precisar de outro."
            : "Escolha o tipo e preencha a configuração. Nenhum deploy é necessário: fica disponível para os agentes assim que salva."}
        </DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-4">
        {!isEdit && (
          <Field label="Tipo">
            <div className="grid grid-cols-3 gap-2">
              {(Object.keys(TOOL_KIND_META) as ToolKind[]).map((k) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => {
                    setKind(k);
                    // A toolkit sabe se age fora do serviço; API e Python, só quem as escreveu.
                    setSideEffect(k === "builtin" && builtinSpec ? builtinSideEffect(builtinSpec, builtinParams) : "");
                  }}
                  className={cn(
                    "flex flex-col gap-0.5 rounded-lg border px-3 py-2 text-left text-[13px] transition-colors",
                    kind === k ? "border-primary bg-primary-soft" : "border-input hover:bg-accent/50"
                  )}
                >
                  <span className="font-medium">{TOOL_KIND_META[k].label}</span>
                  <span className="text-xs text-muted-foreground">{TOOL_KIND_META[k].description}</span>
                </button>
              ))}
            </div>
          </Field>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Rótulo" htmlFor="tool-label" error={labelError}>
            <Input
              id="tool-label"
              value={label}
              onChange={(e) => {
                setLabel(e.target.value);
                if (!nameTouched && !isEdit) setToolName(slugify(e.target.value));
              }}
              placeholder="Ex.: Consulta de CEP"
            />
          </Field>
          <Field label="Nome (slug)" htmlFor="tool-name" error={nameError} hint={isEdit ? undefined : "Usado em AgentDefinition.tools"}>
            <Input
              id="tool-name"
              value={toolName}
              disabled={isEdit}
              onChange={(e) => {
                setNameTouched(true);
                setToolName(e.target.value);
              }}
              className="font-mono"
            />
          </Field>
        </div>

        <Field label="Descrição" htmlFor="tool-description" hint="Opcional — ajuda o modelo a saber quando usar a tool.">
          <Textarea id="tool-description" value={description} onChange={(e) => setDescription(e.target.value)} rows={2} />
        </Field>

        <label className="flex w-fit cursor-pointer items-center gap-2 text-[13px]">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="accent-primary" />
          Ativada (desligue para tirar de uso sem excluir)
        </label>

        <Field
          label="Efeito colateral"
          htmlFor="tool-side-effect"
          hint={
            sideEffect === "" ? (
              <span className="text-warning">
                Não classificada. Pode salvar assim, mas defina depois: a fila de revisão dos Logs usa isto para
                destacar as execuções que gravaram, cobraram ou enviaram algo.
              </span>
            ) : (
              "Usado na fila de revisão dos Logs para destacar as execuções que mudaram algo no sistema chamado."
            )
          }
        >
          <Select id="tool-side-effect" value={sideEffect} onChange={(e) => setSideEffect(e.target.value as typeof sideEffect)}>
            <option value="">Não classificada</option>
            <option value="yes">Sim — grava, cobra, transfere ou envia algo</option>
            <option value="no">Não — só consulta</option>
          </Select>
        </Field>

        <div className="border-t border-border pt-4">
          {kind === "builtin" && (
            <BuiltinFields
              catalog={catalog}
              builtinId={builtinId}
              onBuiltinIdChange={(id) => {
                setBuiltinId(id);
                setBuiltinParams({});
                const entry = catalog?.find((c) => c.builtin_id === id);
                if (entry) setSideEffect(builtinSideEffect(entry, {}));
              }}
              spec={builtinSpec}
              params={builtinParams}
              onParamsChange={(next) => {
                setBuiltinParams(next);
                if (builtinSpec && next.read_only !== builtinParams.read_only) {
                  setSideEffect(builtinSideEffect(builtinSpec, next));
                }
              }}
              disabled={isEdit}
            />
          )}
          {kind === "api" && <ApiFields draft={apiDraft} onChange={setApiDraft} paramsError={apiParamsError} />}
          {kind === "python" && (
            <PythonFields draft={pythonDraft} onChange={setPythonDraft} enabledOnServer={pythonEnabled} />
          )}
        </div>

        {submitError && <p className="text-[13px] text-destructive">{submitError}</p>}
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)}>
          Cancelar
        </Button>
        <Button onClick={() => void handleSubmit()} disabled={submitting || hasErrors}>
          {submitting && <Spinner />}
          {isEdit ? "Salvar" : "Criar tool"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

// -- kind="builtin" ----------------------------------------------------

/** O catálogo já vem na ordem de apresentação; agrupa preservando-a. */
function catalogGroups(catalog: BuiltinCatalogEntry[]): [string, BuiltinCatalogEntry[]][] {
  const groups = new Map<string, BuiltinCatalogEntry[]>();
  for (const entry of catalog) groups.set(entry.category, [...(groups.get(entry.category) ?? []), entry]);
  return [...groups.entries()];
}

/** Mesma regra do servidor ao criar: tem escrita e não está em somente leitura. */
function builtinSideEffect(entry: BuiltinCatalogEntry, params: Record<string, unknown>): "yes" | "no" {
  const readOnlyParam = entry.params.find((p) => p.name === "read_only");
  const readOnly = Boolean(params.read_only ?? readOnlyParam?.default ?? false);
  return entry.side_effect && !readOnly ? "yes" : "no";
}

function BuiltinFields({
  catalog,
  builtinId,
  onBuiltinIdChange,
  spec,
  params,
  onParamsChange,
  disabled,
}: {
  catalog: BuiltinCatalogEntry[] | null;
  builtinId: string;
  onBuiltinIdChange: (id: string) => void;
  spec: BuiltinCatalogEntry | null;
  params: Record<string, unknown>;
  onParamsChange: (params: Record<string, unknown>) => void;
  disabled: boolean;
}) {
  if (catalog === null) {
    return (
      <p className="flex items-center gap-2 text-[13px] text-muted-foreground">
        <Spinner /> Carregando catálogo...
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <Field
        label="Toolkit"
        htmlFor="builtin-id"
        hint={
          spec && !spec.available ? (
            <span className="text-warning">{spec.unavailable_reason}</span>
          ) : spec ? (
            <>
              {spec.description}
              {spec.side_effect && (
                <span className="mt-1 block">
                  Age fora do serviço (envia, cria ou agenda). Nos testes em dry_run essas funções não rodam: o
                  modelo recebe o que elas teriam feito.
                </span>
              )}
            </>
          ) : undefined
        }
      >
        <Select id="builtin-id" value={builtinId} disabled={disabled} onChange={(e) => onBuiltinIdChange(e.target.value)}>
          {catalogGroups(catalog).map(([category, entries]) => (
            <optgroup key={category} label={category}>
              {entries.map((entry) => (
                <option
                  key={entry.builtin_id}
                  value={entry.builtin_id}
                  disabled={!entry.available && entry.builtin_id !== builtinId}
                >
                  {entry.label}
                  {entry.available ? "" : " (não instalada)"}
                </option>
              ))}
            </optgroup>
          ))}
        </Select>
      </Field>
      {spec && spec.params.length > 0 && (
        <div className="flex flex-col gap-3">
          {spec.params.map((p) => (
            <Field key={p.name} label={p.label + (p.required ? " *" : "")} htmlFor={`param-${p.name}`} hint={p.description || undefined}>
              {p.type === "boolean" ? (
                <label className="flex w-fit cursor-pointer items-center gap-2 text-[13px]">
                  <input
                    id={`param-${p.name}`}
                    type="checkbox"
                    checked={Boolean(params[p.name] ?? p.default ?? false)}
                    onChange={(e) => onParamsChange({ ...params, [p.name]: e.target.checked })}
                    className="accent-primary"
                  />
                  {p.description || "Ativado"}
                </label>
              ) : p.choices && p.choices.length > 0 ? (
                <Select
                  id={`param-${p.name}`}
                  value={String(params[p.name] ?? p.default ?? p.choices[0])}
                  onChange={(e) => onParamsChange({ ...params, [p.name]: e.target.value })}
                >
                  {p.choices.map((choice) => (
                    <option key={choice} value={choice}>
                      {choice}
                    </option>
                  ))}
                </Select>
              ) : (
                <Input
                  id={`param-${p.name}`}
                  type={p.secret ? "password" : p.type === "integer" ? "number" : "text"}
                  value={(params[p.name] as string | number | undefined) ?? ""}
                  onChange={(e) =>
                    onParamsChange({
                      ...params,
                      [p.name]: p.type === "integer" ? (e.target.value === "" ? undefined : Number(e.target.value)) : e.target.value,
                    })
                  }
                />
              )}
            </Field>
          ))}
        </div>
      )}
    </div>
  );
}

// -- kind="api" ----------------------------------------------------------

function ApiFields({
  draft,
  onChange,
  paramsError,
}: {
  draft: ApiDraft;
  onChange: (draft: ApiDraft) => void;
  paramsError?: string | null;
}) {
  function update<K extends keyof ApiDraft>(key: K, value: ApiDraft[K]) {
    onChange({ ...draft, [key]: value });
  }
  function updateParam(index: number, patch: Partial<ApiToolParam>) {
    update(
      "parameters",
      draft.parameters.map((p, i) => (i === index ? { ...p, ...patch } : p))
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-[120px_1fr] gap-3">
        <Field label="Método" htmlFor="api-method">
          <Select id="api-method" value={draft.method} onChange={(e) => update("method", e.target.value as ApiToolConfig["method"])}>
            {API_METHODS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="URL" htmlFor="api-url" hint="Use {nome} para um parâmetro de path — ex.: https://api.com/items/{id}">
          <Input
            id="api-url"
            value={draft.url}
            onChange={(e) => update("url", e.target.value)}
            placeholder="https://api.exemplo.com/recurso"
            className="font-mono text-xs"
          />
        </Field>
      </div>

      <Field label="Timeout (segundos)" htmlFor="api-timeout">
        <Input
          id="api-timeout"
          type="number"
          min={1}
          max={60}
          value={draft.timeout_seconds}
          onChange={(e) => update("timeout_seconds", Number(e.target.value))}
          className="w-28"
        />
      </Field>

      <Field label="Autenticação">
        <div className="flex flex-col gap-2">
          <Select
            value={draft.authType}
            onChange={(e) => update("authType", e.target.value as ApiAuth["type"])}
            wrapperClassName="w-44"
          >
            <option value="none">Nenhuma</option>
            <option value="bearer">Bearer token</option>
            <option value="api_key">Chave em header</option>
            <option value="basic">Usuário/senha</option>
          </Select>
          {draft.authType === "bearer" && (
            <Input
              type="password"
              placeholder="Token"
              value={draft.authFields.token ?? ""}
              onChange={(e) => update("authFields", { ...draft.authFields, token: e.target.value })}
            />
          )}
          {draft.authType === "api_key" && (
            <div className="grid grid-cols-2 gap-2">
              <Input
                placeholder="Nome do header (ex.: X-API-Key)"
                value={draft.authFields.header ?? ""}
                onChange={(e) => update("authFields", { ...draft.authFields, header: e.target.value })}
              />
              <Input
                type="password"
                placeholder="Valor"
                value={draft.authFields.value ?? ""}
                onChange={(e) => update("authFields", { ...draft.authFields, value: e.target.value })}
              />
            </div>
          )}
          {draft.authType === "basic" && (
            <div className="grid grid-cols-2 gap-2">
              <Input
                placeholder="Usuário"
                value={draft.authFields.username ?? ""}
                onChange={(e) => update("authFields", { ...draft.authFields, username: e.target.value })}
              />
              <Input
                type="password"
                placeholder="Senha"
                value={draft.authFields.password ?? ""}
                onChange={(e) => update("authFields", { ...draft.authFields, password: e.target.value })}
              />
            </div>
          )}
        </div>
      </Field>

      <Field
        label="Parâmetros"
        error={paramsError}
        hint="'path' precisa aparecer na URL como {nome}; 'body' vira um campo do JSON enviado. A origem diz quem preenche o valor."
        aside={
          <Button
            variant="ghost"
            size="sm"
            onClick={() => update("parameters", [...draft.parameters, { name: "", type: "string", location: "query", required: false }])}
          >
            <Plus /> Parâmetro
          </Button>
        }
      >
        {draft.parameters.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">Nenhum — a tool não recebe argumentos do modelo.</p>
        ) : (
          <div className="flex flex-col gap-2">
            {draft.parameters.map((p, i) => (
              <div key={i} className="flex flex-col gap-1.5 rounded-md border border-border/60 p-2">
                <div className="grid grid-cols-[1fr_100px_110px_auto_auto] items-center gap-1.5">
                <Input
                  placeholder="nome"
                  value={p.name}
                  onChange={(e) => updateParam(i, { name: e.target.value })}
                  className="font-mono text-xs"
                />
                <Select value={p.type} onChange={(e) => updateParam(i, { type: e.target.value as ApiParamType })}>
                  {API_PARAM_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </Select>
                <Select value={p.location} onChange={(e) => updateParam(i, { location: e.target.value as ApiParamLocation })}>
                  {API_PARAM_LOCATIONS.map((l) => (
                    <option key={l} value={l}>
                      {l}
                    </option>
                  ))}
                </Select>
                <label className="flex items-center gap-1 text-xs text-muted-foreground" title="Obrigatório">
                  <input
                    type="checkbox"
                    checked={Boolean(p.required)}
                    onChange={(e) => updateParam(i, { required: e.target.checked })}
                    className="accent-primary"
                  />
                  obrig.
                </label>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Remover parâmetro"
                  onClick={() => update("parameters", draft.parameters.filter((_, idx) => idx !== i))}
                >
                  <Trash />
                </Button>
                </div>

                <div className="grid grid-cols-[110px_1fr] items-center gap-1.5">
                  <Select
                    aria-label="Origem do valor"
                    value={p.source ?? "model"}
                    onChange={(e) => {
                      const source = e.target.value as ApiParamSource;
                      updateParam(i, {
                        source,
                        dependency: source === "dependency" ? (p.dependency ?? p.name) : undefined,
                        value: source === "const" ? (p.value ?? "") : undefined,
                      });
                    }}
                  >
                    <option value="model">modelo</option>
                    <option value="dependency">dependência</option>
                    <option value="const">fixo</option>
                  </Select>
                  {(p.source ?? "model") === "model" && (
                    <p className="text-[12px] text-muted-foreground">O modelo preenche ao chamar a tool.</p>
                  )}
                  {p.source === "dependency" && (
                    <Input
                      placeholder="campo de dependencies (ex.: cpf)"
                      value={p.dependency ?? ""}
                      onChange={(e) => updateParam(i, { dependency: e.target.value })}
                      className="font-mono text-xs"
                    />
                  )}
                  {p.source === "const" && (
                    <Input
                      placeholder="valor fixo"
                      value={String(p.value ?? "")}
                      onChange={(e) => updateParam(i, { value: e.target.value })}
                      className="font-mono text-xs"
                    />
                  )}
                </div>

                {/* `object` e `array` só entram no schema do modelo com a forma
                    de dentro declarada: um {"type":"array"} pelado o provedor
                    recusa, e o backend devolve 422 ao salvar. Em dependência ou
                    valor fixo o parâmetro não vai para o schema, então não se aplica. */}
                {(p.source ?? "model") === "model" && p.type === "object" && (
                  <div className="border-l-2 border-border pl-3">
                    <p className="mb-1.5 text-[12px] text-muted-foreground">campos do objeto</p>
                    <SchemaFieldsEditor
                      value={p.fields ?? []}
                      onChange={(fields) => updateParam(i, { fields })}
                      depth={2}
                    />
                  </div>
                )}
                {(p.source ?? "model") === "model" && p.type === "array" && (
                  <div className="border-l-2 border-border pl-3">
                    <p className="mb-1.5 text-[12px] text-muted-foreground">cada item é</p>
                    <Select
                      aria-label="Tipo do item"
                      value={p.items?.type ?? "string"}
                      onChange={(e) => {
                        const type = e.target.value as ItemType;
                        updateParam(i, {
                          items: type === "object" ? { type, fields: p.items?.fields ?? [] } : { type },
                        });
                      }}
                      className="h-8 w-32"
                    >
                      {ITEM_TYPES.map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </Select>
                    {p.items?.type === "object" && (
                      <div className="mt-2">
                        <SchemaFieldsEditor
                          value={p.items.fields ?? []}
                          onChange={(fields) => updateParam(i, { items: { type: "object", fields } })}
                          depth={2}
                        />
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Field>

      <Field
        label="Headers fixos"
        aside={
          <Button variant="ghost" size="sm" onClick={() => update("headerRows", [...draft.headerRows, { key: "", value: "" }])}>
            <Plus /> Header
          </Button>
        }
      >
        {draft.headerRows.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">Nenhum.</p>
        ) : (
          <div className="flex flex-col gap-2">
            {draft.headerRows.map((h, i) => (
              <div key={i} className="grid grid-cols-[1fr_1fr_auto] gap-1.5">
                <Input
                  placeholder="Nome"
                  value={h.key}
                  onChange={(e) =>
                    update(
                      "headerRows",
                      draft.headerRows.map((row, idx) => (idx === i ? { ...row, key: e.target.value } : row))
                    )
                  }
                />
                <Input
                  placeholder="Valor"
                  value={h.value}
                  onChange={(e) =>
                    update(
                      "headerRows",
                      draft.headerRows.map((row, idx) => (idx === i ? { ...row, value: e.target.value } : row))
                    )
                  }
                />
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Remover header"
                  onClick={() => update("headerRows", draft.headerRows.filter((_, idx) => idx !== i))}
                >
                  <Trash />
                </Button>
              </div>
            ))}
          </div>
        )}
      </Field>
    </div>
  );
}

// -- kind="python" ---------------------------------------------------------

function PythonFields({
  draft,
  onChange,
  enabledOnServer,
}: {
  draft: PythonToolConfig;
  onChange: (draft: PythonToolConfig) => void;
  enabledOnServer: boolean | null;
}) {
  function update<K extends keyof PythonToolConfig>(key: K, value: PythonToolConfig[K]) {
    onChange({ ...draft, [key]: value });
  }
  return (
    <div className="flex flex-col gap-4">
      {enabledOnServer === false && (
        <p className="rounded-lg border border-warning/30 bg-warning/5 px-3 py-2 text-[13px] text-warning">
          Tools Python estão desligadas no servidor (<code className="font-mono">CUSTOM_PYTHON_TOOLS_ENABLED=false</code>). Você
          pode criar e editar, mas ela só roda quando isso for ligado.
        </p>
      )}
      <p className="text-[13px] text-muted-foreground">
        Só <code className="font-mono text-xs">import</code>, <code className="font-mono text-xs">def</code>/
        <code className="font-mono text-xs">async def</code> e constantes simples no nível do módulo — sem classes, sem{" "}
        <code className="font-mono text-xs">os</code>/<code className="font-mono text-xs">subprocess</code>/
        <code className="font-mono text-xs">eval</code>. Módulos liberados: math, json, re, datetime, statistics, httpx,
        random, collections, itertools, functools, hashlib, base64, uuid, string, textwrap, decimal, time.
      </p>
      <Field label="Código" htmlFor="python-code">
        <Textarea
          id="python-code"
          value={draft.code}
          onChange={(e) => update("code", e.target.value)}
          rows={12}
          spellCheck={false}
          className="font-mono text-xs"
        />
      </Field>
      <div className="grid grid-cols-2 gap-4">
        <Field label="Função de entrada" htmlFor="python-entrypoint" hint="O nome de uma função definida no código acima.">
          <Input id="python-entrypoint" value={draft.entrypoint} onChange={(e) => update("entrypoint", e.target.value)} className="font-mono" />
        </Field>
        <Field label="Timeout (segundos)" htmlFor="python-timeout">
          <Input
            id="python-timeout"
            type="number"
            min={1}
            max={30}
            value={draft.timeout_seconds}
            onChange={(e) => update("timeout_seconds", Number(e.target.value))}
          />
        </Field>
      </div>
    </div>
  );
}
