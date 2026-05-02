"use client";

import { Kpi } from "../Page";

export function SummaryCards({
  cards,
}: {
  cards: Array<Record<string, unknown>>;
}) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
      {cards.map((card) => (
        <Kpi
          key={String(card.label)}
          label={String(card.label)}
          value={formatValue(card.value)}
          tone={toneOf(String(card.tone || "neutral"))}
        />
      ))}
    </div>
  );
}

function toneOf(tone: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  if (tone === "positive") return "ok";
  if (tone === "negative") return "danger";
  if (tone === "warning") return "warn";
  return "neutral";
}

function formatValue(value: unknown): string {
  if (typeof value === "number") return Number.isFinite(value) ? value.toFixed(2) : "inf";
  return String(value ?? "null");
}

