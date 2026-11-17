import Link from "next/link";
import { REASONING_LEVELS, type ModelOption } from "@/lib/agent-meta";
import type { FormValues } from "@/lib/agent-form";
import type { Collection, ModelCredential, ModelProviderSummary } from "@/lib/types";
import { Select } from "@/components/ui/select";
import { Field, Spinner } from "@/components/ui/primitives";
import { FormSection, ParamInput, type SectionProps } from "./form-section";

/** O nome unificado da tela `/models`. */
const MODEL_CREDENTIALS_LABEL = "Credenciais de modelos";

/** Agrupa as opções de modelo pelo rótulo do provedor, pros `<optgroup>` do seletor. */
function groupByProvider(models: ModelOption[], providers: ModelProviderSummary[]): Record<string, ModelOption[]> {
  const labelByProvider = new Map(providers.map((p) => [p.provider, p.label]));
  const groups: Record<string, ModelOption[]> = {};
  for (const model of models) {
    const label = labelByProvider.get(model.provider) ?? model.provider;
    (groups[label] ??= []).push(model);
  }
  return groups;
}

export interface ModelListState {
  models: ModelOption[];
  loading: boolean;
  /** Provedor → motivo (não respondeu, tempo esgotado): para ele vale a lista fixa. */
  errors: Record<string, string>;
}

export function ModelSection({
  id,
  variant,
  values,
  update,
  fieldError,
  anchor,
  modelList,
  modelProviders,
  credentials,
  collections,
  availableProviderCount,
  onModelChange,
}: SectionProps & {
  anchor?: string;
  modelList: ModelListState;
  modelProviders: ModelProviderSummary[];
  /** Credenciais habilitadas do provedor do modelo escolhido. */
  credentials: ModelCredential[];
  collections: Collection[];
  availableProviderCount: number;
  onModelChange: (modelId: string) => void;
}) {
  const { models, loading, errors } = modelList;
  const current = models.find((m) => m.id === values.modelId);
  const provider = current?.provider;
  const failed = Object.keys(errors);

  return (
    <FormSection
      anchor={anchor}
      variant={variant}
      title={values.kind === "analysis" ? "Modelo e base" : "Modelo"}
      description={
        values.kind === "analysis"
          ? "Qual LLM analisa o documento e de qual base ele pode consultar."
          : "Qual LLM responde, com qual credencial, de qual base consulta e com quais parâmetros."
      }
    >
      <div className="flex flex-col gap-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Modelo"
            htmlFor={`${id}-model`}
            hint={
              <>
                {loading ? (
                  <span className="inline-flex items-center gap-1.5">
                    <Spinner className="size-3" /> Buscando a lista nos provedores — o modelo salvo continua valendo.{" "}
                  </span>
                ) : failed.length > 0 ? (
                  `Lista fixa para ${failed.join(", ")} (${Object.values(errors)[0]}). `
                ) : (
                  "Lista lida agora dos provedores. "
                )}
                {current?.channel === "preview" && (
                  <span className="text-warning">
                    Modelo preview: pode mudar ou sair do ar sem aviso — evite em produção.{" "}
                  </span>
                )}
                {current?.channel === "alias" && (
                  <span className="text-warning">
                    Apelido (-latest): o provedor troca o modelo por trás dele — fixe uma versão em produção.{" "}
                  </span>
                )}
                {availableProviderCount <= 1 && (
                  <Link href="/models" className="hover:underline">
                    Configure outros provedores em {MODEL_CREDENTIALS_LABEL}
                  </Link>
                )}
              </>
            }
          >
            {/* Enquanto a lista real não chega, o seletor mostra só o modelo salvo: nada de
                apresentar a lista fixa como se fosse a dos provedores — nem de esconder o que está valendo. */}
            {loading ? (
              <Select id={`${id}-model`} value={values.modelId} disabled aria-busy>
                <option value={values.modelId}>{current?.label ?? values.modelId}</option>
              </Select>
            ) : (
              <Select id={`${id}-model`} value={values.modelId} onChange={(e) => onModelChange(e.target.value)}>
                {Object.entries(groupByProvider(models, modelProviders)).map(([providerLabel, options]) => (
                  <optgroup key={providerLabel} label={providerLabel}>
                    {options.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label}
                        {m.channel === "preview" ? " · preview" : m.channel === "alias" ? " · alias" : ""}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </Select>
            )}
          </Field>
          <Field
            label="Credencial"
            htmlFor={`${id}-credential`}
            hint={
              credentials.length === 0 ? (
                <Link href="/models" className="hover:underline">
                  Nenhuma credencial cadastrada para este provedor — cadastre em {MODEL_CREDENTIALS_LABEL}
                </Link>
              ) : (
                "Mesmo provedor, credenciais diferentes — útil pra separar clientes/times."
              )
            }
          >
            <Select
              id={`${id}-credential`}
              value={values.credentialId}
              onChange={(e) => update("credentialId", e.target.value)}
              disabled={credentials.length === 0 && !values.credentialId}
            >
              <option value="">Padrão do provedor</option>
              {credentials.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                  {c.key_hint ? ` (${c.key_hint})` : ""}
                </option>
              ))}
              {/* A credencial salva some da lista se foi desabilitada: continua visível, para não parecer trocada. */}
              {values.credentialId && !credentials.some((c) => c.id === values.credentialId) && (
                <option value={values.credentialId}>Credencial salva (indisponível)</option>
              )}
            </Select>
          </Field>
          <Field
            label="Base de conhecimento"
            htmlFor={`${id}-knowledge`}
            hint={
              collections.length === 0 ? (
                <Link href="/knowledge" className="hover:underline">
                  Crie uma coleção em Base de conhecimento
                </Link>
              ) : (
                "O agente ganha uma busca na coleção e decide sozinho quando consultá-la."
              )
            }
          >
            <Select
              id={`${id}-knowledge`}
              value={values.knowledgeCollection}
              onChange={(e) => update("knowledgeCollection", e.target.value)}
              disabled={collections.length === 0 && !values.knowledgeCollection}
            >
              <option value="">Sem base de conhecimento</option>
              {collections.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.label}
                </option>
              ))}
              {values.knowledgeCollection && !collections.some((c) => c.name === values.knowledgeCollection) && (
                <option value={values.knowledgeCollection}>{values.knowledgeCollection}</option>
              )}
            </Select>
          </Field>
        </div>
        <Field
          label="Parâmetros de geração"
          error={fieldError("modelParams")}
          hint="Vazio = padrão do provedor. Mudar qualquer um gera uma nova versão da configuração."
        >
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 2xl:grid-cols-5">
            <ParamInput
              id={`${id}-temperature`}
              label="Temperatura"
              placeholder="0–2"
              step="0.1"
              value={values.temperature}
              onChange={(v) => update("temperature", v)}
            />
            <ParamInput
              id={`${id}-top-p`}
              label="top_p"
              placeholder="0–1"
              step="0.05"
              value={values.topP}
              onChange={(v) => update("topP", v)}
            />
            <ParamInput
              id={`${id}-max-tokens`}
              label="Máx. tokens"
              placeholder="padrão"
              value={values.maxTokens}
              onChange={(v) => update("maxTokens", v)}
            />
            {(provider === "google" || provider === "openai") && (
              <label
                htmlFor={`${id}-reasoning`}
                className="flex flex-col gap-1 text-xs text-muted-foreground"
                title="Quanto o modelo pensa antes de responder. Baixo mantém o planejamento das tools com menos latência e custo; desligado só para respostas diretas."
              >
                Raciocínio
                <Select
                  id={`${id}-reasoning`}
                  value={values.reasoning}
                  onChange={(e) => update("reasoning", e.target.value as FormValues["reasoning"])}
                  className="h-8"
                >
                  {REASONING_LEVELS.map((level) => (
                    <option key={level.value} value={level.value}>
                      {level.label}
                    </option>
                  ))}
                </Select>
              </label>
            )}
            {provider === "anthropic" && (
              <label
                htmlFor={`${id}-prompt-cache`}
                className="flex flex-col gap-1 text-xs text-muted-foreground"
                title="Guarda as instructions e as tools no cache do Claude: as chamadas seguintes pagam só uma fração desse trecho. 1h compensa quando as conversas são espaçadas. Gemini e OpenAI fazem isso sozinhos."
              >
                Cache do prompt
                <Select
                  id={`${id}-prompt-cache`}
                  value={values.promptCache}
                  onChange={(e) => update("promptCache", e.target.value as FormValues["promptCache"])}
                  className="h-8"
                >
                  <option value="">Desligado</option>
                  <option value="5m">5 minutos</option>
                  <option value="1h">1 hora</option>
                </Select>
              </label>
            )}
            <ParamInput
              id={`${id}-timeout`}
              label="Tempo limite (s)"
              placeholder="padrão"
              title="Passou disso, a execução para e a API responde 504 — quem chama cai no próprio fallback."
              value={values.timeoutSeconds}
              onChange={(v) => update("timeoutSeconds", v)}
            />
          </div>
        </Field>
      </div>
    </FormSection>
  );
}
