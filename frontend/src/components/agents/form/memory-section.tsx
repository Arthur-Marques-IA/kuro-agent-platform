import { cn } from "@/lib/cn";
import { MEMORY_BACKENDS, MEMORY_MODES } from "@/lib/agent-meta";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/ui/primitives";
import { FormSection, type SectionProps } from "./form-section";

export function MemorySection({ id, variant, values, update, fieldError, anchor }: SectionProps & { anchor?: string }) {
  if (values.kind === "analysis") {
    return (
      <FormSection
        anchor={anchor}
        variant={variant}
        title="Memória"
        description="Histórico da sessão e memória de longo prazo."
      >
        <p className="rounded-lg border border-dashed border-border px-4 py-3 text-[13px] text-muted-foreground">
          Um agente analista é one-shot: sem sessão, sem histórico e sem memória de longo prazo. A base de
          conhecimento continua valendo.
        </p>
      </FormSection>
    );
  }

  return (
    <FormSection
      anchor={anchor}
      variant={variant}
      title="Memória"
      description="Como o agente lembra do usuário entre mensagens e entre sessões, e em que formato responde."
    >
      <div className="flex flex-col gap-5">
        <Field
          label="Execuções no histórico"
          htmlFor={`${id}-history`}
          error={fieldError("numHistoryRuns")}
          hint="Quantas trocas anteriores da sessão entram no contexto."
        >
          <Input
            id={`${id}-history`}
            type="number"
            min={0}
            value={Number.isNaN(values.numHistoryRuns) ? "" : values.numHistoryRuns}
            onChange={(e) => update("numHistoryRuns", e.target.valueAsNumber)}
            className="sm:max-w-40"
          />
          <label className="mt-2 flex items-start gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={values.sessionSummary}
              onChange={(e) => update("sessionSummary", e.target.checked)}
              className="mt-0.5 accent-primary"
            />
            <span>
              <span className="font-medium text-foreground">Resumir o que sai do histórico</span> — a conversa longa
              mantém o contexto sem reenviar todas as trocas. O resumo é atualizado uma vez a cada janela, com o modelo
              auxiliar.
            </span>
          </label>
          <label className="mt-2 flex items-start gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={values.plainText}
              onChange={(e) => update("plainText", e.target.checked)}
              className="mt-0.5 accent-primary"
            />
            <span>
              <span className="font-medium text-foreground">Responder em texto puro</span> — sem markdown (negrito,
              listas com *). Para canais como WhatsApp, onde a formatação chega crua.
            </span>
          </label>
        </Field>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1.5 text-[13px] font-medium">Memória de longo prazo</legend>
          <label className="flex items-start gap-2 text-[13px]">
            <input
              type="checkbox"
              checked={values.memoryBackend !== "none"}
              onChange={(e) => update("memoryBackend", e.target.checked ? "auto" : "none")}
              className="mt-0.5 accent-primary"
            />
            <span>
              <span className="font-medium">Lembrar do usuário entre conversas</span>
              <span className="block text-xs text-muted-foreground">
                Desligada, o agente só conta com o histórico da sessão — sem tool de memória no prompt e sem chamadas
                extras. Ligue quando o mesmo usuário volta em sessões diferentes e o agente precisa lembrar dele.
              </span>
            </span>
          </label>
          {values.memoryBackend !== "none" && (
            <div className="grid gap-2 sm:grid-cols-3">
              {MEMORY_MODES.map((backend) => {
                const current = values.memoryBackend === "common" ? "agentic" : values.memoryBackend;
                const selected = current === backend;
                return (
                  <label
                    key={backend}
                    className={cn(
                      "flex cursor-pointer gap-3 rounded-lg border p-3 transition-colors",
                      selected ? "border-primary bg-primary-soft/50 ring-1 ring-primary" : "border-input hover:bg-accent/50"
                    )}
                  >
                    <input
                      type="radio"
                      name={`${id}-memory`}
                      value={backend}
                      checked={selected}
                      onChange={() => update("memoryBackend", backend)}
                      className="mt-0.5 accent-primary"
                    />
                    <span className="flex flex-col gap-0.5">
                      <span className="text-[13px] font-medium">{MEMORY_BACKENDS[backend].label}</span>
                      <span className="text-xs text-muted-foreground">{MEMORY_BACKENDS[backend].description}</span>
                    </span>
                  </label>
                );
              })}
            </div>
          )}
        </fieldset>
      </div>
    </FormSection>
  );
}
