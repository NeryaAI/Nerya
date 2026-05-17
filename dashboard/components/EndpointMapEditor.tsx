"use client";

/**
 * EndpointMapEditor — friendly editor for the
 * `Record<string, Record<string, unknown>>` shape used by the
 * exchange author wizard's HTTP `endpoints` field.
 *
 * Each row exposes the three fields operators actually fill in
 * (name / method / path) plus an optional auth-required toggle.
 * Anything more exotic still falls through into a per-row "extras"
 * `KeyValueEditor` so we never block the user from emitting JSON
 * the backend will accept.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { TrashIcon, PlusIcon } from "./icons";
import { Select } from "./Select";

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

interface EndpointRow {
  id: string;
  name: string;
  method: Method;
  path: string;
  authRequired: boolean;
  extras: Record<string, unknown>;
}

interface Props {
  value: Record<string, Record<string, unknown>>;
  onChange: (next: Record<string, Record<string, unknown>>) => void;
  disabled?: boolean;
}

const METHOD_OPTIONS = (["GET", "POST", "PUT", "PATCH", "DELETE"] as const).map(
  (m) => ({ value: m, label: m }),
);

let counter = 0;
function makeId(): string {
  counter += 1;
  return `endpoint-${counter}`;
}

function normalizeMethod(value: unknown): Method {
  if (typeof value !== "string") return "GET";
  const upper = value.toUpperCase();
  return (METHOD_OPTIONS.find((opt) => opt.value === upper)?.value ?? "GET") as Method;
}

function rowsFromMap(
  map: Record<string, Record<string, unknown>>,
): EndpointRow[] {
  return Object.entries(map ?? {}).map(([name, body]) => {
    const safe = (body ?? {}) as Record<string, unknown>;
    const { method, path, auth_required, ...rest } = safe;
    return {
      id: makeId(),
      name,
      method: normalizeMethod(method),
      path: typeof path === "string" ? path : "",
      authRequired: Boolean(auth_required),
      extras: rest,
    };
  });
}

function rowsToMap(rows: EndpointRow[]): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {};
  for (const row of rows) {
    const name = row.name.trim();
    if (!name) continue;
    const body: Record<string, unknown> = {
      method: row.method,
      path: row.path.trim(),
      ...row.extras,
    };
    if (row.authRequired) body.auth_required = true;
    out[name] = body;
  }
  return out;
}

export function EndpointMapEditor({ value, onChange, disabled = false }: Props) {
  const [rows, setRows] = useState<EndpointRow[]>(() => rowsFromMap(value));
  const initialRef = useRef<string>("");
  const fingerprint = useMemo(() => {
    try {
      return JSON.stringify(value ?? {});
    } catch {
      return "";
    }
  }, [value]);

  useEffect(() => {
    if (initialRef.current === fingerprint) return;
    initialRef.current = fingerprint;
    setRows(rowsFromMap(value));
  }, [fingerprint, value]);

  function commit(next: EndpointRow[]) {
    setRows(next);
    initialRef.current = ""; // force re-sync on next external value change
    onChange(rowsToMap(next));
  }

  function update(id: string, patch: Partial<EndpointRow>) {
    commit(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  function remove(id: string) {
    commit(rows.filter((r) => r.id !== id));
  }

  function add() {
    commit([
      ...rows,
      {
        id: makeId(),
        name: "",
        method: "GET",
        path: "",
        authRequired: false,
        extras: {},
      },
    ]);
  }

  return (
    <div className="space-y-2">
      {rows.length === 0 ? (
        <div className="text-[12px] text-ink-500 italic px-1 py-2">
          No endpoints yet. Add at least one (e.g. <code className="font-mono">markets</code>{" "}
          <code className="font-mono">GET /api/markets</code>).
        </div>
      ) : (
        <div className="space-y-1.5">
          <div className="grid grid-cols-[minmax(0,140px)_90px_minmax(0,1fr)_auto_auto] gap-2 px-2 text-[11px] text-ink-500 font-medium">
            <span>Name</span>
            <span>Method</span>
            <span>Path</span>
            <span className="text-center">Auth</span>
            <span className="sr-only">Actions</span>
          </div>
          {rows.map((row) => (
            <div
              key={row.id}
              className="grid grid-cols-[minmax(0,140px)_90px_minmax(0,1fr)_auto_auto] items-center gap-2 rounded-lg border border-brand-500/10 bg-ink-900/40 px-2 py-2"
            >
              <input
                value={row.name}
                onChange={(e) => update(row.id, { name: e.target.value.trim() })}
                placeholder="markets"
                disabled={disabled}
                className="input-dark font-mono text-[12px]"
              />
              <Select<Method>
                value={row.method}
                onChange={(method) => update(row.id, { method })}
                options={METHOD_OPTIONS}
                size="sm"
                ariaLabel="HTTP method"
                disabled={disabled}
              />
              <input
                value={row.path}
                onChange={(e) => update(row.id, { path: e.target.value })}
                placeholder="/api/markets"
                disabled={disabled}
                className="input-dark font-mono text-[12px]"
              />
              <label
                className={`inline-flex items-center justify-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium ${
                  row.authRequired
                    ? "border-brand-500/40 bg-brand-500/10 text-brand-100"
                    : "border-brand-500/15 text-ink-500"
                }`}
              >
                <input
                  type="checkbox"
                  className="h-3 w-3"
                  checked={row.authRequired}
                  onChange={(e) => update(row.id, { authRequired: e.target.checked })}
                  disabled={disabled}
                />
                {row.authRequired ? "yes" : "no"}
              </label>
              <button
                type="button"
                onClick={() => remove(row.id)}
                disabled={disabled}
                aria-label="remove endpoint"
                className="cursor-pointer inline-flex h-7 w-7 items-center justify-center rounded-md border border-rose-500/20 text-rose-300/80 hover:bg-rose-500/10 hover:text-rose-200 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <TrashIcon className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      {!disabled ? (
        <button
          type="button"
          onClick={add}
          className="cursor-pointer inline-flex items-center gap-1 rounded-md border border-brand-500/25 bg-brand-500/10 px-2.5 py-1 text-[11px] text-brand-100 hover:bg-brand-500/15"
        >
          <PlusIcon className="h-3 w-3" />
          Add endpoint
        </button>
      ) : null}
    </div>
  );
}

export default EndpointMapEditor;
