"use client";

export function SummaryCards({
  cards,
}: {
  cards: Array<Record<string, unknown>>;
}) {
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,9.5rem),1fr))] gap-3">
      {cards.map((card) => {
        const label = String(card.label ?? "metric");
        const value = formatValue(card.value, label);
        return (
          <div
            key={label}
            className="min-w-0 rounded-lg border border-[color:var(--line)] bg-[color:var(--card)] px-4 py-3.5 shadow-sm"
          >
            <div
              title={label}
              className="min-h-9 break-words text-[13px] font-semibold leading-5 text-[color:var(--text-muted)]"
            >
              {label}
            </div>
            <div
              title={rawTitle(card.value)}
              className={`mt-2 min-w-0 break-words font-semibold leading-tight ${valueSizeClass(
                value,
              )} ${toneClass(String(card.tone || "neutral"))}`}
            >
              {value}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function toneClass(tone: string): string {
  if (tone === "positive") return "text-emerald-500";
  if (tone === "negative") return "text-rose-500";
  if (tone === "warning") return "text-amber-500";
  return "text-[color:var(--text-base)]";
}

function formatValue(value: unknown, label: string): string {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return "inf";
    const normalizedLabel = label.toLowerCase();
    const suffix = normalizedLabel.includes("pct") || normalizedLabel.includes("percent") ? "%" : "";
    const formatter = normalizedLabel.includes("trade")
      ? new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 })
      : compactFormatter(value);
    return `${formatter.format(value)}${suffix}`;
  }
  return String(value ?? "null");
}

function compactFormatter(value: number): Intl.NumberFormat {
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000) {
    return new Intl.NumberFormat(undefined, {
      notation: "compact",
      maximumFractionDigits: 2,
    });
  }
  if (absolute >= 100) return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
  if (absolute >= 1) return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 });
}

function valueSizeClass(value: string): string {
  if (value.length > 14) return "text-lg";
  if (value.length > 9) return "text-xl";
  if (value.length > 6) return "text-2xl";
  return "text-3xl";
}

function rawTitle(value: unknown): string {
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "inf";
  return String(value ?? "null");
}

