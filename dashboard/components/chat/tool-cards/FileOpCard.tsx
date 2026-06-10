"use client";

import { ReactNode, useState } from "react";
import type { NativeBlock } from "../../../lib/chat";
import { CopyButton, Tag } from "./atoms";
import { arrayOfRecords, recordOf } from "./helpers";

function fileIconFor(action: string): ReactNode {
  switch (action) {
    case "edit_file":
      return (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <path d="M14 2v6h6" />
          <path d="M10 13l-2 2 2 2" />
          <path d="M14 13l2 2-2 2" />
        </svg>
      );
    case "write_file":
      return (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
          <path d="M17 21v-8H7v8" />
          <path d="M7 3v5h8" />
        </svg>
      );
    case "list_dir":
    case "glob":
      return (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
        </svg>
      );
    case "grep":
      return (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="11" cy="11" r="7" />
          <path d="M21 21l-4.3-4.3" />
        </svg>
      );
    case "read_file":
    default:
      return (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <path d="M14 2v6h6" />
          <line x1="8" y1="13" x2="16" y2="13" />
          <line x1="8" y1="17" x2="14" y2="17" />
        </svg>
      );
  }
}

const FILE_LABELS: Record<string, string> = {
  read_file: "Read file",
  edit_file: "Edit file",
  write_file: "Write file",
  list_dir: "List directory",
  glob: "Glob search",
  grep: "Grep",
};

function fileLabelFor(action: string): string {
  return FILE_LABELS[action] || action;
}

function pickPath(block: NativeBlock): string {
  const payload = recordOf(block.payload);
  const result = recordOf(block.result);
  return String(
    payload.path ||
      payload.file ||
      payload.pattern ||
      payload.dir ||
      result.path ||
      result.file ||
      "",
  );
}

function pickDiffOrText(block: NativeBlock): {
  kind: "diff" | "text" | "";
  body: string;
} {
  const result = recordOf(block.result);
  const content = result.content;
  if (Array.isArray(content)) {
    for (const part of content) {
      const r = recordOf(part);
      const partKind = String(r.type || r.kind || "");
      if (partKind === "diff" && typeof r.text === "string") {
        return { kind: "diff", body: r.text };
      }
    }
    for (const part of content) {
      const r = recordOf(part);
      if (typeof r.text === "string" && r.text.trim()) {
        return { kind: "text", body: r.text };
      }
    }
  }
  if (typeof result.diff === "string") return { kind: "diff", body: result.diff };
  if (typeof result.text === "string") return { kind: "text", body: result.text };
  return { kind: "", body: "" };
}

function DiffPanel({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  return (
    <div className="rounded-xl border border-brand-500/15 bg-ink-900/40 overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-brand-500/10">
        <span className="text-[11px] text-ink-500 font-medium">
          unified diff
        </span>
        <CopyButton text={diff} />
      </div>
      <pre className="px-3 py-2 text-[11px] font-mono leading-relaxed overflow-auto max-h-72">
        {lines.map((line, i) => {
          let cls = "text-ink-300";
          if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@")) {
            cls = "text-brand-300";
          } else if (line.startsWith("+")) {
            cls = "text-emerald-300 bg-emerald-400/[0.04]";
          } else if (line.startsWith("-")) {
            cls = "text-rose-300 bg-rose-400/[0.04]";
          }
          return (
            <div key={i} className={`${cls} px-1`}>
              {line || "\u00A0"}
            </div>
          );
        })}
      </pre>
    </div>
  );
}

export function FileOpCard({
  block,
  variant,
  pending = false,
}: {
  block: NativeBlock;
  variant: "use" | "result";
  pending?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const action = String(block.action || "").toLowerCase();
  const label = fileLabelFor(action);
  const path = pickPath(block);
  const ok = block.ok !== false && !block.error;
  const isResult = variant === "result";
  const result = recordOf(block.result);
  const payload = recordOf(block.payload);

  const { kind: bodyKind, body } = isResult
    ? pickDiffOrText(block)
    : { kind: "" as const, body: "" };
  const lineRange =
    typeof payload.line_offset === "number" || typeof payload.line_limit === "number"
      ? `lines ${payload.line_offset ?? 1}\u2013${
          (Number(payload.line_offset) || 1) + (Number(payload.line_limit) || 0)
        }`
      : "";
  const matches = arrayOfRecords(result.matches);
  const entries = arrayOfRecords(result.entries);
  const truncated = result.truncated === true;
  const previewBytes =
    typeof result.bytes === "number"
      ? `${result.bytes} bytes`
      : typeof result.size === "number"
      ? `${result.size} bytes`
      : "";

  const lines = body ? body.split("\n") : [];
  const previewLineCount = action === "read_file" ? 12 : 6;
  const preview = lines.slice(0, previewLineCount).join("\n");
  const hasMore = lines.length > previewLineCount;

  return (
    <div className="rounded-2xl border border-brand-500/15 bg-brand-500/[0.04] px-4 py-3 space-y-2.5">
      <div className="flex items-center justify-between gap-3 min-w-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="inline-flex items-center justify-center w-7 h-7 rounded-xl bg-brand-500/10 border border-brand-500/25 text-brand-200">
            {fileIconFor(action)}
          </span>
          <div className="min-w-0">
            <div className="text-[11px] text-ink-500 font-medium">
              {isResult ? `${label} \u00B7 result` : label}
              {pending ? (
                <span className="ml-2 inline-flex items-center gap-1 text-fluid-400 normal-case tracking-normal">
                  <span className="typing-dot" />
                  <span>running</span>
                </span>
              ) : null}
            </div>
            <div className="text-[12.5px] font-mono text-ink-100 truncate">
              {path || (action === "grep" ? String(payload.pattern || "") : "\u2014")}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {isResult ? (
            ok ? (
              <Tag tone="ok">
                {action === "edit_file" || action === "write_file" ? "applied" : "ok"}
              </Tag>
            ) : (
              <Tag tone="err">{(block.error_kind as string | undefined) || "error"}</Tag>
            )
          ) : null}
          {previewBytes ? <Tag>{previewBytes}</Tag> : null}
          {truncated ? <Tag tone="warn">truncated</Tag> : null}
          {typeof block.elapsed_ms === "number" ? <Tag>{block.elapsed_ms}ms</Tag> : null}
        </div>
      </div>

      {!isResult && lineRange ? (
        <div className="text-[11px] text-ink-400 font-mono">{lineRange}</div>
      ) : null}

      {!isResult && action === "grep" && payload.pattern ? (
        <div className="rounded-lg border border-brand-500/10 bg-ink-900/40 px-3 py-1.5">
          <div className="text-[11px] text-ink-500 font-medium mb-0.5">
            pattern
          </div>
          <div className="text-[12px] text-ink-100 font-mono break-words">
            {String(payload.pattern || "")}
          </div>
        </div>
      ) : null}

      {isResult && block.error ? (
        <div className="text-[12px] text-rose-300 break-words">{String(block.error)}</div>
      ) : null}

      {isResult && bodyKind === "diff" && body ? <DiffPanel diff={body} /> : null}

      {isResult && bodyKind === "text" && body && action === "read_file" ? (
        <div className="rounded-xl border border-brand-500/15 bg-ink-900/40 overflow-hidden">
          <div className="flex items-center justify-between px-3 py-1.5 border-b border-brand-500/10">
            <span className="text-[11px] text-ink-500 font-medium">
              file contents
            </span>
            <CopyButton text={body} />
          </div>
          <pre
            className={`px-3 py-2 text-[11px] font-mono leading-relaxed text-ink-200 whitespace-pre overflow-auto ${
              expanded ? "max-h-[640px]" : "max-h-48"
            }`}
          >
            {expanded ? body : preview}
          </pre>
          {hasMore ? (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="w-full text-[12px] text-brand-300 hover:text-brand-200 cursor-pointer transition-colors py-1.5 border-t border-brand-500/10"
            >
              {expanded ? "\u25BE collapse" : `\u25B8 show all ${lines.length} lines`}
            </button>
          ) : null}
        </div>
      ) : null}

      {isResult && (action === "list_dir" || action === "glob") && entries.length ? (
        <div className="rounded-xl border border-brand-500/15 bg-ink-900/40 px-3 py-2 max-h-56 overflow-auto">
          <div className="text-[11px] text-ink-500 font-medium mb-1.5">
            {entries.length} {entries.length === 1 ? "entry" : "entries"}
          </div>
          <ul className="space-y-0.5 font-mono text-[11px] text-ink-200">
            {entries.slice(0, 200).map((entry, i) => {
              const name = String(entry.name || entry.path || "");
              const isDir = entry.is_dir === true || String(entry.kind || "") === "dir";
              const size = typeof entry.size === "number" ? `  ${entry.size}b` : "";
              return (
                <li key={i} className="flex items-center gap-2">
                  <span className={isDir ? "text-brand-300" : "text-ink-400"}>
                    {isDir ? "\u25B8" : "\u00B7"}
                  </span>
                  <span className={isDir ? "text-brand-200" : ""}>{name}</span>
                  <span className="ml-auto text-ink-500">{size}</span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {isResult && action === "grep" && matches.length ? (
        <div className="rounded-xl border border-brand-500/15 bg-ink-900/40 px-3 py-2 max-h-72 overflow-auto">
          <div className="text-[11px] text-ink-500 font-medium mb-1.5">
            {matches.length} {matches.length === 1 ? "match" : "matches"}
          </div>
          <ul className="space-y-1 font-mono text-[11px]">
            {matches.slice(0, 100).map((m, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-2">
                <span className="text-brand-300">{String(m.path || "")}</span>
                <span className="text-ink-500">
                  :{String(m.line || m.line_number || "?")}
                </span>
                <span className="text-ink-200 break-all">
                  {String(m.text || m.match || "")}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
