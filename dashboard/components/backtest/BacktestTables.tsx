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
    <div className="grid grid-cols-1 gap-4">
      {tables.map((table) => (
        <Card key={table.id} title={table.id.replace(/_/g, " ")}>
          {table.rows.length === 0 ? (
            <Empty label="No rows" />
          ) : (
            <div className="embedded-table-scroll max-h-96 overflow-auto rounded-md border border-[color:var(--line)]">
              <table className="min-w-[720px] w-full text-xs">
                <thead className="sticky top-0 bg-[color:var(--card-hi)]">
                  <tr>
                    {table.columns.map((col) => (
                      <th
                        key={col}
                        className="whitespace-nowrap px-3 py-2 text-left font-medium text-[color:var(--text-muted)]"
                      >
                        {col}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {table.rows.map((row, idx) => (
                    <tr key={idx} className="border-t border-brand-500/10">
                      {row.map((cell, cellIdx) => (
                        <td
                          key={cellIdx}
                          className="px-3 py-2 align-top font-mono text-[color:var(--text-base)]"
                        >
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
    return <span className="text-ink-500">-</span>;
  }
  if (typeof value === "number") {
    return (
      <span className="whitespace-nowrap">
        {Number.isFinite(value) ? formatNumber(value) : "inf"}
      </span>
    );
  }
  if (typeof value === "boolean") {
    return (
      <span className={value ? "text-emerald-300" : "text-rose-300"}>
        {value ? "true" : "false"}
      </span>
    );
  }
  if (typeof value !== "object") {
    return <span className="block max-w-[24rem] whitespace-normal break-words">{String(value)}</span>;
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
        <div className="mt-1 min-w-[18rem] normal-case">
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
