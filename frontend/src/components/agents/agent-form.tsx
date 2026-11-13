"use client";

import { FormEvent, useCallback, useEffect, useId, useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/http";
import { slugify } from "@/lib/format";
import { DEFAULT_MODEL, MODEL_OPTIONS, type ModelOption } from "@/lib/agent-meta";
import {
  buildCreate,
  buildUpdate,
  formSections,
  numberProblem,
  sectionStatus,
  toLines,
  valuesFrom,
  type AgentFormPayload,
  type FormErrors,
  type FormSectionId,
  type FormValues,
  type ModelContext,
} from "@/lib/agent-form";
import type { AgentDefinition, AgentKind, ToolSummary } from "@/lib/types";
import { schemaProblem } from "@/components/schema/schema-fields-editor";
import { stagesProblem, starterStages } from "@/components/agents/stages-editor";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/primitives";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { useProviderModels } from "@/lib/use-provider-models";
import { sectionAnchor, type FormVariant, type SectionProps } from "@/components/agents/form/form-section";
import { IdentitySection } from "@/components/agents/form/identity-section";
import { KindSection } from "@/components/agents/form/kind-section";
import { PromptSection } from "@/components/agents/form/prompt-section";
import { ModelSection } from "@/components/agents/form/model-section";
import { MemorySection } from "@/components/agents/form/memory-section";
import { OutputSection, StagesSection } from "@/components/agents/form/structure-sections";
import { ContextSection } from "@/components/agents/form/context-section";
import { ToolsSection } from "@/components/agents/form/tools-section";
import { SectionNav } from "@/components/agents/form/section-nav";
import { SaveReviewDialog } from "@/components/agents/form/save-review-dialog";

export type { AgentFormPayload };

const SLUG_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;

interface AgentFormProps {
  mode: "create" | "edit";
  /** Estado salvo (edição) — base para detectar alterações. */
  agent?: AgentDefinition;
  /** Instruções iniciais diferentes das salvas (ex. versão restaurada do histórico). */
  initialInstructions?: string[];
  availableTools: ToolSummary[];
  onSubmit: (payload: AgentFormPayload) => Promise<void>;
  onCancel?: () => void;
  /** `page`: seções com índice lateral e barra de ações fixa; `sheet`: coluna única para painel lateral. */
  variant?: FormVariant;
}

export function AgentForm({
  mode,
  agent,
  initialInstructions,
  availableTools,
  onSubmit,
  onCancel,
  variant = "page",
}: AgentFormProps) {
  const id = useId();
  // A base das alterações: os valores do formulário para o agente salvo, com os mesmos
  // defaults dos campos que o serviço não devolveu. Nunca a resposta crua da API.
  const initial = useMemo(() => valuesFrom(agent, DEFAULT_MODEL.id), [agent]);
  const [values, setValues] = useState<FormValues>(() => valuesFrom(agent, DEFAULT_MODEL.id, initialInstructions));
  const [slugTouched, setSlugTouched] = useState(mode === "edit");
  const [showErrors, setShowErrors] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);

  const { modelProviders, modelCredentials, collections } = useWorkspace();
  // `google` sempre disponível (fallback antigo via GOOGLE_API_KEY no ambiente);
  // os demais só entram depois de terem ao menos uma credencial configurada e
  // habilitada em /models.
  const availableProviders = useMemo(
    () => new Set(["google", ...modelCredentials.filter((c) => c.configured && c.enabled).map((c) => c.provider)]),
    [modelCredentials]
  );
  const providerModels = useProviderModels([...availableProviders], MODEL_OPTIONS);
  const models = useMemo<ModelOption[]>(() => {
    const base = providerModels.models;
    return agent?.model_id && !base.some((m) => m.id === agent.model_id)
      ? [...base, { provider: agent.model_provider ?? "", id: agent.model_id, label: agent.model_id }]
      : base;
  }, [agent, providerModels.models]);
  const modelCtx = useMemo<ModelContext>(() => ({ models, defaultModel: DEFAULT_MODEL }), [models]);

  const currentProvider = models.find((m) => m.id === values.modelId)?.provider;
  const credentialsForProvider = useMemo(
    () => modelCredentials.filter((c) => c.provider === currentProvider && c.enabled),
    [modelCredentials, currentProvider]
  );

  function handleModelChange(modelId: string) {
    const nextProvider = models.find((m) => m.id === modelId)?.provider;
    const keepsCredential = modelCredentials.some((c) => c.id === values.credentialId && c.provider === nextProvider);
    setValues((prev) => ({ ...prev, modelId, credentialId: keepsCredential ? prev.credentialId : "" }));
  }

  const instructionLines = toLines(values.instructions);
  const changes = mode === "edit" && agent ? buildUpdate(values, initial, agent, modelCtx) : null;
  const dirty = changes
    ? Object.keys(changes).length > 0
    : values.name.trim() !== "" || values.instructions.trim() !== "";

  const errors: FormErrors = {
    name: values.name.trim() ? null : "Dê um nome ao agente.",
    agentType: !values.agentType
      ? "Defina um slug."
      : SLUG_PATTERN.test(values.agentType)
        ? null
        : "Use letras minúsculas, números, '-' ou '_' (começando por letra ou número).",
    instructions: instructionLines.length > 0 ? null : "Escreva ao menos uma instrução.",
    numHistoryRuns:
      values.kind === "analysis" || values.numHistoryRuns >= 0 ? null : "Use um número maior ou igual a zero.",
    historyToolCalls: values.kind === "analysis" ? null : numberProblem("historyToolCalls", values.historyToolCalls),
    modelParams:
      numberProblem("temperature", values.temperature) ??
      numberProblem("topP", values.topP) ??
      numberProblem("maxTokens", values.maxTokens) ??
      numberProblem("timeoutSeconds", values.timeoutSeconds),
    responseSchema:
      values.kind !== "analysis"
        ? null
        : values.responseSchema.length === 0
          ? "Um agente analista precisa de ao menos um campo na saída."
          : schemaProblem(values.responseSchema),
    stages: values.kind === "procedural" ? stagesProblem(values.stages) : null,
  };
  const hasErrors = Object.values(errors).some(Boolean);

  const sections = formSections(values.kind);
  const status = sectionStatus(changes, showErrors ? errors : null);
  const anchorOf = useCallback((section: FormSectionId) => sectionAnchor(id, section), [id]);
  // Só a página tem índice; no painel lateral as seções não precisam de âncora.
  const anchor = (section: FormSectionId) => (variant === "page" ? anchorOf(section) : undefined);

  useEffect(() => {
    if (!dirty || submitting) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty, submitting]);

  function update<K extends keyof FormValues>(key: K, value: FormValues[K]) {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  function handleNameChange(name: string) {
    setValues((prev) => ({ ...prev, name, agentType: slugTouched ? prev.agentType : slugify(name) }));
  }

  function handleSlugChange(slug: string) {
    setSlugTouched(true);
    update("agentType", slug);
  }

  function changeKind(kind: AgentKind) {
    // Um procedural nasce com um ponto de partida que funciona: coletar e confirmar.
    setValues((prev) => ({
      ...prev,
      kind,
      stages: kind === "procedural" && prev.stages.length === 0 ? starterStages() : prev.stages,
    }));
  }

  function discard() {
    setValues(initial);
    setShowErrors(false);
    setSubmitError(null);
  }

  async function submit(payload: AgentFormPayload) {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await onSubmit(payload);
      setShowErrors(false);
      setReviewOpen(false);
    } catch (err) {
      setSubmitError(errorMessage(err));
      setReviewOpen(false);
    } finally {
      setSubmitting(false);
    }
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setShowErrors(true);
    if (hasErrors || submitting) return;
    if (changes) {
      if (Object.keys(changes).length === 0) return;
      // Prompt alterado: antes de virar uma versão nova, o diff contra a salva.
      if (changes.instructions && agent) {
        setReviewOpen(true);
        return;
      }
      void submit(changes);
    } else {
      void submit(buildCreate(values, modelCtx));
    }
  }

  const fieldError = (key: keyof FormErrors) => (showErrors ? errors[key] : null);
  const common: SectionProps = { id, variant, mode, values, update, fieldError };

  const body = (
    <>
      <IdentitySection
        {...common}
        anchor={anchor("identidade")}
        onNameChange={handleNameChange}
        onSlugChange={handleSlugChange}
      />
      <KindSection {...common} anchor={anchor("tipo")} onKindChange={changeKind} />
      <PromptSection {...common} anchor={anchor("prompt")} agent={agent} promptChanged={Boolean(changes?.instructions)} />
      {values.kind === "procedural" && (
        <StagesSection {...common} anchor={anchor("etapas")} availableTools={availableTools} />
      )}
      <ModelSection
        {...common}
        anchor={anchor("modelo")}
        modelList={{ models, loading: providerModels.loading, errors: providerModels.errors }}
        modelProviders={modelProviders}
        credentials={credentialsForProvider}
        collections={collections}
        availableProviderCount={availableProviders.size}
        onModelChange={handleModelChange}
      />
      <MemorySection {...common} anchor={anchor("memoria")} />
      {values.kind === "analysis" && <OutputSection {...common} anchor={anchor("saida")} />}
      <ContextSection {...common} anchor={anchor("contexto")} />
      <ToolsSection {...common} anchor={anchor("tools")} availableTools={availableTools} />
    </>
  );

  const statusLine = submitError ? (
    // role="alert": anunciado também quando o formulário está na gaveta (modal) do Playground.
    <p role="alert" className="min-w-0 text-[13px] text-destructive">{submitError}</p>
  ) : mode === "edit" ? (
    <p className="flex items-center gap-2 text-[13px] text-muted-foreground">
      <span className={cn("size-1.5 rounded-full", dirty ? "bg-warning" : "bg-success")} />
      {dirty ? "Alterações não salvas" : "Tudo salvo"}
    </p>
  ) : (
    <span />
  );

  const buttons = (
    <div className="flex shrink-0 items-center gap-2">
      {mode === "edit" && dirty && (
        <Button variant="ghost" onClick={discard} disabled={submitting}>
          Descartar
        </Button>
      )}
      {onCancel && (mode === "create" || variant === "sheet") && (
        <Button variant="outline" onClick={onCancel} disabled={submitting}>
          Cancelar
        </Button>
      )}
      <Button type="submit" disabled={submitting || (mode === "edit" && !dirty)}>
        {submitting && <Spinner />}
        {mode === "create" ? "Criar agente" : "Salvar alterações"}
      </Button>
    </div>
  );

  const review =
    agent && changes?.instructions ? (
      <SaveReviewDialog
        open={reviewOpen}
        onOpenChange={setReviewOpen}
        saved={agent.instructions}
        savedVersion={agent.prompt_version}
        changes={changes}
        otherSections={sections.filter((s) => s.id !== "prompt" && status.changed.has(s.id))}
        submitting={submitting}
        onConfirm={() => void submit(changes)}
      />
    ) : null;

  if (variant === "sheet") {
    return (
      <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col" noValidate>
        <div className="scrollbar-thin flex flex-1 flex-col gap-7 overflow-y-auto px-6 py-5">{body}</div>
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border bg-surface px-6 py-3.5">
          {statusLine}
          {buttons}
        </div>
        {review}
      </form>
    );
  }

  const nav = (orientation: "vertical" | "horizontal") => (
    <SectionNav
      sections={sections}
      anchorOf={anchorOf}
      changed={status.changed}
      invalid={status.invalid}
      orientation={orientation}
    />
  );

  return (
    <form onSubmit={handleSubmit} className="flex flex-col" noValidate>
      {/* Telas estreitas: o índice vira uma barra compacta presa no topo. */}
      <div className="sticky top-0 z-20 -mx-4 -mt-2 border-b border-border bg-background/90 px-4 py-2 backdrop-blur sm:-mx-6 sm:px-6 lg:hidden">
        {nav("horizontal")}
      </div>
      <div className="lg:grid lg:grid-cols-[10rem_minmax(0,1fr)] lg:gap-8">
        <aside className="hidden lg:block">
          <div className="sticky top-4 pt-2">{nav("vertical")}</div>
        </aside>
        <div className="flex min-w-0 flex-col divide-y divide-border">{body}</div>
      </div>
      <div className="sticky bottom-0 z-10 -mx-4 mt-2 flex flex-wrap items-center justify-between gap-3 border-t border-border bg-background/90 px-4 py-3 backdrop-blur sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8">
        {statusLine}
        {buttons}
      </div>
      {review}
    </form>
  );
}
