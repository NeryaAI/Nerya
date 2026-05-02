"use client";

import { Card, Empty } from "../Page";

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
                    <tr key={idx} className="border-t border-white/5">
                      {row.map((cell, cellIdx) => (
                        <td key={cellIdx} className="py-2 pr-3 text-ink-200 font-mono">
                          {formatCell(cell)}
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

function formatCell(value: unknown): string {
  if (typeof value === "number") return Number.isFinite(value) ? value.toFixed(4) : "inf";
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}

