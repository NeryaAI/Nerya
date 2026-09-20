import type { PortfolioPosition } from "./api";
import { contentValue, record } from "./agentConversation";
import { finiteNumber } from "./financeDisplay";

export type SnapshotAccount = { id: string; mode: string; equity: unknown; cash: unknown; fees: unknown; asOf: unknown; positions: PortfolioPosition[]; positionsKnown: boolean };
export function portfolioSnapshot(block: Record<string, unknown>): SnapshotAccount[] | null {
  if (block.action !== "portfolio_summary" || block.ok === false || block.error) return null;
  const raw = record(block.result);
  if (raw.ok === false || raw.success === false || raw.error) return null;
  const data = record(contentValue(block.result));
  if (data.ok === false || data.success === false || data.error || !Array.isArray(data.accounts)) return null;
  if (data.accounts.some((value) => typeof record(value).id !== "string" || !String(record(value).id).trim())) return null;
  return data.accounts.map((value) => {
    const a = record(value), positions = a.positions;
    const entries: [string, unknown][] = Array.isArray(positions) ? positions.map((p) => [String(record(p).market || ""), p]) : Object.entries(record(positions));
    const positionsKnown = positions !== null && typeof positions === "object" && entries.every(([, p]) => Object.keys(record(p)).length > 0);
    return { id: String(a.id), mode: String(a.mode || ""), equity: a.equity_usd, cash: a.cash_usd, fees: a.fees_paid_usd, asOf: record(a.snapshot).ts ?? a.as_of,
      positionsKnown, positions: entries.filter(([, value]) => Object.keys(record(value)).length && finiteNumber(record(value).size_base ?? record(value).size) !== 0).map(([market, value]) => ({ ...record(value), account_id: String(a.id), market: String(record(value).market || market) }) as PortfolioPosition),
    };
  });
}
