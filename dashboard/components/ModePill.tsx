"use client";

import { Pill } from "./Page";

/**
 * Shared paper/live badge.
 *
 * One visual language for trading-mode across the console:
 * ``LIVE``  → danger red (real money — must read as "hot")
 * ``PAPER`` → brand violet (simulated — calm)
 *
 * ``PAPER``/``LIVE`` are deliberately NOT translated: they are trading
 * terms (like ticker symbols) that operators scan for, in either locale.
 */
export type TradingMode = "paper" | "live";

export function modeTone(mode: string | undefined | null) {
  return mode === "live" ? "danger" : "brand";
}

export function ModePill({ mode }: { mode: string | undefined | null }) {
  if (mode !== "paper" && mode !== "live") return null;
  return (
    <Pill tone={mode === "live" ? "danger" : "brand"}>{mode === "live" ? "LIVE" : "PAPER"}</Pill>
  );
}
