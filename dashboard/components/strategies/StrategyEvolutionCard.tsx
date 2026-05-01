"use client";

import { Card, Empty, Pill } from "../Page";
import { clientApi } from "../../lib/clientApi";
import type { PendingTuningProposal } from "../../lib/strategyTypes";

interface Props {
  strategyId: string;
  proposals: PendingTuningProposal[];
  /** Optional list of dropped guardrail violations from the most recent
   * tuning run. Populated by the parent if the workspace stitched in
   * the latest StrategyTuningRunResult. */
  dropped: Array<{ entry: Record<string, unknown>; reason: string }>;
  onRefresh: () => Promise<void> | void;
}

/**
 * Pending evolution / tuning proposals for the active strategy.
 *
 * "Pending" here means proposals the tuning runner has produced but
 * the operator has not yet promoted. Promoting a tuning proposal goes
 * through the existing ``evolution`` skill rather than touching
 * ``strategies/<id>`` directly.
 */
export function StrategyEvolutionCard({
  strategyId,
  proposals,
  dropped,
  onRefresh,
}: Props) {
  async function apply(proposalId: string) {
    if (!proposalId) return;
    if (
      !window.confirm(
        `Promote proposal ${proposalId} for ${strategyId}? This rewrites the strategy package.`,
      )
    )
      return;
    try {
      await clientApi.proposalApply(proposalId);
      await onRefresh();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e));
    }
  }

  async function rollback(proposalId: string) {
    if (!proposalId) return;
    try {
      await clientApi.proposalRollback(proposalId);
      await onRefresh();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <Card
      title="Pending tuning proposals"
      description="Strategy-tuner output awaiting operator approval."
    >
      {proposals.length === 0 ? (
        <Empty label="No pending tuning proposals." />
      ) : (
        <ul className="embedded-list-scroll divide-y divide-brand-500/10 text-xs">
          {proposals.map((p) => (
            <li key={p.id} className="py-2 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="font-mono text-ink-200 truncate">{p.id}</div>
                <div className="text-[11px] text-ink-400 mt-0.5">
                  {p.summary || "no summary"}
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <Pill tone="brand">{p.state}</Pill>
                  <span className="text-[11px] text-ink-500">
                    {p.ts ? new Date(p.ts).toLocaleString() : ""}
                  </span>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => void apply(p.id)}
                  className="text-[11px] px-2 py-1 rounded border border-accent-500/40 text-accent-300 hover:bg-accent-500/10"
                >
                  Promote
                </button>
                <button
                  onClick={() => void rollback(p.id)}
                  className="text-[11px] px-2 py-1 rounded border border-[#ef4560]/40 text-[#ef4560] hover:bg-[#ef4560]/10"
                >
                  Rollback
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {dropped.length > 0 && (
        <div className="mt-4">
          <div className="text-[11px] uppercase tracking-wider text-ink-500 mb-1">
            Dropped by guardrail
          </div>
          <ul className="embedded-list-scroll-sm text-[11px] space-y-1">
            {dropped.map((d, idx) => (
              <li
                key={idx}
                className="rounded border border-[#f5a524]/30 bg-[#f5a524]/10 px-2 py-1 text-[#f5a524]"
              >
                <span className="font-mono">{String(d.entry?.target ?? "—")}</span>{" "}
                — {d.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
