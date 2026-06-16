"use client";

/**
 * Shared visual atoms used by every tool-card renderer (and by the
 * generic ``Collapsible``/``JsonBlock`` fallbacks in ``TurnBlocks``).
 *
 * Keeping these in a separate file avoids importing the full renderer
 * when a card only needs a small visual primitive.
 */

import { Children, ReactNode, useState } from "react";
import { CheckIcon, CopyIcon } from "../../icons";

export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  if (!text) return null;
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        } catch {
          // Clipboard access can be blocked outside secure contexts.
        }
      }}
      className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-brand-500/20 text-ink-400 hover:text-white hover:border-brand-500/40 transition-colors"
      title={copied ? "Copied" : "Copy"}
      aria-label={copied ? "Copied" : "Copy"}
    >
      {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
    </button>
  );
}

export type TagTone = "neutral" | "ok" | "warn" | "err" | "brand";

export function Tag({
  tone = "neutral",
  children,
}: {
  tone?: TagTone;
  children: ReactNode;
}) {
  const cls = {
    neutral: "bg-ink-900/60 border-ink-700 text-ink-300",
    ok: "bg-brand-500/15 border-brand-500/40 text-brand-300",
    warn: "bg-warn/10 border-warn/40 text-warn",
    err: "bg-danger/10 border-danger/40 text-danger",
    brand: "bg-brand-500/15 border-brand-500/40 text-brand-300",
  }[tone];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-mono ${cls}`}
    >
      {children}
    </span>
  );
}

export function PendingDot({ label = "running" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-1 text-fluid-400 normal-case tracking-normal">
      <span className="typing-dot" />
      <span>{label}</span>
    </span>
  );
}

function RowChevron({ open }: { open: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      className={`transition-transform duration-150 ${open ? "rotate-90" : ""}`}
    >
      <path d="M9 6l6 6-6 6" />
    </svg>
  );
}

export function ToolRowCard({
  icon,
  title,
  subtitle,
  meta,
  children,
  defaultOpen = false,
  tone = "neutral",
}: {
  icon: ReactNode;
  title: ReactNode;
  subtitle?: ReactNode;
  meta?: ReactNode;
  children?: ReactNode;
  defaultOpen?: boolean;
  tone?: "neutral" | "ok" | "warn" | "err" | "brand";
}) {
  const [open, setOpen] = useState(defaultOpen);
  const chrome = {
    neutral: "border-ink-700/70 bg-ink-900/35",
    ok: "border-brand-500/30 bg-brand-500/[0.045]",
    warn: "border-warn/35 bg-warn/[0.045]",
    err: "border-danger/40 bg-danger/[0.05]",
    brand: "border-brand-500/25 bg-brand-500/[0.045]",
  }[tone];
  const iconChrome = {
    neutral: "border-ink-700/70 bg-ink-950/60 text-ink-300",
    ok: "border-brand-500/35 bg-brand-500/10 text-brand-200",
    warn: "border-warn/35 bg-warn/10 text-warn",
    err: "border-danger/40 bg-danger/10 text-danger",
    brand: "border-brand-500/35 bg-brand-500/10 text-brand-200",
  }[tone];
  const hasBody = Children.toArray(children).length > 0;
  return (
    <div className={`overflow-hidden rounded-lg border ${chrome}`}>
      <button
        type="button"
        onClick={() => hasBody && setOpen((v) => !v)}
        className={`flex min-h-9 w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left transition-colors ${
          hasBody ? "hover:bg-ink-800/35 cursor-pointer" : "cursor-default"
        }`}
        aria-expanded={hasBody ? open : undefined}
      >
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={`inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-md border ${iconChrome}`}
          >
            {icon}
          </span>
          <span className="min-w-0">
            <span className="block truncate text-[12.5px] font-semibold leading-tight text-ink-100">
              {title}
            </span>
            {subtitle ? (
              <span className="mt-0.5 block truncate text-[11px] leading-tight text-ink-400">
                {subtitle}
              </span>
            ) : null}
          </span>
        </div>
        <span className="flex shrink-0 items-center gap-1.5">
          {meta}
          {hasBody ? (
            <span className="text-ink-500">
              <RowChevron open={open} />
            </span>
          ) : null}
        </span>
      </button>
      {hasBody && open ? (
        <div className="border-t border-ink-700/50 px-2.5 pb-2.5 pt-2">
          {children}
        </div>
      ) : null}
    </div>
  );
}
