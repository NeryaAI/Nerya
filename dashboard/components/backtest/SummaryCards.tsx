"use client";

export function SummaryCards({
  cards,
}: {
  cards: Array<Record<string, unknown>>;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {cards.map((card) => {
        const label = String(card.label ?? "metric");
        const value = formatValue(card.value, label);
        return (
          <div
            key={label}
            className="w-full min-w-0 rounded-md border border-[color:var(--line)] bg-[color:var(--card)] px-3 py-2 sm:w-[10.5rem]"
          >
            <div
              title={label}
              className="break-words text-[11px] font-medium leading-4 text-[color:var(--text-muted)]"
            >
              {label}
            </div>
            <div
              title={rawTitle(card.value)}
              className={`mt-1 min-w-0 break-words font-mono font-semibold leading-tight tabular-nums ${valueSizeClass(
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
  if (value.length > 14) return "text-[13px]";
  if (value.length > 9) return "text-[15px]";
  if (value.length > 6) return "text-[17px]";
  return "text-[19px]";
}

function rawTitle(value: unknown): string {
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "inf";
  return String(value ?? "null");
}
