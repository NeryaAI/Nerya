"use client";

import { useState } from "react";
import { Card, Empty } from "../Page";
import { JsonView } from "../JsonView";
import { ChevronDownIcon, ChevronRightIcon } from "../icons";

type BacktestTable = { id: string; columns: string[]; rows: unknown[][] };

export function BacktestTables({
  tables,
  compact = false,
  maxHeightClass = "max-h-[420px]",
}: {
  tables: BacktestTable[];
  compact?: boolean;
  maxHeightClass?: string;
}) {
  return (
    <div className={compact ? "grid grid-cols-1 gap-3" : "grid grid-cols-1 gap-4"}>
      {tables.map((table) => (
        <Card key={table.id} title={formatTableTitle(table.id)} padded={false}>
          <div className={compact ? "px-3 py-2.5" : "px-4 py-3.5"}>
            {table.rows.length === 0 ? (
              <Empty label="No rows" />
            ) : (
              <div className={`embedded-table-scroll ${maxHeightClass} rounded-md border border-[color:var(--line)]`}>
                <table className={`min-w-[720px] w-full ${compact ? "text-[11.5px]" : "text-xs"}`}>
                  <thead className="sticky top-0 bg-[color:var(--card-hi)]">
                    <tr>
                      {table.columns.map((col) => (
                        <th
                          key={col}
                          className={`whitespace-nowrap text-left font-medium text-[color:var(--text-muted)] ${
                            compact ? "px-2.5 py-1.5" : "px-3 py-2"
                          }`}
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
                            className={`align-top font-mono text-[color:var(--text-base)] ${
                              compact ? "px-2.5 py-1.5" : "px-3 py-2"
                            }`}
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
          </div>
        </Card>
      ))}
    </div>
  );
}

function formatTableTitle(id: string): string {
  if (/^trades(?:_top\d+)?$/i.test(id)) return "trades";
  return id.replace(/_/g, " ");
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
