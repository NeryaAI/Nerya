import { copy as i18nCopy } from "./i18n";
/** Versioned UI locators, never execution commands or authority to trade. */
export type StrategyDetailTarget =
  | { kind: "strategy"; strategyId: string; proposalId?: string | null; title?: string }
  | { kind: "backtest"; strategyId: string; proposalId?: string | null; ts: string; title?: string };

export function strategyDetailId(target: StrategyDetailTarget): string {
  const identity = [target.strategyId, target.proposalId || ""];
  if (target.kind === "backtest") identity.push(target.ts);
  return `${target.kind}:${encodeURIComponent(JSON.stringify(identity))}`;
}

export function parseStrategyDetailId(id: string): StrategyDetailTarget | null {
  const kind = id.slice(0, id.indexOf(":"));
  if (kind !== "strategy" && kind !== "backtest") return null;
  try {
    const parts: unknown = JSON.parse(decodeURIComponent(id.slice(kind.length + 1)));
    if (!Array.isArray(parts) || parts.length !== (kind === "backtest" ? 3 : 2)
      || !parts.every(part => typeof part === "string" && part.length <= 4096)
      || !parts[0] || (kind === "backtest" && !parts[2])) return null;
    const [strategyId, proposalId, ts] = parts as string[];
    return kind === "backtest" ? { kind, strategyId, proposalId: proposalId || null, ts }
      : { kind, strategyId, proposalId: proposalId || null };
  } catch { return null; }
}

export function strategyDetailLabel(target: StrategyDetailTarget, zh: boolean): string {
  const name = target.title || target.strategyId;
  return target.kind === "strategy" ? `${i18nCopy(zh, "copy.lib_strategyDetail.001")} · ${name}`
    : `${i18nCopy(zh, "copy.lib_strategyDetail.002")} · ${name} · ${target.ts}`;
}
