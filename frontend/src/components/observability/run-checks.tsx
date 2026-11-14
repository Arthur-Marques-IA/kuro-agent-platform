import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { SectionHeading } from "@/components/ui/primitives";
import type { RunCheck } from "@/lib/types";

const show = (value: unknown) => (typeof value === "string" ? JSON.stringify(value) : JSON.stringify(value ?? null));

/** Quantas checagens reprovaram, para o selo das listas (`null` quando nenhuma). */
export function ChecksBadge({ checks }: { checks?: RunCheck[] }) {
  const failed = (checks ?? []).filter((c) => c.status === "failed");
  if (failed.length === 0) return null;
  const hard = failed.some((c) => c.severity === "fail");
  return (
    <Badge variant={hard ? "destructive" : "warning"} title={failed.map((c) => c.name).join(", ")}>
      {failed.length} {failed.length === 1 ? "checagem reprovou" : "checagens reprovaram"}
    </Badge>
  );
}

function CheckStatus({ check }: { check: RunCheck }) {
  if (check.status === "passed") return <Badge variant="success">passou</Badge>;
  if (check.status === "error") return <Badge variant="outline">erro de avaliação</Badge>;
  return <Badge variant={check.severity === "fail" ? "destructive" : "warning"}>reprovou</Badge>;
}

/** O detalhe de cada checagem avaliada no run: a regra, o valor esperado e o que veio. */
export function ChecksPanel({ checks }: { checks: RunCheck[] }) {
  if (checks.length === 0) return null;
  return (
    <section className="flex flex-col gap-3">
      <SectionHeading
        title="Checagens"
        description="Regras determinísticas do agente, avaliadas ao fim deste run. Não mudam o status do run."
      />
      <Card className="divide-y divide-border overflow-hidden">
        {checks.map((c) => (
          <div key={c.name} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-sm">
            <span className="font-medium">{c.name}</span>
            <CheckStatus check={c} />
            <Badge variant="outline">{c.severity === "fail" ? "falha" : "aviso"}</Badge>
            <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground">
              {c.status === "error"
                ? c.error
                : `${c.field} ${c.op} ${show(c.expected)} · veio ${show(c.actual)}`}
            </span>
          </div>
        ))}
      </Card>
    </section>
  );
}
