"use client";

import { useEffect, useState } from "react";
import { Check, CircleAlert, Copy, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/primitives";
import { SidebarTrigger } from "@/components/workspace/app-shell";
import { CONSOLE_BUILD } from "@/lib/service-compat";

/**
 * Falha inesperada numa tela: a sidebar continua de pé e dá para tentar de novo. Quem opera lê
 * uma explicação em português; a mensagem crua do JavaScript fica recolhida, com botão de copiar
 * (para colar num chamado).
 */
export default function WorkspaceError({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  const [copied, setCopied] = useState<"ok" | "fail" | null>(null);

  useEffect(() => {
    console.error(error);
  }, [error]);

  const details = [
    `mensagem: ${error.message || "(vazia)"}`,
    error.digest ? `ref: ${error.digest}` : null,
    `console: ${CONSOLE_BUILD}`,
    error.stack ? `\n${error.stack}` : null,
  ]
    .filter(Boolean)
    .join("\n");

  async function copy() {
    try {
      // Sem HTTPS (console acessado por IP), o navegador não expõe a área de transferência.
      await navigator.clipboard.writeText(`tela: ${window.location.pathname}\n${details}`);
      setCopied("ok");
    } catch {
      setCopied("fail");
    }
    window.setTimeout(() => setCopied(null), 2500);
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-14 shrink-0 items-center border-b border-border px-3 lg:hidden">
        <SidebarTrigger />
      </div>
      <EmptyState
        className="flex-1"
        icon={CircleAlert}
        title="Algo deu errado nesta tela"
        description="O console não conseguiu mostrar esta tela. Tente de novo; se continuar, confira no rodapé da barra lateral se as versões do console e do serviço são compatíveis — atualizar uma imagem sem a outra costuma causar isso."
        action={
          <div className="flex w-full max-w-xl flex-col items-center gap-3">
            <Button variant="outline" onClick={() => retry()}>
              <RotateCcw /> Tentar de novo
            </Button>
            <details className="w-full text-left">
              <summary className="cursor-pointer text-center text-xs text-muted-foreground hover:text-foreground">
                Detalhe técnico
              </summary>
              <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/50 p-2 font-mono text-[11px]">
                {details}
              </pre>
              <Button variant="ghost" size="sm" className="mt-1" onClick={copy}>
                {copied === "ok" ? <Check /> : <Copy />}
                {copied === "ok"
                  ? "Copiado"
                  : copied === "fail"
                    ? "Não deu para copiar — selecione o texto acima"
                    : "Copiar detalhe"}
              </Button>
            </details>
          </div>
        }
      />
    </div>
  );
}
