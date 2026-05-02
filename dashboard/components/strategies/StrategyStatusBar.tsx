"use client";

import { useTranslations } from "next-intl";

import { Card, Kpi, Pill } from "../Page";
import { clientApi } from "../../lib/clientApi";
import type { StrategyWorkspaceEnvelope } from "../../lib/strategyTypes";

interface Props {
  envelope: StrategyWorkspaceEnvelope;
  busy: string | null;
  disabled: boolean;
  onSetBusy: (key: string | null) => void;
  onRefresh: () => Promise<void> | void;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
}

/**
 * Top-of-workspace KPI band. Shows manifest mode, last run summary,
 * kill-switch state, and exposes the kill-switch toggle + run-now
 * action. The buttons here intentionally avoid the destructive
 * lifecycle calls (promote/kill); they're presented inline so an
 * operator can react to a misbehaving strategy without leaving the
 * workspace.
 */
export function StrategyStatusBar({
  envelope,
  busy,
  disabled,
  onSetBusy,
  onRefresh,
  onError,
  onNotice,
}: Props) {
  const t = useTranslations("strategyStatus");
  const manifest = envelope.manifest;
  const last = envelope.last_run;
  const kill = envelope.kill_switch;

  async function setKillSwitch(action: "set" | "clear") {
    const sid = envelope.strategy_id;
    if (!sid) return;
    let reason = "";
    if (action === "set") {
      reason = window.prompt(t("killReasonPrompt", { id: sid }), "operator_halt") ?? "";
      if (!reason) return;
    }
    onSetBusy(`kill:${action}`);
    onError(null);
    try {
      await clientApi.strategyRuntimeKillSwitch({
        strategy_id: sid,
        action,
        reason,
        by: "dashboard",
      });
      onNotice(action === "set" ? t("killArmed", { reason }) : t("killCleared"));
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      onSetBusy(null);
    }
  }

  async function runNow() {
    const sid = envelope.strategy_id;
    if (!sid) return;
    onSetBusy("run_tick");
    onError(null);
    try {
      const out = await clientApi.strategyRuntimeRunTick({
        strategy_id: sid,
        operator: "dashboard",
        note: "manual_run_tick",
      });
      onNotice(
        t("tickResult", {
          runId: out.run_id ?? "",
          status: out.status ?? "?",
          ms: out.duration_ms ?? 0,
        }),
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
      title={manifest?.title || envelope.strategy_id}
      description={
        manifest
          ? `${manifest.strategy_id} · v${manifest.version} · ${manifest.markets.join(", ") || t("noMarkets")}`
          : t("manifestUnavailable")
      }
      actions={
        <div className="flex items-center gap-2">
          <button
            onClick={() => void runNow()}
            disabled={disabled || busy !== null}
            className="bg-brand-500/80 hover:bg-brand-500 disabled:opacity-50 text-white text-xs rounded px-3 py-1.5"
          >
            {busy === "run_tick" ? t("running") : t("runTick")}
          </button>
          {kill?.asserted ? (
            <button
              onClick={() => void setKillSwitch("clear")}
              disabled={disabled || busy !== null}
              className="text-xs rounded px-3 py-1.5 border border-accent-500/40 text-accent-300 hover:border-accent-500/70"
            >
              {busy === "kill:clear" ? t("clearing") : t("clearKill")}
            </button>
          ) : (
            <button
              onClick={() => void setKillSwitch("set")}
              disabled={disabled || busy !== null}
              className="text-xs rounded px-3 py-1.5 border border-[#ef4560]/50 text-[#ef4560] hover:bg-[#ef4560]/10"
            >
              {busy === "kill:set" ? t("arming") : t("killSwitch")}
            </button>
          )}
        </div>
      }
    >
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Kpi
          label={t("mode")}
          value={manifest?.mode ?? "—"}
          tone={
            manifest?.mode === "live"
              ? "danger"
              : manifest?.mode === "shadow"
                ? "warn"
                : "brand"
          }
        />
        <Kpi
          label={t("package")}
          value={
            envelope.package_hash
              ? envelope.package_hash.slice(0, 12)
              : "—"
          }
        />
        <Kpi
          label={t("lastRun")}
          value={
            last
              ? `${last.status} · ${new Date(last.finished_at).toLocaleTimeString()}`
              : t("noRuns")
          }
          tone={
            last?.status === "error"
              ? "danger"
              : last?.status === "hold"
                ? "warn"
                : last
                  ? "ok"
                  : "neutral"
          }
        />
        <Kpi
          label={t("killSwitch")}
          value={kill?.asserted ? t("armed") : t("clear")}
          tone={kill?.asserted ? "danger" : "ok"}
        />
      </div>
      {kill?.asserted && (
        <div className="mt-3 rounded-lg border border-[#ef4560]/30 bg-[#ef4560]/10 px-3 py-2 text-xs text-[#ef4560]">
          {t("armedBy")} <span className="font-mono">{kill.by || "?"}</span> {t("at")}{" "}
          {kill.at ? new Date(kill.at).toLocaleString() : "?"} —{" "}
          {kill.reason || t("noReason")}
        </div>
      )}
      {manifest && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {manifest.markets.map((m) => (
            <Pill key={m} tone="brand">
              {m}
            </Pill>
          ))}
          {manifest.subagents.map((a) => (
            <Pill key={a}>{a}</Pill>
          ))}
        </div>
      )}
    </Card>
  );
}
