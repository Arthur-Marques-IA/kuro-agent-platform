"use client";

import { useEffect, useMemo, useState } from "react";
import { ExternalLink } from "lucide-react";
import { buildSnippet, SNIPPET_LANGUAGES, type ChatEndpoint, type SnippetLanguage } from "@/lib/snippets";
import { useLocalStorage } from "@/lib/use-local-storage";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { CodeBlock } from "@/components/ui/code-block";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { requestJson } from "@/lib/http";
import { Skeleton } from "@/components/ui/primitives";
import type { IntegrationContract } from "@/lib/types";

/**
 * Exemplo de chamada ao contrato estável, montado a partir de
 * `GET /agents/{tipo}/integration`.
 *
 * O exemplo era montado aqui, no cliente, com dependências inventadas e sempre
 * apontando para `/chat`. Isso dava exemplo errado para agente analista (que é
 * `/analyze` com `document`) e sem o header de autenticação quando as chaves
 * estão ligadas — um cURL que não funciona é pior que exemplo nenhum, porque
 * manda quem integra procurar no lugar errado.
 *
 * Pelo mesmo motivo a referência (campos, eventos do stream, resposta e erros) também
 * vem do contrato: a tabela escrita à mão aqui ficou sem `metadata`, `dry_run`,
 * `attachments` e metade dos eventos do SSE. E a Base URL começa pela que o backend
 * conhece (domínio, profile `ip`), não por um `localhost` fixo.
 */
export function IntegrationPanel({
  agentType,
  userId = "user-123",
  sessionId = "session-abc",
  dependencies,
  showReference = false,
}: {
  agentType: string;
  userId?: string;
  sessionId?: string;
  dependencies?: Record<string, unknown> | null;
  showReference?: boolean;
}) {
  const hasRealDependencies = Boolean(dependencies && Object.keys(dependencies).length > 0);
  const [language, setLanguage] = useLocalStorage<SnippetLanguage>("agent-service:snippet-language", "curl");
  const [includeDependencies, setIncludeDependencies] = useState(true);
  /** `null` até a pessoa editar: aí quem decide a URL é o backend. */
  const [baseUrlOverride, setBaseUrlOverride] = useState<string | null>(null);
  const [contract, setContract] = useState<IntegrationContract | null>(null);
  const [erro, setErro] = useState<string | null>(null);

  const analysis = contract?.kind === "analysis";
  const [streaming, setStreaming] = useState(true);
  const endpoint: ChatEndpoint = analysis ? "/analyze" : streaming ? "/chat/stream" : "/chat";
  const baseUrl = (baseUrlOverride ?? contract?.base_url ?? "").replace(/\/+$/, "");

  useEffect(() => {
    let vivo = true;
    const query = baseUrlOverride?.trim() ? `?base_url=${encodeURIComponent(baseUrlOverride.trim())}` : "";
    requestJson<IntegrationContract>(`/api/agents/${encodeURIComponent(agentType)}/integration${query}`, {
      fallbackError: "Falha ao ler o contrato de integração",
    })
      .then((data) => vivo && setContract(data))
      .catch((err) => vivo && setErro(String(err)));
    return () => {
      vivo = false;
    };
  }, [agentType, baseUrlOverride]);

  /** O corpo vem do backend; só a sessão real da tela (quando há uma) entra por cima. */
  const payload = useMemo(() => {
    if (!contract) return null;
    const body: Record<string, unknown> = { ...contract.request_example };
    if (contract.kind !== "analysis") {
      body.user_id = userId;
      body.session_id = sessionId;
    }
    if (hasRealDependencies) body.dependencies = dependencies;
    if (!includeDependencies) delete body.dependencies;
    return body;
  }, [contract, dependencies, hasRealDependencies, includeDependencies, sessionId, userId]);

  const temDependencies = (contract?.dependencies.length ?? 0) > 0 || hasRealDependencies;
  const requiresAuth = contract?.curl.includes("Authorization") ?? false;

  const code = contract && payload ? buildSnippet(language, { baseUrl, endpoint, payload, requiresAuth }) : "";
  const languageLabel = SNIPPET_LANGUAGES.find((l) => l.value === language)?.label ?? language;

  if (erro) {
    return <p className="text-[13px] text-destructive">{erro}</p>;
  }
  if (!contract) {
    return <Skeleton className="h-64" />;
  }

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          {analysis ? (
            <span className="rounded-full bg-muted px-3 py-1 font-mono text-xs">POST /analyze</span>
          ) : (
            <Tabs
              value={streaming ? "stream" : "single"}
              onValueChange={(v) => setStreaming(v === "stream")}
              variant="pill"
            >
              <TabsList>
                <TabsTrigger value="stream">Streaming (SSE)</TabsTrigger>
                <TabsTrigger value="single">Resposta única</TabsTrigger>
              </TabsList>
            </Tabs>
          )}
          <Tabs value={language} onValueChange={(v) => setLanguage(v as SnippetLanguage)} variant="pill">
            <TabsList>
              {SNIPPET_LANGUAGES.map((l) => (
                <TabsTrigger key={l.value} value={l.value}>
                  {l.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          <label className="flex min-w-0 flex-1 items-center gap-2 text-[13px]">
            <span className="shrink-0 text-muted-foreground">Base URL</span>
            <Input
              value={baseUrlOverride ?? contract.base_url}
              onChange={(e) => setBaseUrlOverride(e.target.value.trim() ? e.target.value : null)}
              className="h-8 max-w-xs font-mono text-xs"
              spellCheck={false}
            />
          </label>
          {temDependencies && (
            <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
              <input
                type="checkbox"
                checked={includeDependencies}
                onChange={(e) => setIncludeDependencies(e.target.checked)}
                className="accent-primary"
              />
              Incluir <code className="font-mono text-xs text-foreground">dependencies</code>
              {!hasRealDependencies && " (exemplo)"}
            </label>
          )}
        </div>
        {baseUrlOverride === null && contract.base_url_source === "local" && (
          <p className="text-xs text-muted-foreground">
            O serviço só escuta nesta máquina, então outro servidor não alcança esta URL. Para expor, defina{" "}
            <code className="font-mono">KURO_API_DOMAIN</code> (sem domínio próprio:{" "}
            <code className="font-mono">&lt;ip-com-hífens&gt;.sslip.io</code>, profile <code className="font-mono">tls</code>)
            ou <code className="font-mono">KURO_PUBLIC_URL</code>; ou integre pela rede Docker.
          </p>
        )}
        <CodeBlock code={code} title={`POST ${endpoint} · ${languageLabel}`} />
      </div>

      {contract.warnings.length > 0 && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-[13px] text-destructive">
          {contract.warnings.map((w) => (
            <p key={w}>{w}</p>
          ))}
        </div>
      )}

      {contract.kind === "procedural" && (
        <div className="flex flex-col gap-2 rounded-lg border border-border p-4 text-[13px]">
          <h3 className="text-sm font-semibold">Agente procedural: a resposta traz o estado</h3>
          <p className="text-muted-foreground">
            Além do texto, o <code className="font-mono text-xs">/chat</code> devolve{" "}
            <code className="font-mono text-xs">state</code> (no stream, o evento{" "}
            <code className="font-mono text-xs">state</code>, antes do <code className="font-mono text-xs">done</code>
            ): a etapa atual, o que foi coletado, o que falta e, quando{" "}
            <code className="font-mono text-xs">state.done</code> for verdadeiro, o resultado em{" "}
            <code className="font-mono text-xs">state.result</code>. Não é preciso interpretar o texto para saber que
            acabou.
          </p>
          <p className="text-muted-foreground">
            Mensagens da mesma sessão são processadas uma de cada vez: a segunda recebe{" "}
            <code className="font-mono text-xs">409</code> enquanto a primeira não termina. Espere a resposta e reenvie.
          </p>
          {contract.state_url && (
            <p className="text-muted-foreground">
              Estado de uma conversa: <code className="break-all font-mono text-xs">GET {contract.state_url}</code>
            </p>
          )}
        </div>
      )}

      {contract.dependencies.length > 0 && (
        <div className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold">Campos que este agente espera</h3>
          <div className="overflow-hidden rounded-lg border border-border">
            <table className="w-full text-[13px]">
              <tbody className="divide-y divide-border">
                {contract.dependencies.map((d) => (
                  <tr key={d.name}>
                    <td className="px-3 py-2 font-mono text-xs">{d.name}</td>
                    <td className="px-3 py-2 text-muted-foreground">{d.type}</td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {d.required ? "obrigatório" : "opcional"}
                      {d.required_by_tools.length > 0 && ` · usado por ${d.required_by_tools.join(", ")}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {showReference && <ApiReference contract={contract} />}
    </div>
  );
}

function ApiReference({ contract }: { contract: IntegrationContract }) {
  const analysis = contract.kind === "analysis";
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h3 className="text-sm font-semibold">Corpo da requisição</h3>
            <p className="text-[13px] text-muted-foreground">
              {analysis ? (
                <>
                  <code className="font-mono text-xs">POST /analyze</code>: one-shot, sem histórico.
                </>
              ) : (
                <>
                  O mesmo JSON para <code className="font-mono text-xs">/chat</code> e{" "}
                  <code className="font-mono text-xs">/chat/stream</code>.
                </>
              )}
            </p>
          </div>
          <a
            href="/api/integration/openapi"
            target="_blank"
            rel="noreferrer"
            title={`Quem integra lê em ${contract.openapi_url}, com a chave runtime`}
            className="inline-flex shrink-0 items-center gap-1.5 text-[13px] text-primary hover:underline"
          >
            OpenAPI <ExternalLink className="size-3.5" />
          </a>
        </div>
        <Card className="overflow-hidden">
          <div className="scrollbar-thin overflow-x-auto">
            <table className="w-full min-w-[560px] text-left text-[13px]">
              <thead className="border-b border-border bg-surface text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-2.5 font-medium">Campo</th>
                  <th className="px-4 py-2.5 font-medium">Tipo</th>
                  <th className="px-4 py-2.5 font-medium">Descrição</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {contract.request_fields.map((param) => (
                  <tr key={param.name} className="align-top">
                    <td className="whitespace-nowrap px-4 py-3">
                      <code className="font-mono text-xs font-semibold">{param.name}</code>
                      <div className="mt-1">
                        {param.required ? <Badge>obrigatório</Badge> : <Badge variant="secondary">opcional</Badge>}
                      </div>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-muted-foreground">{param.type}</td>
                    <td className="px-4 py-3 text-muted-foreground">{param.description}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        <p className="text-xs text-muted-foreground">
          Quem integra lê o contrato em <code className="break-all font-mono">{contract.openapi_url}</code> com a chave
          runtime (o <code className="font-mono">/docs</code> exige a chave admin).
        </p>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="flex min-w-0 flex-col gap-2 p-5">
          <p className="font-mono text-xs font-semibold">Resposta ({analysis ? "POST /analyze" : "POST /chat"})</p>
          <p className="text-[13px] text-muted-foreground">
            Guarde <code className="font-mono text-xs">run_id</code>,{" "}
            <code className="font-mono text-xs">agent_version</code> e{" "}
            <code className="font-mono text-xs">config_hash</code> junto da decisão: é o que diz qual configuração
            respondeu. Os valores abaixo são ilustrativos.
          </p>
          <CodeBlock code={JSON.stringify(contract.response_example, null, 2)} title="JSON" />
        </Card>
        {contract.stream_events.length > 0 && (
          <Card className="flex flex-col gap-2 p-5">
            <p className="font-mono text-xs font-semibold">POST /chat/stream</p>
            <ul className="flex flex-col gap-1 text-[13px] text-muted-foreground">
              {contract.stream_events.map((e) => (
                <li key={e.event}>
                  <code className="font-mono text-xs text-foreground">event: {e.event}</code> — {e.data}
                </li>
              ))}
            </ul>
            <p className="text-xs text-muted-foreground">
              O status HTTP do stream sai 200 antes de o agente rodar: falhas chegam como{" "}
              <code className="font-mono">event: error</code>. É POST: consuma com{" "}
              <code className="font-mono">fetch</code> lendo o stream, não com <code className="font-mono">EventSource</code>.
            </p>
          </Card>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">Erros e fallback</h3>
        <Card className="overflow-hidden">
          <div className="scrollbar-thin overflow-x-auto">
            <table className="w-full min-w-[560px] text-left text-[13px]">
              <thead className="border-b border-border bg-surface text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                  <th className="px-4 py-2.5 font-medium">Significa</th>
                  <th className="px-4 py-2.5 font-medium">O que fazer</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {contract.errors.map((e) => (
                  <tr key={e.status} className="align-top">
                    <td className="whitespace-nowrap px-4 py-3 font-mono text-xs">{e.status}</td>
                    <td className="px-4 py-3 text-muted-foreground">{e.meaning}</td>
                    <td className="px-4 py-3 text-muted-foreground">{e.action}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  );
}

