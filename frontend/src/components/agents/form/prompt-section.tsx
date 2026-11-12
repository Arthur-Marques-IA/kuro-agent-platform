"use client";

import { useState } from "react";
import { Maximize2 } from "lucide-react";
import { formatNumber } from "@/lib/format";
import { toLines } from "@/lib/agent-form";
import type { AgentDefinition } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Field } from "@/components/ui/primitives";
import { FormSection, type SectionProps } from "./form-section";
import { PromptEditorDialog } from "./prompt-editor";

export function PromptSection({
  id,
  variant,
  values,
  update,
  fieldError,
  anchor,
  agent,
  promptChanged,
}: SectionProps & { anchor?: string; agent?: AgentDefinition; promptChanged: boolean }) {
  const [editorOpen, setEditorOpen] = useState(false);
  const lines = toLines(values.instructions);

  return (
    <FormSection
      anchor={anchor}
      variant={variant}
      title="Prompt"
      description="Instruções de sistema, uma por linha. Toda alteração salva vira uma nova versão, com histórico e comparação."
      action={
        <Button variant="ghost" size="sm" onClick={() => setEditorOpen(true)} className="-my-1 h-7 px-2 text-xs">
          <Maximize2 className="size-3.5" /> Tela cheia
        </Button>
      }
    >
      <Field
        label="Instruções"
        htmlFor={`${id}-instructions`}
        error={fieldError("instructions")}
        aside={
          promptChanged && agent ? (
            <Badge variant="warning">será salvo como v{agent.prompt_version + 1}</Badge>
          ) : agent ? (
            <Badge variant="secondary">v{agent.prompt_version}</Badge>
          ) : undefined
        }
        hint={`${formatNumber(lines.length)} ${lines.length === 1 ? "instrução" : "instruções"} · ${formatNumber(values.instructions.length)} caracteres · em tela cheia: numeração, busca e diferenças`}
      >
        <Textarea
          id={`${id}-instructions`}
          value={values.instructions}
          onChange={(e) => update("instructions", e.target.value)}
          rows={variant === "sheet" ? 10 : 14}
          placeholder={"Você é um agente de suporte técnico.\nResponda de forma objetiva, em português."}
          aria-invalid={Boolean(fieldError("instructions"))}
          className="min-h-40 resize-y font-mono text-[13px] leading-6"
          spellCheck={false}
        />
      </Field>
      <PromptEditorDialog
        open={editorOpen}
        onOpenChange={setEditorOpen}
        value={values.instructions}
        onChange={(text) => update("instructions", text)}
        saved={agent?.instructions}
        savedVersion={agent?.prompt_version}
        changed={promptChanged}
      />
    </FormSection>
  );
}
