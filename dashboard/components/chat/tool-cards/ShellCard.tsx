"use client";

import type { NativeBlock } from "../../../lib/chat";
import { CopyButton, Tag } from "./atoms";
import { recordOf } from "./helpers";

function shellOutputs(block: NativeBlock): {
  stdout: string;
  stderr: string;
  exit: number | null;
  pid: number | null;
  cwd: string;
} {
  const result = recordOf(block.result);
  let stdout = typeof block.result === "string" ? block.result : String(result.stdout || "");
  let stderr = String(result.stderr || "");
  if (Array.isArray(result.content)) {
    for (const part of result.content) {
      const r = recordOf(part);
      const t = String(r.type || r.kind || "");
      const text = typeof r.text === "string" ? r.text : "";
      if (!text) continue;
      if (t === "stdout" || (!stdout && t === "text")) stdout = text;
      if (t === "stderr") stderr = text;
    }
  }
  const exit = typeof result.exit_code === "number" ? result.exit_code : null;
  const pid = typeof result.pid === "number" ? result.pid : null;
  const cwd = String(result.cwd || "");
  return { stdout, stderr, exit, pid, cwd };
}

export function ShellCard({
  block,
  variant,
  pending = false,
}: {
  block: NativeBlock;
  variant: "use" | "result";
  pending?: boolean;
}) {
  const payload = recordOf(block.payload);
  const command = String(payload.command || "");
  const description = String(payload.description || "");
  const cwd = String(payload.cwd || "");
  const isResult = variant === "result";
  const ok = block.ok !== false && !block.error;
  const { stdout, stderr, exit, pid, cwd: rcwd } = shellOutputs(block);
  const ranBackground =
    block.metadata && (block.metadata as Record<string, unknown>).background === true;

  return (
    <div className="rounded-2xl border border-brand-500/15 bg-brand-500/[0.04] px-4 py-3 space-y-2.5">
      <div className="flex items-center justify-between gap-3 min-w-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="inline-flex items-center justify-center w-7 h-7 rounded-xl bg-ink-900 border border-brand-500/20 text-emerald-300">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="4 17 10 11 4 5" />
              <line x1="12" y1="19" x2="20" y2="19" />
            </svg>
          </span>
          <div className="min-w-0">
            <div className="text-[11px] text-ink-500 font-medium">
              Shell
              {pending ? (
                <span className="ml-2 inline-flex items-center gap-1 text-fluid-400 normal-case tracking-normal">
                  <span className="typing-dot" />
                  <span>running</span>
                </span>
              ) : null}
            </div>
            {description ? (
              <div className="text-[12.5px] text-ink-100 truncate">{description}</div>
            ) : (
              <div className="text-[12.5px] text-ink-200 truncate font-mono">
                {command.length > 120 ? `${command.slice(0, 120)}\u2026` : command}
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {ranBackground ? <Tag tone="brand">background</Tag> : null}
          {isResult ? (
            ok ? (
              exit === 0 ? (
                <Tag tone="ok">exit 0</Tag>
              ) : exit !== null ? (
                <Tag tone="warn">{`exit ${exit}`}</Tag>
              ) : (
                <Tag tone="ok">ok</Tag>
              )
            ) : (
              <Tag tone="err">{(block.error_kind as string | undefined) || "error"}</Tag>
            )
          ) : null}
          {typeof block.elapsed_ms === "number" ? <Tag>{block.elapsed_ms}ms</Tag> : null}
          {pid !== null ? <Tag>{`pid ${pid}`}</Tag> : null}
        </div>
      </div>
      <div className="rounded-xl border border-brand-500/10 bg-ink-950 overflow-hidden">
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-brand-500/10">
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-rose-400/80" />
            <span className="w-2 h-2 rounded-full bg-amber-300/80" />
            <span className="w-2 h-2 rounded-full bg-emerald-400/80" />
            <span className="ml-2 text-[11px] text-ink-500 font-medium font-mono">
              {rcwd || cwd || "shell"}
            </span>
          </div>
          <CopyButton text={command} />
        </div>
        <div className="px-3 py-2.5 font-mono text-[11.5px] leading-relaxed overflow-auto max-h-72">
          <div className="text-emerald-300">
            <span className="text-ink-500 select-none">$ </span>
            <span className="text-ink-100">{command}</span>
          </div>
          {isResult && stdout ? (
            <pre className="text-ink-200 whitespace-pre-wrap mt-2">{stdout}</pre>
          ) : null}
          {isResult && stderr ? (
            <pre className="text-rose-300 whitespace-pre-wrap mt-2">{stderr}</pre>
          ) : null}
          {isResult && !stdout && !stderr && !block.error ? (
            <div className="text-ink-500 italic mt-2">(no output)</div>
          ) : null}
        </div>
      </div>
      {isResult && block.error ? (
        <div className="text-[12px] text-rose-300 break-words">{String(block.error)}</div>
      ) : null}
    </div>
  );
}
