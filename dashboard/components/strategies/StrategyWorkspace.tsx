"use client";

/**
 * StrategyWorkspace — Phase 8 of the agent-generated strategy runtime
 * refactor.
 *
 * Renders the operator surface for one strategy package. Talks to the
 * ``/strategies/runtime/*`` endpoints exposed by
 * :mod:`nerya.api.routes_strategies_runtime` via ``clientApi``. Layout:
 *
 *  +--------------------------------------------------------------+
 *  | Status row  · mode/kill switch/last run                      |
 *  +-------------------------+------------------------------------+
 *  | Schedules + actions     | Tuning: prompt, schedule, snapshot |
 *  +-------------------------+------------------------------------+
 *  | Recent runs             | Pending evolution proposals        |
 *  +-------------------------+------------------------------------+
 *  | Strategy history ledgers (per-event tail panels)             |
 *  +--------------------------------------------------------------+
 *
 * The component is deliberately presentational + light-imperative: it
 * fetches once on mount, keeps a single ``StrategyWorkspaceEnvelope``
 * in state, and exposes a ``refresh()`` callback so child panels can
 * trigger a re-pull after they mutate the strategy.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { Card, Empty, ErrorBanner, Kpi, Pill } from "../Page";
import { clientApi } from "../../lib/clientApi";
import type {
  StrategyRunRecord,
  StrategyTuningStatusEnvelope,
  StrategyWorkspaceEnvelope,
} from "../../lib/strategyTypes";

import { StrategyStatusBar } from "./StrategyStatusBar";
import { StrategyScheduleCard } from "./StrategyScheduleCard";
import { StrategyTuningCard } from "./StrategyTuningCard";
import { StrategyRunsCard } from "./StrategyRunsCard";
import { StrategyHistoryCard } from "./StrategyHistoryCard";
import { StrategyEvolutionCard } from "./StrategyEvolutionCard";

export interface StrategyWorkspaceProps {
  strategyId: string;
  /** Optional callback fired after each successful refresh. */
  onRefreshed?: (envelope: StrategyWorkspaceEnvelope) => void;
}

export function StrategyWorkspace({
  strategyId,
  onRefreshed,
}: StrategyWorkspaceProps) {
  const [envelope, setEnvelope] = useState<StrategyWorkspaceEnvelope | null>(
    null,
  );
  const [tuning, setTuning] = useState<StrategyTuningStatusEnvelope | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!strategyId) return;
    setLoading(true);
    setError(null);
    try {
      const [ws, tn] = await Promise.all([
        clientApi.strategyRuntimeWorkspace(strategyId, 50),
        clientApi.strategyRuntimeTuningStatus(strategyId).catch((err: unknown) => {
          // Tuning status is optional — if a strategy hasn't enabled
          // tuning yet, the backend returns an envelope with ok=false
          // rather than 404. We mirror that here so the panel can
          // render its empty state without taking down the workspace.
          return {
            ok: false,
            strategy_id: strategyId,
            error: err instanceof Error ? err.message : String(err),
          } as StrategyTuningStatusEnvelope;
        }),
      ]);
      setEnvelope(ws);
      setTuning(tn);
      onRefreshed?.(ws);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [strategyId, onRefreshed]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const lastRun: StrategyRunRecord | null = useMemo(
    () => envelope?.last_run ?? null,
    [envelope],
  );

  const pendingProposals = useMemo(
    () => tuning?.pending_proposals ?? [],
    [tuning],
  );

  if (!strategyId) {
    return <Empty label="Pick a strategy from the list to manage it." />;
  }

  return (
    <div className="space-y-4">
      {error && <ErrorBanner error={error} />}
      {notice && (
        <div className="rounded-lg border border-accent-500/30 bg-accent-500/10 px-4 py-2 text-sm text-accent-300">
          {notice}
        </div>
      )}

      {!envelope && loading ? (
        <Empty label={`Loading ${strategyId}…`} />
      ) : !envelope || !envelope.ok ? (
        <Card title={`Strategy ${strategyId}`}>
          <Empty
            label={
              envelope?.error
                ? `Backend error: ${envelope.error}`
                : "Strategy package is not yet promoted."
            }
          />
        </Card>
      ) : (
        <>
          <StrategyStatusBar
            envelope={envelope}
            busy={busy}
            disabled={loading}
            onSetBusy={setBusy}
            onRefresh={refresh}
            onError={setError}
            onNotice={setNotice}
          />

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            <StrategyScheduleCard
              strategyId={strategyId}
              status={envelope.schedules ?? null}
              busy={busy}
              onSetBusy={setBusy}
              onRefresh={refresh}
              onError={setError}
              onNotice={setNotice}
            />
            <StrategyTuningCard
              strategyId={strategyId}
              tuning={tuning}
              busy={busy}
              onSetBusy={setBusy}
              onRefresh={refresh}
              onError={setError}
              onNotice={setNotice}
            />
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            <StrategyRunsCard
              runs={envelope.runs?.runs ?? (lastRun ? [lastRun] : [])}
              total={envelope.runs?.count ?? (lastRun ? 1 : 0)}
            />
            <StrategyEvolutionCard
              strategyId={strategyId}
              proposals={pendingProposals}
              dropped={[]}
              onRefresh={refresh}
            />
          </div>

          <StrategyHistoryCard
            strategyId={strategyId}
            history={envelope.history ?? null}
          />
        </>
      )}

      <div className="flex items-center justify-between text-[11px] text-ink-500">
        <span>
          {loading
            ? `Refreshing ${strategyId}…`
            : `Last refresh ${new Date().toLocaleTimeString()}`}
        </span>
        <button
          onClick={() => void refresh()}
          disabled={loading}
          className="btn-ghost text-xs"
        >
          {loading ? "…" : "Refresh"}
        </button>
      </div>
    </div>
  );
}

export function summariseRunStatus(run: StrategyRunRecord | null) {
  if (!run) return { tone: "neutral" as const, label: "no runs yet" };
  switch (run.status) {
    case "submitted":
      return { tone: "ok" as const, label: "submitted" };
    case "ok":
      return { tone: "ok" as const, label: "ok" };
    case "hold":
      return { tone: "warn" as const, label: "hold" };
    case "error":
      return { tone: "danger" as const, label: "error" };
    default:
      return { tone: "neutral" as const, label: run.status };
  }
}

export type { StrategyWorkspaceEnvelope } from "../../lib/strategyTypes";
export { Kpi, Pill };
