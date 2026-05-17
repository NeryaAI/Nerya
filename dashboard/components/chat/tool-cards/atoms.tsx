"use client";

/**
 * Shared visual atoms used by every tool-card renderer (and by the
 * generic ``Collapsible``/``JsonBlock`` fallbacks in ``TurnBlocks``).
 *
 * Keeping these in a separate file avoids importing the full renderer
 * when a card only needs a small visual primitive.
 */

import { ReactNode, useState } from "react";
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
    warn: "bg-[#f5a524]/10 border-[#f5a524]/40 text-[#f5a524]",
    err: "bg-[#ef5564]/10 border-[#ef5564]/40 text-[#ef5564]",
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
