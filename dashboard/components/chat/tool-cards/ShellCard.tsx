"use client";

import type { NativeBlock } from "../../../lib/chat";
import { CopyButton, Tag, ToolRowCard } from "./atoms";
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
  const title = description || "Ran command";
  const subtitle = command.length > 120 ? `${command.slice(0, 120)}\u2026` : command;

  return (
    <ToolRowCard
      icon={
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="4 17 10 11 4 5" />
          <line x1="12" y1="19" x2="20" y2="19" />
        </svg>
      }
      title={
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <span className="shrink-0">Ran command</span>
          {pending ? (
            <span className="inline-flex items-center gap-1 text-[10px] text-fluid-400">
              <span className="typing-dot" />
              <span>running</span>
            </span>
          ) : null}
        </span>
      }
      subtitle={
        <span className="font-mono">
          {title === "Ran command" ? subtitle : `${title} · ${subtitle}`}
        </span>
      }
      tone={ok ? "neutral" : "err"}
      defaultOpen={pending}
      meta={
        <>
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
        </>
      }
    >
      <div className="rounded-lg border border-brand-500/10 bg-ink-950 overflow-hidden">
        <div className="flex items-center justify-between px-2.5 py-1.5 border-b border-brand-500/10">
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
        <div className="px-2.5 py-2 font-mono text-[11px] leading-relaxed overflow-auto max-h-72">
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
    </ToolRowCard>
  );
}
