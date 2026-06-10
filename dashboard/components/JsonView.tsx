"use client";

/**
 * JsonView — a friendly, collapsible read-only view of arbitrary
 * JSON-shaped data.
 *
 * Replaces the old `<pre>{JSON.stringify(...)}</pre>` blob with:
 *   - key/value tables for plain objects,
 *   - data tables for arrays of objects,
 *   - compact chip rows for primitive arrays,
 *   - typed pills for primitive values (string / number / boolean /
 *     null), and
 *   - a "Raw JSON" toggle for power users that want the canonical
 *     payload.
 */

import { ReactNode, useMemo, useState } from "react";

import { ChevronDownIcon, ChevronRightIcon } from "./icons";

type JsonRecord = Record<string, unknown>;

interface Props {
  value: unknown;
  /** Additional class on the wrapper. */
  className?: string;
  /** Force the initial state collapsed. Defaults to expanded. */
  initialCollapsed?: boolean;
  /** Show the "Raw JSON" toggle. Defaults to true. */
  showRawToggle?: boolean;
  /** Limit visible nesting depth before collapsing. */
  maxDepth?: number;
}

export function JsonView({
  value,
  className,
  initialCollapsed = false,
  showRawToggle = true,
  maxDepth = 6,
}: Props) {
  const [showRaw, setShowRaw] = useState(false);
  const rawText = useMemo(() => {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }, [value]);

  return (
    <div
      className={`rounded-lg border border-brand-500/10 bg-ink-900/40 px-3 py-2 ${className ?? ""}`}
    >
      {showRawToggle ? (
        <div className="flex items-center justify-end gap-2 border-b border-brand-500/10 pb-1.5 text-[11px] text-ink-500 font-medium">
          <button
            type="button"
            onClick={() => setShowRaw(false)}
            className={`cursor-pointer rounded px-2 py-0.5 transition-colors ${
              !showRaw
                ? "bg-brand-500/15 text-brand-100"
                : "text-ink-500 hover:text-ink-200"
            }`}
          >
            Structured
          </button>
          <button
            type="button"
            onClick={() => setShowRaw(true)}
            className={`cursor-pointer rounded px-2 py-0.5 transition-colors ${
              showRaw
                ? "bg-brand-500/15 text-brand-100"
                : "text-ink-500 hover:text-ink-200"
            }`}
          >
            Raw JSON
          </button>
        </div>
      ) : null}
      <div className="embedded-scroll max-h-[420px] overflow-auto pt-1.5">
        {showRaw ? (
          <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-ink-200">
            {rawText || "–"}
          </pre>
        ) : (
          <Node
            value={value}
            depth={0}
            maxDepth={maxDepth}
            initialCollapsed={initialCollapsed}
          />
        )}
      </div>
    </div>
  );
}

function Node({
  value,
  depth,
  maxDepth,
  initialCollapsed,
}: {
  value: unknown;
  depth: number;
  maxDepth: number;
  initialCollapsed: boolean;
}) {
  if (value === null) return <Primitive kind="null">null</Primitive>;
  if (typeof value === "string") return <Primitive kind="string">{quoteIfMultiline(value)}</Primitive>;
  if (typeof value === "number") return <Primitive kind="number">{String(value)}</Primitive>;
  if (typeof value === "boolean") return <Primitive kind="boolean">{value ? "true" : "false"}</Primitive>;
  if (typeof value === "undefined") return <Primitive kind="null">undefined</Primitive>;
  if (Array.isArray(value)) {
    return (
      <ArrayBlock
        items={value}
        depth={depth}
        maxDepth={maxDepth}
        initialCollapsed={initialCollapsed || depth >= maxDepth}
      />
    );
  }
  if (typeof value === "object") {
    return (
      <ObjectBlock
        obj={value as JsonRecord}
        depth={depth}
        maxDepth={maxDepth}
        initialCollapsed={initialCollapsed || depth >= maxDepth}
      />
    );
  }
  return <Primitive kind="string">{String(value)}</Primitive>;
}

function ObjectBlock({
  obj,
  depth,
  maxDepth,
  initialCollapsed,
}: {
  obj: JsonRecord;
  depth: number;
  maxDepth: number;
  initialCollapsed: boolean;
}) {
  const entries = Object.entries(obj);
  const [open, setOpen] = useState(!initialCollapsed);
  if (entries.length === 0) {
    return <span className="font-mono text-[11px] text-ink-500">{"{} (empty)"}</span>;
  }
  if (depth === 0) {
    return <KeyValueTable entries={entries} depth={depth} maxDepth={maxDepth} />;
  }
  return (
    <Collapsible
      open={open}
      onToggle={() => setOpen((v) => !v)}
      summary={`{ ${entries.length} field${entries.length === 1 ? "" : "s"} }`}
    >
      <div className="ml-3 mt-1 border-l border-brand-500/10 pl-3">
        <KeyValueTable entries={entries} depth={depth + 1} maxDepth={maxDepth} compact />
      </div>
    </Collapsible>
  );
}

function ArrayBlock({
  items,
  depth,
  maxDepth,
  initialCollapsed,
}: {
  items: unknown[];
  depth: number;
  maxDepth: number;
  initialCollapsed: boolean;
}) {
  const [open, setOpen] = useState(!initialCollapsed);
  if (items.length === 0) {
    return <span className="font-mono text-[11px] text-ink-500">[] (empty)</span>;
  }

  const objectTable = tableShape(items);
  if (objectTable) {
    return (
      <ArrayObjectTable
        rows={objectTable.rows}
        columns={objectTable.columns}
        depth={depth}
        maxDepth={maxDepth}
        initialCollapsed={initialCollapsed}
      />
    );
  }

  const allPrimitive = items.every(
    (item) =>
      item === null ||
      typeof item === "string" ||
      typeof item === "number" ||
      typeof item === "boolean",
  );
  if (allPrimitive && items.length <= 12) {
    return (
      <div className="flex flex-wrap gap-1">
        {items.map((item, idx) => (
          <span key={idx} className="font-mono text-[11px]">
            <Node value={item} depth={depth + 1} maxDepth={maxDepth} initialCollapsed={false} />
          </span>
        ))}
      </div>
    );
  }
  if (allPrimitive) {
    return (
      <PrimitiveArrayTable
        items={items}
        depth={depth}
        maxDepth={maxDepth}
        initialCollapsed={initialCollapsed}
      />
    );
  }
  return (
    <Collapsible
      open={open}
      onToggle={() => setOpen((v) => !v)}
      summary={`[ ${items.length} item${items.length === 1 ? "" : "s"} ]`}
    >
      <ol className="ml-3 mt-1 list-none space-y-1 border-l border-brand-500/10 pl-3">
        {items.map((child, idx) => (
          <li key={idx} className="flex min-w-0 items-start gap-2">
            <span className="shrink-0 pt-0.5 font-mono text-[10px] text-ink-500">
              {idx}
            </span>
            <div className="min-w-0 flex-1">
              <Node value={child} depth={depth + 1} maxDepth={maxDepth} initialCollapsed={false} />
            </div>
          </li>
        ))}
      </ol>
    </Collapsible>
  );
}

function KeyValueTable({
  entries,
  depth,
  maxDepth,
  compact = false,
}: {
  entries: [string, unknown][];
  depth: number;
  maxDepth: number;
  compact?: boolean;
}) {
  return (
    <div className="embedded-scroll max-w-full overflow-auto">
      <table className="min-w-full table-fixed border-separate border-spacing-0 text-left">
        <tbody className="align-top">
          {entries.map(([key, child]) => (
            <KeyValueRow
              key={key}
              keyText={key}
              child={child}
              depth={depth}
              maxDepth={maxDepth}
              compact={compact}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function KeyValueRow({
  keyText,
  child,
  depth,
  maxDepth,
  compact,
}: {
  keyText: string;
  child: unknown;
  depth: number;
  maxDepth: number;
  compact: boolean;
}) {
  const isContainer =
    child !== null &&
    typeof child === "object" &&
    (Array.isArray(child) || Object.keys(child as JsonRecord).length > 0);
  return (
    <tr className="group">
      <th
        scope="row"
        className={`w-[36%] max-w-[220px] border-b border-brand-500/10 pr-3 align-top text-left font-mono text-[11px] font-medium text-ink-300 ${
          compact ? "py-1.5" : "py-2"
        }`}
        title={keyText}
      >
        <span className="block truncate">{keyText}</span>
      </th>
      <td
        className={`min-w-0 border-b border-brand-500/10 align-top text-[11px] text-ink-200 ${
          compact ? "py-1.5" : "py-2"
        } ${isContainer ? "" : "font-mono"}`}
      >
        <div className="min-w-0">
          <Node value={child} depth={depth} maxDepth={maxDepth} initialCollapsed={false} />
        </div>
      </td>
    </tr>
  );
}

function ArrayObjectTable({
  rows,
  columns,
  depth,
  maxDepth,
  initialCollapsed,
}: {
  rows: JsonRecord[];
  columns: string[];
  depth: number;
  maxDepth: number;
  initialCollapsed: boolean;
}) {
  const [open, setOpen] = useState(!initialCollapsed);
  const table = (
    <div className="embedded-scroll max-w-full overflow-auto">
      <table className="min-w-[680px] table-auto border-separate border-spacing-0 text-left">
        <thead>
          <tr>
            <th className="sticky left-0 z-10 w-10 border-b border-brand-500/20 bg-ink-950/95 px-2 py-2 text-[11px] font-medium text-ink-500">
              #
            </th>
            {columns.map((column) => (
              <th
                key={column}
                className="min-w-[140px] border-b border-brand-500/20 bg-ink-950/80 px-2 py-2 text-[11px] font-medium text-ink-400"
                title={column}
              >
                <span className="block truncate">{column}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex} className="group">
              <td className="sticky left-0 z-10 border-b border-brand-500/10 bg-ink-950/95 px-2 py-2 align-top font-mono text-[10px] text-ink-500">
                {rowIndex}
              </td>
              {columns.map((column) => (
                <td
                  key={column}
                  className="max-w-[260px] border-b border-brand-500/10 px-2 py-2 align-top text-[11px] text-ink-200"
                >
                  <CellValue value={row[column]} depth={depth + 1} maxDepth={maxDepth} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

  if (depth === 0) return table;
  return (
    <Collapsible
      open={open}
      onToggle={() => setOpen((v) => !v)}
      summary={`table: ${rows.length} row${rows.length === 1 ? "" : "s"} x ${columns.length} column${columns.length === 1 ? "" : "s"}`}
    >
      <div className="mt-1">{table}</div>
    </Collapsible>
  );
}

function PrimitiveArrayTable({
  items,
  depth,
  maxDepth,
  initialCollapsed,
}: {
  items: unknown[];
  depth: number;
  maxDepth: number;
  initialCollapsed: boolean;
}) {
  const [open, setOpen] = useState(!initialCollapsed);
  const table = (
    <div className="embedded-scroll max-w-full overflow-auto">
      <table className="min-w-[320px] table-fixed border-separate border-spacing-0 text-left">
        <tbody>
          {items.map((item, idx) => (
            <tr key={idx}>
              <th
                scope="row"
                className="w-14 border-b border-brand-500/10 px-2 py-1.5 text-left font-mono text-[10px] font-medium text-ink-500"
              >
                {idx}
              </th>
              <td className="border-b border-brand-500/10 px-2 py-1.5">
                <Node value={item} depth={depth + 1} maxDepth={maxDepth} initialCollapsed={false} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
  if (depth === 0) return table;
  return (
    <Collapsible
      open={open}
      onToggle={() => setOpen((v) => !v)}
      summary={`[ ${items.length} values ]`}
    >
      <div className="mt-1">{table}</div>
    </Collapsible>
  );
}

function CellValue({
  value,
  depth,
  maxDepth,
}: {
  value: unknown;
  depth: number;
  maxDepth: number;
}) {
  if (isPlainRecord(value) || Array.isArray(value)) {
    return (
      <Node
        value={value}
        depth={depth}
        maxDepth={maxDepth}
        initialCollapsed
      />
    );
  }
  return <Node value={value} depth={depth} maxDepth={maxDepth} initialCollapsed={false} />;
}

function tableShape(items: unknown[]): { rows: JsonRecord[]; columns: string[] } | null {
  if (!items.length || !items.every(isPlainRecord)) return null;
  const rows = items as JsonRecord[];
  const columns: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!columns.includes(key)) columns.push(key);
    }
  }
  if (!columns.length) return null;
  return { rows, columns };
}

function isPlainRecord(value: unknown): value is JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function Collapsible({
  open,
  onToggle,
  summary,
  children,
}: {
  open: boolean;
  onToggle: () => void;
  summary: string;
  children: ReactNode;
}) {
  return (
    <div className="min-w-0">
      <button
        type="button"
        onClick={onToggle}
        className="inline-flex cursor-pointer items-center gap-1 text-[11px] text-ink-400 font-medium hover:text-ink-200"
      >
        {open ? (
          <ChevronDownIcon className="h-3 w-3" />
        ) : (
          <ChevronRightIcon className="h-3 w-3" />
        )}
        <span className="font-mono text-[11px] normal-case tracking-normal text-ink-400">
          {summary}
        </span>
      </button>
      {open ? <div className="min-w-0">{children}</div> : null}
    </div>
  );
}

function Primitive({
  kind,
  children,
}: {
  kind: "string" | "number" | "boolean" | "null";
  children: ReactNode;
}) {
  const cls = {
    string: "text-emerald-200/90",
    number: "text-amber-200",
    boolean: "text-sky-200",
    null: "text-ink-500 italic",
  }[kind];
  return (
    <span className={`break-all font-mono text-[11px] ${cls}`}>{children}</span>
  );
}

function quoteIfMultiline(text: string): string {
  if (!text) return '""';
  if (text.length > 120 || text.includes("\n")) {
    return `"${text.slice(0, 200)}${text.length > 200 ? "…" : ""}"`;
  }
  return `"${text}"`;
}

export default JsonView;
