"use client";

import { Card, Empty, Pill } from "../Page";
import { clientApi } from "../../lib/clientApi";
import type {
  StrategyScheduleEntry,
  StrategyScheduleStatus,
} from "../../lib/strategyTypes";

interface Props {
  strategyId: string;
  status: StrategyScheduleStatus | null;
  busy: string | null;
  onSetBusy: (key: string | null) => void;
  onRefresh: () => Promise<void> | void;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
}

/**
 * Shows the trading + tuning schedule rows installed in
 * ``triggers/schedules.yml`` by ``StrategySchedulerBridge``. The
 * pause/resume buttons toggle *both* schedules together because the
 * bridge installs them in lockstep — pausing trading while leaving
 * tuning hot would let the tuner keep mutating a strategy that's
 * frozen for trading, which is exactly the foot-gun we want to avoid.
 */
export function StrategyScheduleCard({
  strategyId,
  status,
  busy,
  onSetBusy,
  onRefresh,
  onError,
  onNotice,
}: Props) {
  async function reschedule() {
    onSetBusy("schedule:reschedule");
    onError(null);
    try {
      await clientApi.strategyRuntimeSchedule(strategyId);
      onNotice("Schedules re-installed from manifest.");
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      onSetBusy(null);
    }
  }

  async function pause() {
    onSetBusy("schedule:pause");
    onError(null);
    try {
      await clientApi.strategyRuntimePause(strategyId);
      onNotice("Trading + tuning paused.");
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      onSetBusy(null);
    }
  }

  async function resume() {
    onSetBusy("schedule:resume");
    onError(null);
    try {
      await clientApi.strategyRuntimeResume(strategyId);
      onNotice("Trading + tuning resumed.");
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      onSetBusy(null);
    }
  }

  return (
    <Card
      title="Schedules"
      description="Trading tick + tuning cron rows installed in triggers/schedules.yml"
      actions={
        <div className="flex items-center gap-2">
          <button
            onClick={() => void reschedule()}
            disabled={busy !== null}
            className="btn-ghost text-xs"
          >
            {busy === "schedule:reschedule" ? "Saving…" : "Re-install"}
          </button>
          <button
            onClick={() => void pause()}
            disabled={busy !== null}
            className="text-xs rounded px-3 py-1.5 border border-[#f5a524]/40 text-[#f5a524] hover:bg-[#f5a524]/10"
          >
            {busy === "schedule:pause" ? "Pausing…" : "Pause"}
          </button>
          <button
            onClick={() => void resume()}
            disabled={busy !== null}
            className="text-xs rounded px-3 py-1.5 border border-accent-500/40 text-accent-300 hover:bg-accent-500/10"
          >
            {busy === "schedule:resume" ? "Resuming…" : "Resume"}
          </button>
        </div>
      }
    >
      {status ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <ScheduleRow label="Trading tick" entry={status.trading} />
          <ScheduleRow label="Tuning cron" entry={status.tuning} />
        </div>
      ) : (
        <Empty label="Schedules not yet installed for this strategy." />
      )}
    </Card>
  );
}

function ScheduleRow({
  label,
  entry,
}: {
  label: string;
  entry: StrategyScheduleEntry | null;
}) {
  if (!entry) {
    return (
      <div className="rounded-lg border border-brand-500/10 bg-ink-900/40 p-3">
        <div className="text-[10px] uppercase tracking-wider text-ink-500">
          {label}
        </div>
        <div className="mt-1.5 text-xs text-ink-500 italic">
          not installed
        </div>
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-900/40 p-3 text-sm">
      <div className="flex items-center justify-between">
        <div className="text-[10px] uppercase tracking-wider text-ink-500">
          {label}
        </div>
        <Pill tone={entry.enabled ? "ok" : "warn"}>
          {entry.enabled ? "enabled" : "paused"}
        </Pill>
      </div>
      <div className="mt-1 font-mono text-[12px] text-ink-200 truncate">
        {entry.id}
      </div>
      <div className="mt-1 text-[12px] text-ink-400">
        {entry.cron
          ? `cron: ${entry.cron}`
          : entry.every_seconds
            ? `every ${entry.every_seconds}s`
            : "—"}
      </div>
      <div className="mt-0.5 text-[11px] text-ink-500 truncate">
        target → <span className="font-mono">{entry.target}</span>
      </div>
    </div>
  );
}
