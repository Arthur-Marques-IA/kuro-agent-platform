"use client";

import { useState } from "react";
import { ArrowDown, ArrowUp, Plus, Trash } from "lucide-react";
import type {
  DependencyFieldType,
  ProcedureField,
  ProcedureStage,
  ProcedureStageType,
  ToolSummary,
} from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { STAGE_TYPE_META } from "@/components/chat/procedure-progress";

const FIELD_TYPES: DependencyFieldType[] = ["string", "integer", "number", "boolean"];
const STAGE_ID = /^[a-z0-9][a-z0-9_-]*$/;
const FIELD_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const RESERVED = new Set(["confirmacao", "idempotency_key", "dry_run"]);

function emptyField(): ProcedureField {
  return { name: "", type: "string", label: "", description: "", required: true, default: null };
}

function newStage(type: ProcedureStageType, stages: ProcedureStage[]): ProcedureStage {
  const base = { collect: "coleta", confirm: "confirmacao", action: "acao" }[type];
  let id = base;
  for (let n = 2; stages.some((s) => s.id === id); n++) id = `${base}-${n}`;
  if (type === "collect") return { id, type, goal: "", fields: [emptyField()] };
  if (type === "action") return { id, type, goal: "", tool: "" };
  return { id, type, goal: "" };
}

/** Ponto de partida de um agente procedural novo: coletar, confirmar. */
export function starterStages(): ProcedureStage[] {
  return [
    {
      id: "identificacao",
      type: "collect",
      goal: "Identificar o cliente",
      fields: [{ ...emptyField(), name: "nome", label: "Nome" }],
    },
    { id: "confirmacao", type: "confirm", goal: "Confira os dados antes de continuar:" },
  ];
}

/**
 * O que o backend recusaria com 422 (`agents/procedural.py::validate_stages`) —
 * conferido aqui para o erro aparecer no campo, antes de salvar. As tools das
 * etapas de ação são conferidas só no servidor.
 */
export function stagesProblem(stages: ProcedureStage[]): string | null {
  if (stages.length === 0) return "Um agente procedural precisa de ao menos uma etapa.";
  const ids = new Set<string>();
  const owners = new Map<string, string>();
  let collected = false;
  let confirmedSinceAction = false;
  for (const [i, stage] of stages.entries()) {
    const where = `Etapa ${i + 1}`;
    if (!STAGE_ID.test(stage.id)) return `${where}: o id precisa ser um slug (a-z, 0-9, '-' ou '_').`;
    if (ids.has(stage.id)) return `${where}: o id "${stage.id}" se repete.`;
    ids.add(stage.id);
    if (stage.type === "collect") {
      const fields = stage.fields ?? [];
      if (fields.length === 0) return `${where}: uma coleta precisa de ao menos um campo.`;
      for (const field of fields) {
        if (!FIELD_NAME.test(field.name)) return `${where}: nome de campo inválido "${field.name}".`;
        if (RESERVED.has(field.name)) return `${where}: "${field.name}" é um nome reservado.`;
        const owner = owners.get(field.name);
        if (owner) return `O campo "${field.name}" aparece nas etapas "${owner}" e "${stage.id}" — cada campo é de uma etapa só.`;
        owners.set(field.name, stage.id);
        if (field.pattern) {
          if (field.type !== "string") return `${where}: o formato (regex) de "${field.name}" só vale para texto.`;
          try {
            new RegExp(field.pattern);
          } catch {
            return `${where}: o formato de "${field.name}" não é uma regex válida.`;
          }
        }
      }
      if (!fields.some((f) => f.required)) return `${where}: marque ao menos um campo como obrigatório.`;
      collected = true;
    } else if (stage.type === "confirm") {
      if (!collected) return `${where}: a confirmação precisa vir depois de uma etapa de coleta.`;
      confirmedSinceAction = true;
    } else {
      if (!stage.tool) return `${where}: escolha a tool que a ação executa.`;
      if (!confirmedSinceAction) return `${where}: uma ação precisa de uma confirmação antes dela.`;
      confirmedSinceAction = false;
    }
  }
  return null;
}

export function StagesEditor({
  stages,
  onChange,
  tools,
}: {
  stages: ProcedureStage[];
  onChange: (stages: ProcedureStage[]) => void;
  tools: ToolSummary[];
}) {
  function patch(index: number, change: Partial<ProcedureStage>) {
    onChange(stages.map((s, i) => (i === index ? { ...s, ...change } : s)));
  }

  function move(index: number, delta: number) {
    const next = [...stages];
    const [item] = next.splice(index, 1);
    next.splice(index + delta, 0, item);
    onChange(next);
  }

  return (
    <div className="flex flex-col gap-3">
      {stages.map((stage, index) => {
        const Icon = STAGE_TYPE_META[stage.type].icon;
        return (
          <div key={index} className="rounded-lg border border-border">
            <div className="flex flex-wrap items-center gap-2 border-b border-border bg-muted/40 px-3 py-2">
              <span className="flex size-6 items-center justify-center rounded-full bg-background text-xs font-medium">
                {index + 1}
              </span>
              <Badge variant="outline" className="gap-1">
                <Icon className="size-3" /> {STAGE_TYPE_META[stage.type].label}
              </Badge>
              <Input
                aria-label={`Id da etapa ${index + 1}`}
                value={stage.id}
                onChange={(e) => patch(index, { id: e.target.value })}
                className="h-7 w-40 font-mono text-xs"
                spellCheck={false}
              />
              <div className="ml-auto flex items-center gap-0.5">
                <Button variant="ghost" size="icon-sm" aria-label="Subir etapa" disabled={index === 0} onClick={() => move(index, -1)}>
                  <ArrowUp />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Descer etapa"
                  disabled={index === stages.length - 1}
                  onClick={() => move(index, 1)}
                >
                  <ArrowDown />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Remover etapa"
                  onClick={() => onChange(stages.filter((_, i) => i !== index))}
                >
                  <Trash />
                </Button>
              </div>
            </div>
            <div className="flex flex-col gap-3 p-3">
              <Input
                aria-label={`Objetivo da etapa ${index + 1}`}
                value={stage.goal}
                onChange={(e) => patch(index, { goal: e.target.value })}
                placeholder={
                  stage.type === "collect"
                    ? "Objetivo — ex.: Obter o CPF e o nome do cliente"
                    : stage.type === "confirm"
                      ? "Frase que abre a confirmação — ex.: Confira os dados do chamado:"
                      : "O que a ação faz — ex.: Abrir o chamado"
                }
                className="text-[13px]"
              />
              {stage.type === "collect" && (
                <StageFieldsEditor fields={stage.fields ?? []} onChange={(fields) => patch(index, { fields })} />
              )}
              {stage.type === "confirm" && (
                <p className="text-xs text-muted-foreground">
                  O servidor mostra os dados coletados até aqui e espera um sim — a mensagem é montada dos dados, sem o
                  modelo. Corrigir um dado depois de confirmar pede a confirmação de novo.
                </p>
              )}
              {stage.type === "action" && (
                <ActionEditor stage={stage} tools={tools} onChange={(change) => patch(index, change)} />
              )}
            </div>
          </div>
        );
      })}
      <div className="flex flex-wrap gap-2">
        {(["collect", "confirm", "action"] as ProcedureStageType[]).map((type) => (
          <Button key={type} variant="outline" size="sm" onClick={() => onChange([...stages, newStage(type, stages)])}>
            <Plus /> {STAGE_TYPE_META[type].label}
          </Button>
        ))}
      </div>
    </div>
  );
}

function StageFieldsEditor({ fields, onChange }: { fields: ProcedureField[]; onChange: (fields: ProcedureField[]) => void }) {
  function update(index: number, change: Partial<ProcedureField>) {
    onChange(fields.map((f, i) => (i === index ? { ...f, ...change } : f)));
  }
  return (
    <div className="flex flex-col gap-2">
      {fields.map((field, i) => (
        <div key={i} className="flex flex-col gap-1.5 rounded-md border border-dashed border-border p-2">
          <div className="grid grid-cols-[1fr_1fr_100px_auto_auto] items-center gap-1.5">
            <Input
              placeholder="nome (ex.: cpf)"
              aria-label="Nome do campo"
              value={field.name}
              onChange={(e) => update(i, { name: e.target.value })}
              className="h-8 font-mono text-xs"
              spellCheck={false}
            />
            <Input
              placeholder="rótulo (ex.: CPF)"
              aria-label="Rótulo do campo"
              value={field.label}
              onChange={(e) => update(i, { label: e.target.value })}
              className="h-8 text-xs"
            />
            <Select
              aria-label="Tipo do campo"
              value={field.type}
              onChange={(e) => {
                const type = e.target.value as DependencyFieldType;
                update(i, { type, enum: null, ...(type !== "string" ? { pattern: null } : {}) });
              }}
              className="h-8"
            >
              {FIELD_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Select>
            <label className="flex items-center gap-1 text-xs text-muted-foreground" title="Obrigatório">
              <input
                type="checkbox"
                checked={field.required}
                onChange={(e) => update(i, { required: e.target.checked })}
                className="accent-primary"
              />
              obrig.
            </label>
            <Button variant="ghost" size="icon-sm" aria-label="Remover campo" onClick={() => onChange(fields.filter((_, idx) => idx !== i))}>
              <Trash />
            </Button>
          </div>
          <div className="grid gap-1.5 sm:grid-cols-[1fr_1fr]">
            <Input
              placeholder="descrição para o modelo (opcional)"
              aria-label="Descrição do campo"
              value={field.description}
              onChange={(e) => update(i, { description: e.target.value })}
              className="h-8 text-xs"
            />
            {field.type === "boolean" ? (
              <span />
            ) : (
              <div className="grid grid-cols-2 gap-1.5">
                <EnumInput field={field} onChange={(values) => update(i, { enum: values })} />
                <Input
                  placeholder="formato (regex)"
                  aria-label="Formato do campo (regex)"
                  title="Só texto. Ex.: ^\d{11}$ para um CPF só com números."
                  value={field.pattern ?? ""}
                  disabled={field.type !== "string"}
                  onChange={(e) => update(i, { pattern: e.target.value || null })}
                  className="h-8 font-mono text-xs"
                  spellCheck={false}
                />
              </div>
            )}
          </div>
        </div>
      ))}
      <Button variant="ghost" size="sm" className="w-fit" onClick={() => onChange([...fields, emptyField()])}>
        <Plus /> Campo
      </Button>
    </div>
  );
}

/** Lista de valores permitidos, digitada com vírgulas — o texto fica local enquanto se digita. */
function EnumInput({ field, onChange }: { field: ProcedureField; onChange: (values: (string | number)[] | null) => void }) {
  const joined = (field.enum ?? []).join(", ");
  const [text, setText] = useState(joined);
  // Quando o valor muda por fora (trocar o tipo limpa a lista), o texto acompanha.
  const [synced, setSynced] = useState(joined);
  if (joined !== synced) {
    setSynced(joined);
    setText(joined);
  }

  function commit() {
    const parts = text
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean);
    const numeric = field.type === "integer" || field.type === "number";
    const values = numeric ? parts.map(Number).filter((n) => !Number.isNaN(n)) : parts;
    onChange(values.length ? values : null);
  }

  return (
    <Input
      placeholder="valores permitidos (a, b, c)"
      aria-label="Valores permitidos"
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      className="h-8 text-xs"
    />
  );
}

function ActionEditor({
  stage,
  tools,
  onChange,
}: {
  stage: ProcedureStage;
  tools: ToolSummary[];
  onChange: (change: Partial<ProcedureStage>) => void;
}) {
  const selected = tools.find((t) => t.tool_name === stage.tool);
  // As com efeito colateral primeiro: são as que fazem sentido como ação.
  const ordered = [...tools].sort((a, b) => Number(b.side_effect === true) - Number(a.side_effect === true));
  return (
    <div className="flex flex-col gap-1.5">
      <div className="grid gap-1.5 sm:grid-cols-2">
        <Select aria-label="Tool da ação" value={stage.tool ?? ""} onChange={(e) => onChange({ tool: e.target.value })} className="h-8">
          <option value="">Escolha a tool…</option>
          {ordered.map((tool) => (
            <option key={tool.tool_name} value={tool.tool_name} disabled={!tool.enabled}>
              {tool.label} ({tool.tool_name}){tool.side_effect ? " · efeito colateral" : ""}
              {!tool.enabled ? " · desativada" : ""}
            </option>
          ))}
        </Select>
        {selected?.kind === "builtin" && (
          <Input
            placeholder="função da toolkit"
            aria-label="Função da toolkit"
            value={stage.function ?? ""}
            onChange={(e) => onChange({ function: e.target.value })}
            className="h-8 font-mono text-xs"
          />
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        O servidor chama a tool depois da confirmação, com os campos coletados como argumentos (o nome do campo tem de ser
        o do parâmetro) e com <code className="font-mono">idempotency_key</code> em dependencies — a mesma enquanto os
        dados não mudam. Se ela falhar, a pessoa
        precisa confirmar de novo para tentar outra vez.
      </p>
    </div>
  );
}
