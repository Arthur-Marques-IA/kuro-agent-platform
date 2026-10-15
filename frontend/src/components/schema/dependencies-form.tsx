"use client";

import { type ReactNode, useId, useMemo, useState } from "react";
import { Braces, ListChecks, Plus, Trash, X } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  coerceText,
  displayText,
  emptyItem,
  extraKeys,
  formatLoose,
  isPlainObject,
  parseLoose,
  parseObjectText,
  setKey,
  stringifyObject,
  validateObject,
  type FormField,
  type JsonObject,
} from "@/lib/dependencies-form";
import type { SchemaItem } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

/**
 * Formulário de um objeto JSON (o `dependencies` do Playground e da Análise, os
 * argumentos e o `dependencies` do teste de tool), gerado dos campos declarados.
 * Não confundir com o `SchemaFieldsEditor`, que edita a declaração em si.
 *
 * A fonte da verdade é o texto JSON (`value`/`onChange`): o modo JSON é o mesmo
 * texto, então os dois ficam sincronizados sem estado duplicado, e quem já
 * persistia o texto continua persistindo. Chaves que não estão em `fields`
 * aparecem em "Outros campos" e nunca se perdem; sem campos declarados, o
 * formulário é só esse editor chave/valor.
 */
export function DependenciesForm({
  fields,
  value,
  onChange,
  invalid,
  jsonRows = 7,
  placeholder,
  label = "dependencies",
  className,
}: {
  fields: FormField[];
  value: string;
  onChange: (text: string) => void;
  /** Marca o textarea do modo JSON como inválido (o erro em si fica com quem chama). */
  invalid?: boolean;
  jsonRows?: number;
  placeholder?: string;
  /** Nome do objeto, para os rótulos acessíveis. */
  label?: string;
  className?: string;
}) {
  const baseId = useId();
  const parsed = useMemo(() => parseObjectText(value), [value]);
  // Abre no JSON se o texto de entrada não vira formulário; daí em diante, quem decide é a pessoa.
  const [mode, setMode] = useState<"form" | "json">(() => (parsed.value ? "form" : "json"));
  const obj = parsed.value;
  const errors = useMemo(() => (obj ? validateObject(fields, obj) : {}), [fields, obj]);

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div role="tablist" aria-label={`Editar ${label} como`} className="flex w-fit rounded-lg border border-border p-0.5">
        <ModeButton active={mode === "form"} onClick={() => setMode("form")}>
          <ListChecks /> Formulário
        </ModeButton>
        <ModeButton active={mode === "json"} onClick={() => setMode("json")}>
          <Braces /> JSON
        </ModeButton>
      </div>

      {mode === "json" ? (
        <Textarea
          id={`${baseId}-json`}
          aria-label={`${label} (JSON)`}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={jsonRows}
          spellCheck={false}
          placeholder={placeholder}
          aria-invalid={invalid || undefined}
          className="resize-y font-mono text-xs leading-5"
        />
      ) : obj ? (
        <ObjectFields
          fields={fields}
          value={obj}
          onChange={(next) => onChange(stringifyObject(next))}
          path=""
          baseId={baseId}
          errors={errors}
          showExtras
        />
      ) : (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          {parsed.error} — o formulário só mostra um objeto válido.{" "}
          <button type="button" className="font-medium underline underline-offset-2" onClick={() => setMode("json")}>
            Corrigir no JSON
          </button>
        </div>
      )}
    </div>
  );
}

function ModeButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "inline-flex h-6 items-center gap-1 rounded-md px-2 text-xs font-medium transition-colors [&_svg]:size-3.5",
        active ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground"
      )}
    >
      {children}
    </button>
  );
}

function fieldId(baseId: string, path: string) {
  return `${baseId}-${path.replace(/[^A-Za-z0-9_-]/g, "_")}`;
}

function ObjectFields({
  fields,
  value,
  onChange,
  path,
  baseId,
  errors,
  showExtras = false,
}: {
  fields: FormField[];
  value: JsonObject;
  onChange: (next: JsonObject) => void;
  path: string;
  baseId: string;
  errors: Record<string, string>;
  /** Só no nível de cima: dentro de um object, o que não é declarado é preservado sem aparecer. */
  showExtras?: boolean;
}) {
  return (
    <div className="flex flex-col gap-3">
      {fields.map((field) => {
        const fieldPath = path ? `${path}.${field.name}` : field.name;
        return (
          <FieldControl
            key={field.name}
            field={field}
            value={value[field.name]}
            onChange={(v) => onChange(setKey(value, field.name, v))}
            path={fieldPath}
            baseId={baseId}
            errors={errors}
          />
        );
      })}
      {showExtras && (
        <ExtraFields
          keys={extraKeys(fields, value)}
          value={value}
          onChange={onChange}
          titled={fields.length > 0}
          baseId={baseId}
        />
      )}
    </div>
  );
}

function FieldControl({
  field,
  value,
  onChange,
  path,
  baseId,
  errors,
}: {
  field: FormField;
  value: unknown;
  onChange: (value: unknown) => void;
  path: string;
  baseId: string;
  errors: Record<string, string>;
}) {
  const id = fieldId(baseId, path);
  const error = errors[path];
  const label = field.label && field.label !== field.name ? field.label : null;
  const presente = value !== undefined;

  let control: ReactNode;
  if (field.type === "object" && (value === undefined || isPlainObject(value)) && field.fields?.length) {
    control = (
      <div className="rounded-lg border border-border bg-muted/30 p-2">
        <ObjectFields
          fields={field.fields}
          value={(value as JsonObject | undefined) ?? {}}
          onChange={(next) => onChange(Object.keys(next).length === 0 && !field.required ? undefined : next)}
          path={path}
          baseId={baseId}
          errors={errors}
        />
      </div>
    );
  } else if (field.type === "array" && (value === undefined || Array.isArray(value)) && field.items) {
    control = (
      <ArrayControl
        items={field.items}
        value={(value as unknown[] | undefined) ?? []}
        onChange={(next) => onChange(next.length === 0 && !field.required ? undefined : next)}
        path={path}
        baseId={baseId}
        errors={errors}
      />
    );
  } else if (field.type === "object" || field.type === "array") {
    // Valor que não tem a forma declarada (ou declaração sem fields/items): edita solto, sem perder nada.
    control = <LooseInput id={id} value={value} onChange={onChange} invalid={Boolean(error)} />;
  } else {
    control = (
      <LeafControl
        id={id}
        type={field.type}
        enumValues={field.enum}
        value={value}
        onChange={onChange}
        invalid={Boolean(error)}
        placeholder={field.default !== undefined && field.default !== null ? `padrão: ${displayText(field.default)}` : undefined}
      />
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="flex min-w-0 items-baseline gap-1.5">
        <label htmlFor={id} className="min-w-0 truncate text-[13px] font-medium">
          {label ?? <span className="font-mono text-xs">{field.name}</span>}
          {field.required && (
            <span className="text-destructive" title="obrigatório">
              {" "}
              *
            </span>
          )}
        </label>
        {label && <span className="truncate font-mono text-[11px] text-muted-foreground">{field.name}</span>}
        <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground">
          {field.type === "array" && field.items ? `${field.items.type}[]` : field.type}
        </span>
        {presente && !field.required && field.type !== "object" && field.type !== "array" && (
          <button
            type="button"
            onClick={() => onChange(undefined)}
            className="shrink-0 text-muted-foreground hover:text-foreground"
            aria-label={`Não enviar ${field.name}`}
            title="Não enviar este campo"
          >
            <X className="size-3" />
          </button>
        )}
      </div>
      {control}
      {error && <p className="text-xs text-destructive">{error}</p>}
      {field.description && <p className="text-xs text-muted-foreground">{field.description}</p>}
    </div>
  );
}

function LeafControl({
  id,
  type,
  enumValues,
  value,
  onChange,
  invalid,
  placeholder,
}: {
  id: string;
  type: string;
  enumValues?: (string | number)[] | null;
  value: unknown;
  onChange: (value: unknown) => void;
  invalid?: boolean;
  placeholder?: string;
}) {
  if (type === "boolean") {
    return (
      <label className="flex h-8 items-center gap-2 text-[13px]">
        <input
          id={id}
          type="checkbox"
          checked={value === true}
          onChange={(e) => onChange(e.target.checked)}
          className="accent-primary"
          aria-invalid={invalid || undefined}
        />
        {value === undefined ? (
          <span className="text-muted-foreground">não enviado</span>
        ) : typeof value === "boolean" ? (
          <span className="font-mono text-xs">{String(value)}</span>
        ) : (
          <span className="font-mono text-xs text-destructive">{displayText(value)}</span>
        )}
      </label>
    );
  }

  if (enumValues?.length) {
    const atual = enumValues.find((v) => v === value);
    // Valor fora do enum continua visível (e apontado) em vez de sumir do select.
    const fora = value !== undefined && atual === undefined;
    return (
      <Select
        id={id}
        value={value === undefined ? "" : displayText(value)}
        onChange={(e) => {
          const escolhido = enumValues.find((v) => String(v) === e.target.value);
          onChange(e.target.value === "" ? undefined : (escolhido ?? e.target.value));
        }}
        aria-invalid={invalid || undefined}
        className="h-8 text-[13px]"
      >
        <option value="">— não enviar —</option>
        {fora && <option value={displayText(value)}>{displayText(value)}</option>}
        {enumValues.map((v) => (
          <option key={String(v)} value={String(v)}>
            {String(v)}
          </option>
        ))}
      </Select>
    );
  }

  return (
    <DraftInput
      id={id}
      value={value}
      parse={(text) => coerceText(type, text)}
      onChange={onChange}
      invalid={invalid}
      placeholder={placeholder}
      inputMode={type === "integer" ? "numeric" : type === "number" ? "decimal" : undefined}
    />
  );
}

/**
 * Input que guarda o que foi digitado enquanto isso ainda vale o valor do
 * objeto: "1." mostra "1." (e não "1") com o objeto já em 1. Se o valor muda
 * por fora (modo JSON, "Valores de teste"), o input passa a mostrar o novo.
 */
function DraftInput({
  id,
  value,
  parse,
  onChange,
  invalid,
  placeholder,
  inputMode,
  className,
  mono,
}: {
  id?: string;
  value: unknown;
  parse: (text: string) => unknown;
  onChange: (value: unknown) => void;
  invalid?: boolean;
  placeholder?: string;
  inputMode?: "numeric" | "decimal";
  className?: string;
  mono?: boolean;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const shown = draft !== null && sameValue(parse(draft), value) ? draft : formatFor(value, mono);
  return (
    <Input
      id={id}
      value={shown}
      onChange={(e) => {
        setDraft(e.target.value);
        onChange(parse(e.target.value));
      }}
      placeholder={placeholder}
      inputMode={inputMode}
      spellCheck={false}
      aria-invalid={invalid || undefined}
      className={cn("h-8 text-[13px]", mono && "font-mono text-xs", className)}
    />
  );
}

function formatFor(value: unknown, loose?: boolean) {
  return loose ? formatLoose(value) : displayText(value);
}

function sameValue(a: unknown, b: unknown) {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

/** Valor sem tipo declarado: JSON quando dá, texto quando não (como o `chave=valor` da CLI). */
function LooseInput({
  id,
  value,
  onChange,
  invalid,
  className,
}: {
  id?: string;
  value: unknown;
  onChange: (value: unknown) => void;
  invalid?: boolean;
  className?: string;
}) {
  return <DraftInput id={id} value={value} parse={parseLoose} onChange={onChange} invalid={invalid} className={className} mono />;
}

function ArrayControl({
  items,
  value,
  onChange,
  path,
  baseId,
  errors,
}: {
  items: SchemaItem;
  value: unknown[];
  onChange: (next: unknown[]) => void;
  path: string;
  baseId: string;
  errors: Record<string, string>;
}) {
  const replace = (index: number, item: unknown) => onChange(value.map((v, i) => (i === index ? item : v)));
  const remove = (index: number) => onChange(value.filter((_, i) => i !== index));

  return (
    <div className="flex flex-col gap-1.5">
      {value.map((item, index) => {
        const itemPath = `${path}[${index}]`;
        const error = errors[itemPath];
        const removeButton = (
          <Button
            variant="ghost"
            size="icon-xs"
            onClick={() => remove(index)}
            aria-label={`Remover item ${index + 1}`}
            className="shrink-0"
          >
            <Trash />
          </Button>
        );
        if (items.type === "object" && items.fields?.length && isPlainObject(item)) {
          return (
            <div key={index} className="rounded-lg border border-border bg-muted/30 p-2">
              <div className="mb-1 flex items-center justify-between">
                <span className="font-mono text-[11px] text-muted-foreground">#{index + 1}</span>
                {removeButton}
              </div>
              <ObjectFields
                fields={items.fields}
                value={item}
                onChange={(next) => replace(index, next)}
                path={itemPath}
                baseId={baseId}
                errors={errors}
              />
            </div>
          );
        }
        return (
          <div key={index} className="flex flex-col gap-0.5">
            <div className="flex items-center gap-1">
              <div className="min-w-0 flex-1">
                {items.type === "object" ? (
                  <LooseInput value={item} onChange={(v) => replace(index, v)} invalid={Boolean(error)} />
                ) : (
                  <LeafControl
                    id={fieldId(baseId, itemPath)}
                    type={items.type}
                    enumValues={items.enum}
                    value={item}
                    onChange={(v) => replace(index, v ?? "")}
                    invalid={Boolean(error)}
                  />
                )}
              </div>
              {removeButton}
            </div>
            {error && <p className="text-xs text-destructive">{error}</p>}
          </div>
        );
      })}
      <div>
        <Button variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={() => onChange([...value, emptyItem(items)])}>
          <Plus /> Adicionar item
        </Button>
      </div>
    </div>
  );
}

/** Chaves fora dos campos declarados: editáveis como chave/valor solto. */
function ExtraFields({
  keys,
  value,
  onChange,
  titled,
  baseId,
}: {
  keys: string[];
  value: JsonObject;
  onChange: (next: JsonObject) => void;
  titled: boolean;
  baseId: string;
}) {
  const [novaChave, setNovaChave] = useState("");
  const [novoValor, setNovoValor] = useState("");
  const chave = novaChave.trim();
  const repetida = chave !== "" && Object.prototype.hasOwnProperty.call(value, chave);

  function adicionar() {
    if (!chave || repetida) return;
    onChange(setKey(value, chave, parseLoose(novoValor)));
    setNovaChave("");
    setNovoValor("");
  }

  return (
    <div className={cn("flex flex-col gap-1.5", titled && "border-t border-border pt-3")}>
      {titled && (
        <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Outros campos <span className="normal-case tracking-normal">— não declarados, enviados como estão</span>
        </p>
      )}
      {keys.map((key) => (
        <div key={key} className="flex items-center gap-1">
          <label
            htmlFor={fieldId(baseId, `extra-${key}`)}
            className="w-28 shrink-0 truncate font-mono text-xs text-muted-foreground"
            title={key}
          >
            {key}
          </label>
          <LooseInput
            id={fieldId(baseId, `extra-${key}`)}
            value={value[key]}
            onChange={(v) => onChange(setKey(value, key, v))}
            className="min-w-0 flex-1"
          />
          <Button
            variant="ghost"
            size="icon-xs"
            onClick={() => onChange(setKey(value, key, undefined))}
            aria-label={`Remover ${key}`}
          >
            <Trash />
          </Button>
        </div>
      ))}
      <div className="flex items-center gap-1">
        <Input
          value={novaChave}
          onChange={(e) => setNovaChave(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && adicionar()}
          placeholder="campo"
          aria-label="Nome do novo campo"
          aria-invalid={repetida || undefined}
          spellCheck={false}
          className="h-8 w-28 shrink-0 font-mono text-xs"
        />
        <Input
          value={novoValor}
          onChange={(e) => setNovoValor(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && adicionar()}
          placeholder="valor (texto ou JSON)"
          aria-label="Valor do novo campo"
          spellCheck={false}
          className="h-8 min-w-0 flex-1 font-mono text-xs"
        />
        <Button variant="ghost" size="icon-xs" onClick={adicionar} disabled={!chave || repetida} aria-label="Adicionar campo">
          <Plus />
        </Button>
      </div>
      {repetida && <p className="text-xs text-destructive">O campo {chave} já existe.</p>}
    </div>
  );
}
