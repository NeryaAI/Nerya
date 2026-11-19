/** Lifecycle status, not account mode, determines the initial strategy surface. */
export function strategyLanding(status?: string, hasRuns = false, proposalId?: string | null): "performance" | "workflow" {
  if (proposalId) return "workflow";
  const state = (status || "").toLowerCase();
  return ["paper", "canary", "live", "running"].includes(state)
    || hasRuns && ["paused", "archived"].includes(state) ? "performance" : "workflow";
}

/** Preserve settlement suffixes (e.g. BTC/USDT:USDT); never guess a venue. */
export function strategyMarketTarget(value: string): { venue: string; market: string } | null {
  const colon = value.indexOf(":");
  if (colon <= 0 || colon === value.length - 1) return null;
  const venue = value.slice(0, colon).trim().toLowerCase();
  if (!/^[a-z0-9_-]+$/.test(venue)) return null;
  const market = value.slice(colon + 1).trim();
  return market ? { venue, market } : null;
}

/** The market API returns a venue-prefixed identity; legacy responses may be bare. */
export function strategyMarketResponseMatches(target: { venue: string; market: string }, returned?: string): boolean {
  if (!returned || returned === target.market) return true;
  const identity = strategyMarketTarget(returned);
  return identity?.venue === target.venue && identity.market === target.market;
}
