"use client";

import type { ReactNode } from "react";
import { ChartIcon } from "../icons";

type Tone = "default" | "loading" | "error" | "empty";

const toneStyles: Record<Tone, string> = {
  default: "border-brand-500/15 bg-white/[0.02] text-ink-400",
  loading: "border-fluid-500/40 bg-fluid-500/[0.04] text-fluid-200",
  error: "border-rose-500/40 bg-rose-500/[0.06] text-rose-300",
  empty: "border-brand-500/15 bg-white/[0.02] text-ink-500",
};

export function ChartPlaceholder({
  title,
  subtitle,
  tone = "default",
  height = 220,
  children,
}: {
  title: string;
  subtitle?: string;
  tone?: Tone;
  height?: number;
  children?: ReactNode;
}) {
  return (
    <div
      className={`relative rounded-lg border ${toneStyles[tone]} flex items-center justify-center px-4 py-6`}
      style={{ minHeight: height }}
    >
      <div className="flex flex-col items-center gap-2 text-center">
        <ChartIcon className="opacity-60" size={22} />
        <div className="text-sm font-medium">{title}</div>
        {subtitle ? (
          <div className="text-xs text-ink-500 max-w-[28rem]">{subtitle}</div>
        ) : null}
        {children}
      </div>
    </div>
  );
}
