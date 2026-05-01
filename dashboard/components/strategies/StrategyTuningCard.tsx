"use client";

import { useEffect, useMemo, useState } from "react";

import { Card, Empty, Pill } from "../Page";
import { clientApi } from "../../lib/clientApi";
import type { StrategyTuningStatusEnvelope } from "../../lib/strategyTypes";

interface Props {
  strategyId: string;
  tuning: StrategyTuningStatusEnvelope | null;
  busy: string | null;
  onSetBusy: (key: string | null) => void;
  onRefresh: () => Promise<void> | void;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
}

/**
 * Tuning surface — of the agent-generated strategy runtime
 * refactor.
 *
 * Shows the strategy tuner's current prompt, schedule, and the most
 * recent performance snapshot. Lets the operator:
 *   - inline-edit and re-generate the tuning block (`tuning_generate`),
 *   - install/pause/resume the tuning schedule, and
 *   - run an out-of-band tuning pass (`tuning_run`) without waiting
 *     for the cron to fire.
 *
 * The "edit" flow does NOT auto-apply: it submits a
 * ``strategy_tuning_proposal`` patch and the operator still has to
 * promote it through the normal evolution review path.
 */
export function StrategyTuningCard({
  strategyId,
  tuning,
  busy,
  onSetBusy,
  onRefresh,
  onError,
  onNotice,
}: Props) {
  const enabled = !!tuning?.tuning?.enabled;
  const schedule = tuning?.schedule ?? null;

  const initialPrompt = tuning?.tuning?.tuning_prompt ?? "";
  const initialCron = tuning?.tuning?.schedule?.cron ?? "0 */6 * * *";
  const initialObjectives = (tuning?.tuning?.objectives ?? []).join(", ");

  const [prompt, setPrompt] = useState(initialPrompt);
  const [cron, setCron] = useState(initialCron);
  const [objectives, setObjectives] = useState(initialObjectives);
  const [requireBacktest, setRequireBacktest] = useState(
    tuning?.tuning?.guardrails?.require_backtest ?? true,
  );
  const [requireShadow, setRequireShadow] = useState(
    tuning?.tuning?.guardrails?.require_shadow_run ?? false,
  );
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    setPrompt(initialPrompt);
    setCron(initialCron);
    setObjectives(initialObjectives);
    setRequireBacktest(tuning?.tuning?.guardrails?.require_backtest ?? true);
    setRequireShadow(tuning?.tuning?.guardrails?.require_shadow_run ?? false);
    setEditing(false);
  }, [
    initialPrompt,
    initialCron,
    initialObjectives,
    tuning?.tuning?.guardrails?.require_backtest,
    tuning?.tuning?.guardrails?.require_shadow_run,
  ]);

  const summary = useMemo(() => {
    const snap = tuning?.snapshot;
    if (!snap) return null;
    const runs = (snap.run_metrics ?? {}) as Record<string, unknown>;
    const trades = (snap.trade_metrics ?? {}) as Record<string, unknown>;
    return {
      considered: snap.runs_considered,
      hit_rate: runs["hit_rate"],
      total_trades: trades["total"],
      win_rate: trades["win_rate"],
    };
  }, [tuning]);

  async function generateProposal() {
    onSetBusy("tuning:generate");
    onError(null);
    try {
      const out = await clientApi.strategyRuntimeTuningGenerate({
        strategy_id: strategyId,
        prompt,
        cron,
        objectives: objectives
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        require_backtest: requireBacktest,
        require_shadow_run: requireShadow,
      });
      if (out.proposal_id) {
        onNotice(`Tuning proposal ${out.proposal_id} created — promote in evolution.`);
      } else {
        onNotice("Tuning proposal created.");
      }
      setEditing(false);
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      onSetBusy(null);
    }
  }

  async function installSchedule() {
    onSetBusy("tuning:schedule");
    onError(null);
    try {
      await clientApi.strategyRuntimeTuningSchedule(strategyId);
      onNotice("Tuning schedule installed.");
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      onSetBusy(null);
    }
  }

  async function pause() {
    onSetBusy("tuning:pause");
    try {
      await clientApi.strategyRuntimeTuningPause(strategyId);
      onNotice("Tuning paused.");
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      onSetBusy(null);
    }
  }

  async function resume() {
    onSetBusy("tuning:resume");
    try {
      await clientApi.strategyRuntimeTuningResume(strategyId);
      onNotice("Tuning resumed.");
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      onSetBusy(null);
    }
  }

  async function runNow(dry: boolean) {
    onSetBusy(dry ? "tuning:dry_run" : "tuning:run");
    onError(null);
    try {
      const out = await clientApi.strategyRuntimeTuningRun({
        strategy_id: strategyId,
        dry_run: dry,
        operator: "dashboard",
        note: dry ? "manual_dry_tuning" : "manual_tuning",
      });
      onNotice(
        out.proposal_id
          ? `Tuning ran (${out.status}) — proposal ${out.proposal_id}`
          : `Tuning ran (${out.status})${out.reason ? ` · ${out.reason}` : ""}`,
      );
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      onSetBusy(null);
    }
  }

  return (
    <Card
      title="Self-evolution / tuning"
      description="Strategy tuner prompt, schedule, and most-recent performance snapshot."
      actions={
        <div className="flex items-center gap-2">
          {enabled ? (
            schedule?.enabled ? (
              <button
                onClick={() => void pause()}
                disabled={busy !== null}
                className="text-xs rounded px-2 py-1 border border-[#f5a524]/40 text-[#f5a524] hover:bg-[#f5a524]/10"
              >
                Pause
              </button>
            ) : (
              <button
                onClick={() => void resume()}
                disabled={busy !== null}
                className="text-xs rounded px-2 py-1 border border-accent-500/40 text-accent-300 hover:bg-accent-500/10"
              >
                Resume
              </button>
            )
          ) : null}
          {enabled && !schedule && (
            <button
              onClick={() => void installSchedule()}
              disabled={busy !== null}
              className="btn-ghost text-xs"
            >
              {busy === "tuning:schedule" ? "Installing…" : "Install schedule"}
            </button>
          )}
          <button
            onClick={() => void runNow(true)}
            disabled={busy !== null}
            className="btn-ghost text-xs"
          >
            {busy === "tuning:dry_run" ? "Running…" : "Dry run"}
          </button>
          <button
            onClick={() => void runNow(false)}
            disabled={busy !== null}
            className="bg-brand-500/80 hover:bg-brand-500 text-white text-xs rounded px-3 py-1.5"
          >
            {busy === "tuning:run" ? "Running…" : "Run tuner"}
          </button>
        </div>
      }
    >
      {!tuning?.ok && !enabled ? (
        <Empty label="Tuning not yet enabled for this strategy. Use 'Edit & propose' below to author a tuning block." />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
          <Stat label="Status" tone={enabled ? "ok" : "warn"}>
            {enabled ? "enabled" : "disabled"}
          </Stat>
          <Stat label="Schedule">
            {schedule
              ? schedule.cron
                ? `cron · ${schedule.cron}`
                : schedule.every_seconds
                  ? `every ${schedule.every_seconds}s`
                  : "installed"
              : "not installed"}
          </Stat>
          <Stat label="Snapshot">
            {summary
              ? `${summary.considered ?? 0} runs · trades=${summary.total_trades ?? 0}`
              : "no data"}
          </Stat>
        </div>
      )}

      {(tuning?.tuning?.objectives ?? []).length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1">
          {tuning?.tuning?.objectives?.map((o) => (
            <Pill key={o}>{o}</Pill>
          ))}
        </div>
      )}

      <div className="mt-4">
        <button
          onClick={() => setEditing((v) => !v)}
          className="btn-ghost text-xs"
        >
          {editing ? "Hide editor" : "Edit & propose"}
        </button>
      </div>

      {editing && (
        <div className="mt-3 space-y-2 text-xs">
          <Field label="tuner prompt" full>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              className="input-dark font-mono h-40"
              placeholder="Describe how the tuner should reason about this strategy."
            />
          </Field>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            <Field label="cron">
              <input
                value={cron}
                onChange={(e) => setCron(e.target.value)}
                className="input-dark font-mono"
              />
            </Field>
            <Field label="objectives (comma-separated)">
              <input
                value={objectives}
                onChange={(e) => setObjectives(e.target.value)}
                className="input-dark font-mono"
              />
            </Field>
          </div>
          <div className="flex gap-4 items-center">
            <label className="inline-flex items-center gap-2 text-[11px] text-ink-300">
              <input
                type="checkbox"
                checked={requireBacktest}
                onChange={(e) => setRequireBacktest(e.target.checked)}
              />
              require backtest
            </label>
            <label className="inline-flex items-center gap-2 text-[11px] text-ink-300">
              <input
                type="checkbox"
                checked={requireShadow}
                onChange={(e) => setRequireShadow(e.target.checked)}
              />
              require shadow run
            </label>
          </div>
          <div className="flex justify-end">
            <button
              onClick={() => void generateProposal()}
              disabled={busy !== null}
              className="bg-brand-500/80 hover:bg-brand-500 disabled:opacity-50 text-white text-xs rounded px-3 py-1.5"
            >
              {busy === "tuning:generate" ? "Submitting…" : "Submit proposal"}
            </button>
          </div>
        </div>
      )}
    </Card>
  );
}

function Stat({
  label,
  children,
  tone = "neutral",
}: {
  label: string;
  children: React.ReactNode;
  tone?: "neutral" | "ok" | "warn" | "danger";
}) {
  const toneClass = {
    neutral: "text-ink-100",
    ok: "text-accent-300",
    warn: "text-[#f5a524]",
    danger: "text-[#ef4560]",
  }[tone];
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-900/40 p-3">
      <div className="text-[10px] uppercase tracking-wider text-ink-500">
        {label}
      </div>
      <div className={`mt-1 ${toneClass}`}>{children}</div>
    </div>
  );
}

function Field({
  label,
  children,
  full = false,
}: {
  label: string;
  children: React.ReactNode;
  full?: boolean;
}) {
  return (
    <label className={`block ${full ? "md:col-span-2" : ""}`}>
      <span className="text-[11px] text-ink-400">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  );
}
