"use client";

/**
 * KeyValueEditor — graphical editor for `Record<string, unknown>` style
 * configuration blobs.
 *
 * Renders one row per key with a typed input (string / number /
 * boolean / null). Nested objects and arrays are summarised as a
 * read-only chip with a "Show JSON" expander, so operators don't have
 * to hand-craft JSON for the common cases (primitive flags & numbers)
 * but power users still have an escape hatch.
 *
 * Why a custom editor instead of a generic JSON tree:
 *   - keeps rows compact and editable inline,
 *   - emits real `unknown` values rather than coerced strings,
 *   - matches the "airy violet token" surface of the dashboard.
 */

import { useEffect, useMemo, useState } from "react";

import { TrashIcon, PlusIcon } from "./icons";

import { Select } from "./Select";

type ValueKind = "string" | "number" | "boolean" | "null" | "json";

interface InternalRow {
  id: string;
  key: string;
  kind: ValueKind;
  raw: string;
  bool: boolean;
  json: string;
  error?: string;
}

interface Props {
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  /** Suggested keys, surfaced as quick-add chips. */
  suggestedKeys?: string[];
  /** Disable editing (read-only display). */
  disabled?: boolean;
  /** Empty-state copy. */
  emptyLabel?: string;
  /** Add-row label. */
  addLabel?: string;
  /** Class name on the wrapping div. */
  className?: string;
}

let counter = 0;
function makeId(): string {
  counter += 1;
  return `kv-${counter}`;
}

function kindOf(value: unknown): ValueKind {
  if (value === null) return "null";
  if (typeof value === "string") return "string";
  if (typeof value === "number") return "number";
  if (typeof value === "boolean") return "boolean";
  return "json";
}

function rowsFromObject(obj: Record<string, unknown>): InternalRow[] {
  return Object.entries(obj).map(([key, value]) => {
    const kind = kindOf(value);
    return {
      id: makeId(),
      key,
      kind,
      raw: kind === "string" ? String(value ?? "") : kind === "number" ? String(value ?? "") : "",
      bool: kind === "boolean" ? Boolean(value) : false,
      json: kind === "json" ? safeStringify(value) : "",
    };
  });
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function rowToValue(row: InternalRow): { ok: true; value: unknown } | { ok: false; error: string } {
  switch (row.kind) {
    case "string":
      return { ok: true, value: row.raw };
    case "number": {
      if (row.raw.trim() === "") return { ok: false, error: "Number value required" };
      const n = Number(row.raw);
      if (!Number.isFinite(n)) return { ok: false, error: "Not a finite number" };
      return { ok: true, value: n };
    }
    case "boolean":
      return { ok: true, value: row.bool };
    case "null":
      return { ok: true, value: null };
    case "json": {
      const text = row.json.trim();
      if (!text) return { ok: true, value: null };
      try {
        return { ok: true, value: JSON.parse(text) };
      } catch (e) {
        return { ok: false, error: e instanceof Error ? e.message : "Invalid JSON" };
      }
    }
  }
}

function rowsToObject(rows: InternalRow[]): {
  obj: Record<string, unknown>;
  errors: Record<string, string>;
} {
  const obj: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const row of rows) {
    const k = row.key.trim();
    if (!k) continue;
    const result = rowToValue(row);
    if (!result.ok) {
      errors[row.id] = result.error;
      continue;
    }
    obj[k] = result.value;
  }
  return { obj, errors };
}

export function KeyValueEditor({
  value,
  onChange,
  suggestedKeys,
  disabled = false,
  emptyLabel = "No fields configured.",
  addLabel = "Add field",
  className,
}: Props) {
  const [rows, setRows] = useState<InternalRow[]>(() => rowsFromObject(value));
  const initialKey = useMemo(() => safeStringify(value), [value]);

  useEffect(() => {
    setRows(rowsFromObject(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialKey]);

  function commit(next: InternalRow[]) {
    setRows(next);
    const { obj } = rowsToObject(next);
    onChange(obj);
  }

  function update(id: string, patch: Partial<InternalRow>) {
    commit(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  function remove(id: string) {
    commit(rows.filter((r) => r.id !== id));
  }

  function add(key = "") {
    commit([
      ...rows,
      {
        id: makeId(),
        key,
        kind: "string",
        raw: "",
        bool: false,
        json: "",
      },
    ]);
  }

  const usedKeys = new Set(rows.map((r) => r.key.trim()).filter(Boolean));
  const remainingSuggestions =
    suggestedKeys?.filter((k) => !usedKeys.has(k)) ?? [];

  return (
    <div className={`space-y-2 ${className ?? ""}`}>
      {rows.length === 0 ? (
        <div className="text-[12px] text-ink-500 italic px-1 py-2">
          {emptyLabel}
        </div>
      ) : (
        <div className="space-y-1.5">
          {rows.map((row) => {
            const valueResult = rowToValue(row);
            const error = !valueResult.ok ? valueResult.error : undefined;
            return (
              <div
                key={row.id}
                className="grid grid-cols-[minmax(0,1fr)_120px_minmax(0,2fr)_auto] items-start gap-2 rounded-lg border border-brand-500/10 bg-ink-900/40 px-2 py-2"
              >
                <input
                  value={row.key}
                  onChange={(e) => update(row.id, { key: e.target.value })}
                  placeholder="key"
                  disabled={disabled}
                  className="input-dark font-mono text-[12px]"
                />
                <Select<ValueKind>
                  value={row.kind}
                  onChange={(kind) => update(row.id, { kind })}
                  options={[
                    { value: "string", label: "string" },
                    { value: "number", label: "number" },
                    { value: "boolean", label: "boolean" },
                    { value: "null", label: "null" },
                    { value: "json", label: "json/array" },
                  ]}
                  size="sm"
                  ariaLabel="value kind"
                  disabled={disabled}
                />
                <div className="min-w-0">
                  <ValueInput row={row} disabled={disabled} onPatch={(patch) => update(row.id, patch)} />
                  {error ? (
                    <div className="mt-1 text-[10px] text-rose-300 font-mono">{error}</div>
                  ) : null}
                </div>
                <button
                  type="button"
                  onClick={() => remove(row.id)}
                  disabled={disabled}
                  aria-label="remove field"
                  className="cursor-pointer mt-1 inline-flex h-7 w-7 items-center justify-center rounded-md border border-rose-500/20 text-rose-300/80 hover:bg-rose-500/10 hover:text-rose-200 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <TrashIcon className="h-3.5 w-3.5" />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {!disabled ? (
        <div className="flex flex-wrap items-center gap-1.5 pt-1">
          <button
            type="button"
            onClick={() => add()}
            className="cursor-pointer inline-flex items-center gap-1 rounded-md border border-brand-500/25 bg-brand-500/10 px-2.5 py-1 text-[11px] text-brand-100 hover:bg-brand-500/15"
          >
            <PlusIcon className="h-3 w-3" />
            {addLabel}
          </button>
          {remainingSuggestions.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => add(key)}
              className="cursor-pointer rounded-full border border-brand-500/20 bg-ink-900/60 px-2 py-0.5 text-[10px] font-mono text-ink-300 hover:border-brand-500/40 hover:text-brand-100"
            >
              + {key}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ValueInput({
  row,
  disabled,
  onPatch,
}: {
  row: InternalRow;
  disabled: boolean;
  onPatch: (patch: Partial<InternalRow>) => void;
}) {
  if (row.kind === "boolean") {
    return (
      <Select
        value={row.bool ? "true" : "false"}
        onChange={(v) => onPatch({ bool: v === "true" })}
        options={[
          { value: "true", label: "true" },
          { value: "false", label: "false" },
        ]}
        size="sm"
        ariaLabel="boolean value"
        disabled={disabled}
      />
    );
  }
  if (row.kind === "null") {
    return (
      <div className="text-[11px] font-mono text-ink-500 px-2 py-1.5">
        null
      </div>
    );
  }
  if (row.kind === "json") {
    return (
      <textarea
        value={row.json}
        onChange={(e) => onPatch({ json: e.target.value })}
        placeholder='{"foo": 1} or [1,2,3]'
        rows={Math.min(6, Math.max(2, row.json.split("\n").length))}
        disabled={disabled}
        className="input-dark font-mono text-[11px] w-full resize-y"
        spellCheck={false}
      />
    );
  }
  if (row.kind === "number") {
    return (
      <input
        type="number"
        value={row.raw}
        onChange={(e) => onPatch({ raw: e.target.value })}
        placeholder="0"
        disabled={disabled}
        className="input-dark font-mono text-[12px]"
      />
    );
  }
  return (
    <input
      value={row.raw}
      onChange={(e) => onPatch({ raw: e.target.value })}
      placeholder="value"
      disabled={disabled}
      className="input-dark text-[12px]"
    />
  );
}

export default KeyValueEditor;
