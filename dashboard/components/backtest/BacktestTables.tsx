"use client";

import { useState } from "react";
import { Card, Empty } from "../Page";
import { JsonView } from "../JsonView";
import { ChevronDownIcon, ChevronRightIcon } from "../icons";

export function BacktestTables({
  tables,
}: {
  tables: Array<{ id: string; columns: string[]; rows: unknown[][] }>;
}) {
  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
      {tables.map((table) => (
        <Card key={table.id} title={table.id.replace(/_/g, " ")}>
          {table.rows.length === 0 ? (
            <Empty label="No rows" />
          ) : (
            <div className="embedded-table-scroll max-h-80 overflow-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-ink-950">
                  <tr>
                    {table.columns.map((col) => (
                      <th key={col} className="text-left font-medium text-ink-300 py-2 pr-3">
                        {col}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {table.rows.map((row, idx) => (
                    <tr key={idx} className="border-t border-brand-500/10">
                      {row.map((cell, cellIdx) => (
                        <td key={cellIdx} className="py-2 pr-3 text-ink-200 font-mono align-top">
                          <Cell value={cell} />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}

function Cell({ value }: { value: unknown }) {
  const [open, setOpen] = useState(false);
  if (value === null || value === undefined || value === "") {
    return <span className="text-ink-500">—</span>;
  }
  if (typeof value === "number") {
    return <span>{Number.isFinite(value) ? formatNumber(value) : "inf"}</span>;
  }
  if (typeof value === "boolean") {
    return (
      <span className={value ? "text-emerald-300" : "text-rose-300"}>
        {value ? "true" : "false"}
      </span>
    );
  }
  if (typeof value !== "object") {
    return <span>{String(value)}</span>;
  }
  const summary = Array.isArray(value)
    ? `[${value.length} item${value.length === 1 ? "" : "s"}]`
    : `{${Object.keys(value as Record<string, unknown>).length} field${
        Object.keys(value as Record<string, unknown>).length === 1 ? "" : "s"
      }}`;
  return (
    <div className="min-w-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="cursor-pointer inline-flex items-center gap-1 text-[11px] text-brand-200 hover:text-brand-100"
      >
        {open ? <ChevronDownIcon className="h-3 w-3" /> : <ChevronRightIcon className="h-3 w-3" />}
        {summary}
      </button>
      {open ? (
        <div className="mt-1 normal-case">
          <JsonView value={value} showRawToggle={false} className="bg-ink-950/40" />
        </div>
      ) : null}
    </div>
  );
}

function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return "inf";
  if (Math.abs(value) >= 100) return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (Math.abs(value) >= 1) return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return value.toLocaleString(undefined, { maximumFractionDigits: 6 });
}
