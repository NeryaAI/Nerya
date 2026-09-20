import type { PortfolioPosition } from "./api";
import { finiteNumber, positionSide } from "./financeDisplay";

/** Gross reported notional only: never substitute cash, equity, or inferred marks. */
export function positionExposure(positions: PortfolioPosition[]) {
  const markets = new Map<string, { market: string; value: number; count: number }>();
  const directions = { long: 0, short: 0, unknown: 0 };
  let known = 0, missing = 0, gross = 0;
  for (const position of positions) {
    const value = finiteNumber(position.notional_usd);
    if (value === null) { missing += 1; continue; }
    const absolute = Math.abs(value);
    known += 1; gross += absolute;
    directions[positionSide(position)] += absolute;
    const market = position.market || "";
    const group = markets.get(market) || { market, value: 0, count: 0 };
    group.value += absolute; group.count += 1; markets.set(market, group);
  }
  return { gross, known, missing, total: positions.length, directions,
    markets: [...markets.values()].sort((a, b) => b.value - a.value || a.market.localeCompare(b.market)),
  };
}
