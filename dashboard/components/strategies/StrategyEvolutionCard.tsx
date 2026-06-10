"use client";

import { useTranslations } from "next-intl";
import { Card, Empty, Pill } from "../Page";
import { clientApi } from "../../lib/clientApi";
import { alert as alertDialog, confirm as confirmDialog } from "../../lib/dialogs";
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
  const t = useTranslations("strategyEvolution");
  async function apply(proposalId: string) {
    if (!proposalId) return;
    const confirmed = await confirmDialog({
      message: t("promoteConfirm", { id: proposalId, strategyId }),
      tone: "brand",
    });
    if (!confirmed) return;
    try {
      await clientApi.proposalApply(proposalId);
      await onRefresh();
    } catch (e) {
      await alertDialog({
        message: e instanceof Error ? e.message : String(e),
        tone: "danger",
      });
    }
  }

  async function rollback(proposalId: string) {
    if (!proposalId) return;
    try {
      await clientApi.proposalRollback(proposalId);
      await onRefresh();
    } catch (e) {
      await alertDialog({
        message: e instanceof Error ? e.message : String(e),
        tone: "danger",
      });
    }
  }

  return (
    <Card
      title={t("title")}
      description={t("description")}
    >
      {proposals.length === 0 ? (
        <Empty label={t("noProposals")} />
      ) : (
        <ul className="embedded-list-scroll divide-y divide-brand-500/10 text-xs">
          {proposals.map((p) => (
            <li key={p.id} className="py-2 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="font-mono text-ink-200 truncate">{p.id}</div>
                <div className="text-[11px] text-ink-400 mt-0.5">
                  {p.summary || t("noSummary")}
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
                  {t("promote")}
                </button>
                <button
                  onClick={() => void rollback(p.id)}
                  className="text-[11px] px-2 py-1 rounded border border-danger/40 text-danger hover:bg-danger/10"
                >
                  {t("rollback")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {dropped.length > 0 && (
        <div className="mt-4">
          <div className="text-[12px] text-ink-500 font-medium mb-1">
            {t("droppedByGuardrail")}
          </div>
          <ul className="embedded-list-scroll-sm text-[11px] space-y-1">
            {dropped.map((d, idx) => (
              <li
                key={idx}
                className="rounded border border-warn/30 bg-warn/10 px-2 py-1 text-warn"
              >
                <span className="font-mono">{String(d.entry?.target ?? "–")}</span>{" "}
                <span className="text-[color:var(--text-muted)]">·</span> {d.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
