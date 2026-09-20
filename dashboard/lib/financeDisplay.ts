import type { PortfolioPosition } from "./api";

/** Missing values must not be shown as financial zeroes. */
export function finiteNumber(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
export function financeNumber(value: unknown, locale = "en", digits?: number): string {
  const n = finiteNumber(value);
  if (n === null) return "—";
  // Tiny quotes retain significant figures instead of becoming a false zero.
  const options: Intl.NumberFormatOptions = digits !== undefined ? { maximumFractionDigits: digits }
    : n !== 0 && Math.abs(n) < 1 ? { maximumSignificantDigits: 6 } : { maximumFractionDigits: 6 };
  return new Intl.NumberFormat(locale, options).format(Object.is(n, -0) ? 0 : n);
}
export function financeMoney(value: unknown, locale = "en", signed = false): string {
  const n = finiteNumber(value);
  if (n === null) return "—";
  return new Intl.NumberFormat(locale, { style: "currency", currency: "USD", maximumFractionDigits: 2, signDisplay: signed ? "exceptZero" : "auto" }).format(Math.abs(n) < .005 ? 0 : n);
}
export function financeTone(value: unknown): string {
  const n = finiteNumber(value);
  return n === null || n === 0 ? "text-[color:var(--text-muted)]" : n > 0 ? "text-ok" : "text-danger";
}
export function sumKnown(values: unknown[]): number | null {
  const numbers = values.map(finiteNumber);
  return numbers.some((n) => n === null) ? null : (numbers as number[]).reduce((a, b) => a + b, 0);
}
export function positionSide(position: PortfolioPosition): "long" | "short" | "unknown" {
  const side = String(position.side || "").toLowerCase();
  return side === "long" || side === "buy" ? "long" : side === "short" || side === "sell" ? "short" : "unknown";
}
export function chartTime(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? Math.floor(value > 1e12 ? value / 1000 : value) : null;
  if (typeof value !== "string" || !value.trim()) return null;
  if (/^\d+(\.\d+)?$/.test(value)) return chartTime(Number(value));
  const ms = Date.parse(value);
  return Number.isFinite(ms) && ms > 0 ? Math.floor(ms / 1000) : null;
}
