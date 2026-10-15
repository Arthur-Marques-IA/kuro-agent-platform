"use client";

import { useEffect, useMemo, useState } from "react";
import { Play } from "lucide-react";
import { errorMessage, requestJson } from "@/lib/http";
import { parseDependencies } from "@/lib/agent-meta";
import type { ApiToolParam, ToolDetail, ToolInvokeResult, ToolSummary } from "@/lib/types";
import { fieldsFromToolParams } from "@/lib/dependencies-form";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { Dialog, DialogBody, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, Spinner } from "@/components/ui/primitives";
import { Select } from "@/components/ui/select";
import { DependenciesForm } from "@/components/schema/dependencies-form";
import { DryRunToggle, useDryRun } from "@/components/tools/dry-run-toggle";
import { TOOL_FAILURE_META } from "@/lib/tool-failures";

/** Roda uma tool uma vez, fora de qualquer agente — pra conferir se ela está configurada certo. */
export function ToolInvokeDialog({ tool, onOpenChange }: { tool: ToolSummary; onOpenChange: (open: boolean) => void }) {
  const [detail, setDetail] = useState<ToolDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [functionName, setFunctionName] = useState("");
  const [argumentsText, setArgumentsText] = useState("");
  const [dependenciesText, setDependenciesText] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ToolInvokeResult | null>(null);
  const [dryRun, setDryRun] = useDryRun();
  // Builtin não faz chamada HTTP nossa: não há a quem avisar.
  const avisaDryRun = tool.kind === "api" || tool.kind === "python";

  useEffect(() => {
    let cancelled = false;
    requestJson<ToolDetail>(`/api/tools/${encodeURIComponent(tool.tool_name)}`, { fallbackError: "Falha ao carregar a tool" })
      .then((data) => {
        if (cancelled) return;
        setDetail(data);
        if (data.functions?.length) setFunctionName(data.functions[0]);
      })
      .catch((err) => !cancelled && setLoadError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [tool.tool_name]);

  const argumentsCheck = parseDependencies(argumentsText);
  const dependenciesCheck = parseDependencies(dependenciesText);
  // Tool `kind="api"` declara os parâmetros: os de origem "modelo" viram o formulário dos argumentos e os de
  // origem "dependência" o do `dependencies` (no /chat quem os preenche é o servidor, a partir do `dependencies`
  // da requisição). Builtin e python não descrevem os argumentos aqui: o formulário cai no chave/valor.
  const { args: argFields, deps: depFields } = useMemo(
    () =>
      fieldsFromToolParams(
        tool.kind === "api" ? (detail?.config as { parameters?: ApiToolParam[] } | undefined)?.parameters : undefined
      ),
    [detail, tool.kind]
  );
  const usaDependencies = depFields.length > 0;

  async function run() {
    if (argumentsCheck.error || dependenciesCheck.error) return;
    setRunning(true);
    setResult(null);
    try {
      const response = await requestJson<ToolInvokeResult>(`/api/tools/${encodeURIComponent(tool.tool_name)}/invoke`, {
        method: "POST",
        json: {
          arguments: argumentsCheck.value ?? {},
          function_name: tool.kind === "builtin" ? functionName || null : null,
          dependencies: dependenciesCheck.value ?? {},
          dry_run: avisaDryRun && dryRun,
        },
        fallbackError: "Falha ao testar a tool",
      });
      setResult(response);
    } catch (err) {
      setResult({ ok: false, error: errorMessage(err) });
    } finally {
      setRunning(false);
    }
  }

  return (
    <Dialog open onOpenChange={onOpenChange} size="md">
      <DialogHeader>
        <DialogTitle>Testar {tool.label}</DialogTitle>
        <DialogDescription>Roda a tool uma vez com os argumentos abaixo, sem passar por um agente.</DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-4">
        {loadError && <p className="text-[13px] text-destructive">{loadError}</p>}
        {!detail && !loadError && (
          <p className="flex items-center gap-2 text-[13px] text-muted-foreground">
            <Spinner /> Carregando...
          </p>
        )}
        {detail?.build_error && (
          <p className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-[13px] text-destructive">
            {detail.build_error}
          </p>
        )}

        {tool.kind === "builtin" && detail?.functions && (
          <Field label="Função" htmlFor="invoke-function" hint="Uma toolkit padrão tem várias — escolha qual testar.">
            <Select id="invoke-function" value={functionName} onChange={(e) => setFunctionName(e.target.value)}>
              {detail.functions.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </Select>
          </Field>
        )}

        {usaDependencies && (
          <Field
            label="Dependencies"
            error={dependenciesCheck.error}
            hint="Simula o dependencies do /chat — é daqui que saem os parâmetros com origem 'dependência'."
          >
            <DependenciesForm
              fields={depFields}
              value={dependenciesText}
              onChange={setDependenciesText}
              invalid={Boolean(dependenciesCheck.error)}
              jsonRows={3}
              placeholder='{"cpf": "000.000.000-00"}'
            />
          </Field>
        )}

        {detail && (
          <Field
            label="Argumentos"
            error={argumentsCheck.error}
            hint={argFields.length === 0 ? "O que o modelo mandaria na chamada — chave/valor ou JSON." : undefined}
          >
            <DependenciesForm
              fields={argFields}
              value={argumentsText}
              onChange={setArgumentsText}
              invalid={Boolean(argumentsCheck.error)}
              jsonRows={5}
              placeholder='{"a": 1, "b": 2}'
              label="argumentos"
            />
          </Field>
        )}

        {avisaDryRun && <DryRunToggle checked={dryRun} onChange={setDryRun} />}

        {result && (
          <div className="flex flex-col gap-2">
            <p className={`text-[13px] font-medium ${result.ok ? "text-success" : "text-destructive"}`}>
              {result.ok
                ? result.failure
                  ? `Respondeu ${TOOL_FAILURE_META[result.failure].label.toLowerCase()}${result.http_status ? ` (HTTP ${result.http_status})` : ""} — não conta como falha`
                  : "Sucesso"
                : result.failure
                  ? `Falhou — ${TOOL_FAILURE_META[result.failure].label}${result.http_status ? ` (HTTP ${result.http_status})` : ""}`
                  : "Erro"}
            </p>
            {result.failure && <p className="text-xs text-muted-foreground">{TOOL_FAILURE_META[result.failure].action}</p>}
            <CodeBlock
              title={result.ok ? "result" : "error"}
              code={result.ok ? formatResult(result.result) : (result.error ?? "")}
            />
          </div>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={() => onOpenChange(false)}>
          Fechar
        </Button>
        <Button onClick={() => void run()} disabled={running || !detail || Boolean(argumentsCheck.error) || Boolean(dependenciesCheck.error)}>
          {running ? <Spinner /> : <Play />}
          Rodar
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

function formatResult(result: unknown): string {
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result, null, 2);
  } catch {
    return String(result);
  }
}
