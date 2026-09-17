"use client";

/**
 * Strategy detail page (`/strategies/[id]`).
 *
 * One operator-grade workspace for a single strategy package. Pulls
 * the data the legacy split-view did (manifest, prompts, limits,
 * trade history, runs, schedules, tuning, pending proposals) and
 * renders it as one scrollable surface where every editable field is
 * inline — no modal hops, no JSON-only fallbacks.
 *
 * Why this lives here instead of inside ``app/strategies/page.tsx``:
 *
 * - the list page is now a thin overview / launcher;
 * - this page reads from ``/strategies/runtime/workspace`` (the
 *   aggregate envelope) so the heavy joins happen on the backend;
 * - editable forms talk to the legacy ``/strategy/...`` REST surface
 *   for prompt / limit / config / file edits, and to the runtime
 *   ``/strategies/runtime/...`` endpoints for schedule / tuning /
 *   tick / kill switch operations.
 *
 * Sections rendered (top → bottom):
 *
 *   1. Header   · status, mode, package hash, kill-switch, run-tick.
 *   2. KPIs     · live PnL, # runs, last run timing, schedule cadence.
 *   3. Schedules + Reflection (trading cron, tuning cron + prompt).
 *   4. Subagent prompts editor (per-strategy ``subagents/*.agent.md``).
 *   5. Strategy file editor (``main.py``, ``strategy.md``, etc).
 *   6. Runs / Trades / Audit (existing ``StrategyWorkspace`` cards).
 *   7. Manifest + Limits + Config raw editors (fallback).
 */

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import {
  Advanced,
  Card,
  Empty,
  ErrorBanner,
  Kpi,
  PageBody,
  PageHeader,
  Pill,
} from "../../../components/Page";
import { EditIcon, PauseIcon, TrashIcon } from "../../../components/icons";
import { ModePill } from "../../../components/ModePill";
import { StrategyWorkflowPanel } from "../../../components/workflows/StrategyWorkflowPanel";
import { useWorkflowText } from "../../../components/workflows/WorkflowCanvas";
import {
  clientApi,
  type StrategyDetail,
} from "../../../lib/clientApi";
import {
  confirm as confirmDialog,
  toast,
} from "../../../lib/dialogs";
import {
  strategyStatusLabel,
  useCadenceHint,
  useStrategyLifecycle,
} from "../../../lib/useStrategyLifecycle";
import type {
  StrategyRunRecord,
  StrategyTuningStatusEnvelope,
  StrategyWorkspaceEnvelope,
} from "../../../lib/strategyTypes";
import { KeyValueEditor } from "../../../components/KeyValueEditor";
import { StrategyBindCard } from "../../../components/strategies/StrategyBindCard";
import { StrategyHistoryCard } from "../../../components/strategies/StrategyHistoryCard";
import { StrategyPerformanceCard } from "../../../components/strategies/StrategyPerformanceCard";
import { StrategyEvolutionCard } from "../../../components/strategies/StrategyEvolutionCard";
import { StrategyAgentSessionsCard } from "../../../components/strategies/StrategyAgentSessionsCard";
import { StrategyPromotionCard } from "../../../components/strategies/StrategyPromotionCard";
import { StrategyRiskDecisionsCard } from "../../../components/strategies/StrategyRiskDecisionsCard";
import { StrategyRunsCard } from "../../../components/strategies/StrategyRunsCard";
import { StrategyScheduleCard } from "../../../components/strategies/StrategyScheduleCard";
import { StrategyStatusBar } from "../../../components/strategies/StrategyStatusBar";
import { StrategyTuningCard } from "../../../components/strategies/StrategyTuningCard";

interface PackageFile {
  rel_path: string;
  size: number;
  kind: "python" | "yaml" | "markdown" | "json" | "text";
  content: string | null;
  error?: "decode_failed" | "too_large";
}

interface FilesEnvelope {
  strategy_id: string;
  root: string;
  files: PackageFile[];
}

type StrategyDetailTab =
  | "workflow"
  | "overview"
  | "performance"
  | "agent_sessions"
  | "automation"
  | "files"
  | "history"
  | "debug";

const STRATEGY_DETAIL_TABS: StrategyDetailTab[] = [
  "workflow",
  "overview",
  "performance",
  "agent_sessions",
  "automation",
  "files",
  "history",
  "debug",
];

export default function StrategyDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const t = useTranslations("strategyDetail");
  const tStrategies = useTranslations("strategies");
  const tCommon = useTranslations("common");
  const strategyId = decodeURIComponent(params.id);
  const [detail, setDetail] = useState<StrategyDetail | null>(null);
  const [workspace, setWorkspace] =
    useState<StrategyWorkspaceEnvelope | null>(null);
  const [tuning, setTuning] = useState<StrategyTuningStatusEnvelope | null>(
    null,
  );
  const [files, setFiles] = useState<FilesEnvelope | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] =
    useState<StrategyDetailTab>("workflow");
  const [workflowDirty, setWorkflowDirty] = useState(false);
  // Dirty tracking for the Files & Prompts editors — leaving the tab
  // (or the page) with unsaved edits asks for confirmation.
  const [promptsDirty, setPromptsDirty] = useState(false);
  const [packageFilesDirty, setPackageFilesDirty] = useState(false);
  const filesDirty = promptsDirty || packageFilesDirty;
  // Cards (status bar / schedules / tuning) share the busy channel the
  // header used to own; the lifecycle hook carries its own busy state.
  const [cardBusy, setCardBusy] = useState<string | null>(null);
  const router = useRouter();
  const lifecycle = useStrategyLifecycle({ onRefresh: refresh });
  const busy = lifecycle.busy ?? cardBusy;
  const busyOn = useCallback(
    (action: string) =>
      typeof lifecycle.busy === "string" &&
      (lifecycle.busy === action || lifecycle.busy.startsWith(`${action}:`)),
    [lifecycle.busy],
  );
  // Success feedback is toast-based (the old violet notice bar kept the
  // confirmation far away from the action that triggered it).
  const notifyOk = useCallback((msg: string | null) => {
    if (msg) toast({ message: msg, tone: "ok" });
  }, []);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [d, w, t, f] = await Promise.all([
        clientApi.strategyGet(strategyId).catch((err) => {
          throw err instanceof Error ? err : new Error(String(err));
        }),
        clientApi.strategyRuntimeWorkspace(strategyId, 50).catch(
          (err: unknown) =>
            ({
              ok: false,
              strategy_id: strategyId,
              error: err instanceof Error ? err.message : String(err),
            }) as StrategyWorkspaceEnvelope,
        ),
        clientApi.strategyRuntimeTuningStatus(strategyId).catch(
          (err: unknown) =>
            ({
              ok: false,
              strategy_id: strategyId,
              error: err instanceof Error ? err.message : String(err),
            }) as StrategyTuningStatusEnvelope,
        ),
        clientApi.strategyFilesList(strategyId).catch(
          () =>
            ({
              strategy_id: strategyId,
              root: "",
              files: [],
            }) as FilesEnvelope,
        ),
      ]);
      setDetail(d);
      setWorkspace(w);
      setTuning(t);
      setFiles(f);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId]);

  // Leaving the Files & Prompts tab with unsaved edits confirms first —
  // same contract as the browser-level beforeunload guard in the editors.
  async function handleTabChange(next: StrategyDetailTab) {
    if ((activeTab === "files" && next !== "files" && filesDirty) || (activeTab === "workflow" && next !== "workflow" && workflowDirty)) {
      const ok = await confirmDialog({
        title: tStrategies("unsavedLeaveTitle"),
        message: tStrategies("unsavedLeaveMessage"),
        okLabel: tStrategies("unsavedLeaveConfirm"),
        tone: "warning",
      });
      if (!ok) return;
    }
    if (next !== "workflow") setWorkflowDirty(false);
    setActiveTab(next);
  }

  const pnl = useMemo(() => summarisePnl(workspace), [workspace]);
  const runs = workspace?.runs?.runs ?? [];
  const lastRun: StrategyRunRecord | null = workspace?.last_run ?? null;
  const subagents = useMemo(() => detail?.strategy.subagents ?? [], [detail]);
  const subagentFiles = useMemo(
    () =>
      (files?.files ?? []).filter(
        (f) =>
          f.rel_path.startsWith("subagents/") && f.rel_path.endsWith(".md"),
      ),
    [files],
  );
  const editableFiles = useMemo(
    () =>
      (files?.files ?? []).filter(
        (f) =>
          f.kind === "python" ||
          f.kind === "yaml" ||
          f.kind === "markdown" ||
          f.kind === "text",
      ),
    [files],
  );

  return (
    <div>
      <PageHeader
        title={detail?.strategy.title || strategyId}
        eyebrow={t("eyebrow")}
        description={
          detail?.strategy.path
            ? detail.strategy.path
            : t("loadingPackage")
        }
        actions={
          <div className="flex items-center gap-2">
            <Link
              href="/strategies"
              className="btn-ghost text-xs"
            >
              {t("allStrategies")}
            </Link>
            {/* Backtests is the primary next step after editing
                (write → backtest → promotion evidence), so it gets the
                primary button instead of sitting next to Delete. */}
            <Link
              href={`/strategies/${encodeURIComponent(strategyId)}/backtests`}
              className="btn btn-primary text-xs"
            >
              {t("backtestsLink")}
            </Link>
            <button
              onClick={() => void refresh()}
              disabled={loading || busy !== null}
              className="btn-ghost text-xs"
            >
              {loading ? tCommon("refreshing") : tCommon("refresh")}
            </button>
            <button
              onClick={() => {
                if (detail) void lifecycle.rename(detail.strategy);
              }}
              disabled={!detail || busy !== null}
              className="btn-ghost text-xs"
            >
              <EditIcon size={13} />
              {busyOn("rename") ? tStrategies("renaming") : tStrategies("editName")}
            </button>
          </div>
        }
      />
      {/* Page-level Strategies|Workflows tabs intentionally omitted on the
          detail page — "← All strategies" already anchors the hierarchy and
          a second tab bar above the workspace tabs doubled the navigation. */}
      <PageBody>
        {error && <ErrorBanner error={error} />}

        {!detail ? (
          <Card title={t("strategyTitle", { id: strategyId })}>
            <Empty
              label={
                loading
                  ? t("loadingStrategy", { id: strategyId })
                  : t("strategyNotFound")
              }
            />
          </Card>
        ) : (
          <>
            <StrategyDetailTabBar
              active={activeTab}
              onChange={handleTabChange}
              counts={{
                runs: workspace?.runs?.count ?? (lastRun ? 1 : 0),
                ledgers: Object.keys(workspace?.history?.ledgers ?? {}).length,
              }}
            />

            {activeTab === "workflow" ? <StrategyWorkflowPanel strategyId={strategyId} onDirtyChange={setWorkflowDirty} /> : null}

            {activeTab === "overview" ? (
              <div className="space-y-4">
                <KpiRow
                  detail={detail}
                  workspace={workspace}
                  pnl={pnl}
                  lastRun={lastRun}
                />

                <StrategyDefinitionCard detail={detail} />

                {workspace && !workspace.ok ? (
                  // The runtime workspace envelope failed — surface it
                  // instead of silently rendering an empty overview.
                  <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-warn/30 bg-warn/10 px-4 py-2.5 text-[13px] text-warn">
                    <span className="min-w-0 break-words">
                      {t("workspaceLoadFailed")}
                      {workspace.error ? `: ${workspace.error}` : ""}
                    </span>
                    <button
                      onClick={() => void refresh()}
                      disabled={loading}
                      className="btn btn-ghost shrink-0 text-xs"
                    >
                      {loading ? tCommon("refreshing") : tCommon("refresh")}
                    </button>
                  </div>
                ) : null}

                {workspace?.ok && (
                  <StrategyStatusBar
                    envelope={workspace}
                    busy={busy}
                    disabled={loading}
                    onSetBusy={setCardBusy}
                    onRefresh={refresh}
                    onError={setError}
                    onNotice={notifyOk}
                  />
                )}

                <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                  <StrategyPromotionCard
                    strategyId={strategyId}
                    status={
                      detail?.strategy?.status ??
                      (workspace?.manifest as Record<string, unknown> | undefined)?.[
                        "status"
                      ] as string | undefined
                    }
                    onError={setError}
                    onNotice={notifyOk}
                    onRefresh={refresh}
                  />

                  <StrategyBindCard
                    strategyId={strategyId}
                    currentAccountId={detail?.strategy?.account_id ?? null}
                    currentWalletId={detail?.strategy?.wallet_id ?? null}
                    onError={setError}
                    onNotice={notifyOk}
                    onRefresh={refresh}
                  />
                </div>

                <StrategyRiskDecisionsCard strategyId={strategyId} />
              </div>
            ) : null}

            {activeTab === "performance" ? (
              <StrategyPerformanceCard
                strategyId={strategyId}
                mode={detail.strategy.mode}
              />
            ) : null}

            {activeTab === "agent_sessions" ? (
              <StrategyAgentSessionsCard
                strategyId={strategyId}
                workspace={workspace}
              />
            ) : null}

            {activeTab === "automation" ? (
              <div className="space-y-4">
                <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                  <StrategyScheduleCard
                    strategyId={strategyId}
                    status={workspace?.schedules ?? null}
                    busy={busy}
                    onSetBusy={setCardBusy}
                    onRefresh={refresh}
                    onError={setError}
                    onNotice={notifyOk}
                  />
                  <StrategyTuningCard
                    strategyId={strategyId}
                    tuning={tuning}
                    busy={busy}
                    onSetBusy={setCardBusy}
                    onRefresh={refresh}
                    onError={setError}
                    onNotice={notifyOk}
                  />
                </div>

                <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                  <StrategyRunsCard
                    runs={runs}
                    total={workspace?.runs?.count ?? (lastRun ? 1 : 0)}
                  />
                  <StrategyEvolutionCard
                    strategyId={strategyId}
                    proposals={tuning?.pending_proposals ?? []}
                    dropped={[]}
                    onRefresh={refresh}
                  />
                </div>
              </div>
            ) : null}

            {activeTab === "files" ? (
              <div className="space-y-4">
                <SubagentPromptsCard
                  strategyId={strategyId}
                  listed={subagents}
                  detail={detail}
                  files={subagentFiles}
                  onAfterSave={async () => {
                    await refresh();
                  }}
                  onError={setError}
                  onNotice={notifyOk}
                  onDirtyChange={setPromptsDirty}
                />

                <StrategyFilesCard
                  strategyId={strategyId}
                  files={editableFiles}
                  onAfterSave={async () => {
                    await refresh();
                  }}
                  onError={setError}
                  onNotice={notifyOk}
                  onDirtyChange={setPackageFilesDirty}
                />
              </div>
            ) : null}

            {activeTab === "history" ? (
              <StrategyHistoryCard
                strategyId={strategyId}
                history={workspace?.history ?? null}
              />
            ) : null}

            {activeTab === "debug" ? (
              <ManifestEditorCard
                detail={detail}
                onAfterSave={async () => {
                  await refresh();
                }}
                onError={setError}
                onNotice={notifyOk}
              />
            ) : null}

            {/* Destructive actions live in a secondary fold instead of
                sharing the header row with navigation — one more click,
                but no more "delete next to refresh". */}
            <Advanced title={t("dangerZone")} description={t("dangerZoneDesc")}>
              <div className="flex flex-wrap items-center gap-2">
                {detail &&
                !["paused", "archived", "draft", "static_review", "backtested"].includes(
                  detail.strategy.status,
                ) ? (
                  <button
                    onClick={() => void lifecycle.pause(detail.strategy)}
                    disabled={busy !== null}
                    className="btn btn-ghost text-xs text-warn"
                  >
                    <PauseIcon size={13} />
                    {busyOn("pause") ? tStrategies("pausing") : tStrategies("pauseStrategy")}
                  </button>
                ) : null}
                <button
                  onClick={() => void lifecycle.remove(detail.strategy)}
                  disabled={busy !== null}
                  className="btn btn-ghost text-xs text-danger"
                >
                  <TrashIcon size={13} />
                  {busyOn("close")
                    ? tStrategies("closingPositions")
                    : busyOn("delete")
                      ? tStrategies("deleting")
                      : tCommon("delete")}
                </button>
              </div>
            </Advanced>
          </>
        )}
      </PageBody>
    </div>
  );
}

function StrategyDetailTabBar({
  active,
  onChange,
  counts,
}: {
  active: StrategyDetailTab;
  onChange: (tab: StrategyDetailTab) => void;
  counts: { runs: number; ledgers: number };
}) {
  const t = useTranslations("strategyDetail");
  const text = useWorkflowText();
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-950/25 p-2">
      <div className="flex gap-1 overflow-x-auto pb-1">
        {STRATEGY_DETAIL_TABS.map((tab) => {
          const selected = active === tab;
          const badge =
            tab === "automation"
              ? counts.runs
              : tab === "history"
                ? counts.ledgers
                : null;
          return (
            <button
              key={tab}
              type="button"
              onClick={() => onChange(tab)}
              className={[
                "group shrink-0 rounded-md border px-3 py-1.5 text-left transition-colors",
                selected
                  ? "border-brand-500/45 bg-brand-500/20 text-white"
                  : "border-transparent text-ink-300 hover:border-brand-500/15 hover:bg-brand-500/10",
              ].join(" ")}
              title={tab === "workflow" ? text("编辑策略卡片、资源关系与复盘流程", "Edit strategy resources, connections and review workflows") : t(`tabs.${tab}.description`)}
            >
              <div className="flex items-center gap-2">
                <span className="text-[12px] font-semibold">{tab === "workflow" ? text("工作流", "Workflow") : t(`tabs.${tab}.label`)}</span>
                {badge !== null ? (
                  <span className="rounded-full border border-brand-500/20 px-1.5 py-0.5 text-[10px] text-ink-300">
                    {badge}
                  </span>
                ) : null}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function StrategyDefinitionCard({ detail }: { detail: StrategyDetail }) {
  const t = useTranslations("strategyDetail");
  const description =
    typeof detail.strategy_yml.description === "string"
      ? detail.strategy_yml.description
      : "";
  return (
    <Card
      title={t("definitionTitle")}
      description={description || t("definitionDescription")}
    >
      <div className="grid gap-3 text-sm md:grid-cols-3">
        <DefinitionBlock label={t("definitionMarkets")} values={detail.strategy.markets} />
        <DefinitionBlock label={t("definitionTriggers")} values={detail.strategy.trigger_kinds} />
        <DefinitionBlock label={t("definitionSubagents")} values={detail.strategy.subagents} />
        {/* status/mode/account intentionally omitted here — status+mode
            already lead the KPI row above and the account binding lives
            in the Bindings card, so repeating them as badges only added
            noise. */}
      </div>
    </Card>
  );
}

function DefinitionBlock({
  label,
  values,
}: {
  label: string;
  values: string[];
}) {
  const t = useTranslations("strategyDetail");
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="text-[11px] text-ink-500 font-medium">
        {label}
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {values.length ? (
          values.map((value) => (
            <Pill key={value} tone="neutral">
              {value}
            </Pill>
          ))
        ) : (
          <span className="text-[12px] text-ink-500">{t("notConfigured")}</span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// KPI strip
// ---------------------------------------------------------------------------

interface PnlSummary {
  realised_usd: number;
  fills: number;
  hits: number;
  misses: number;
  errors: number;
}

function summarisePnl(
  workspace: StrategyWorkspaceEnvelope | null,
): PnlSummary {
  if (!workspace || !workspace.runs) {
    return { realised_usd: 0, fills: 0, hits: 0, misses: 0, errors: 0 };
  }
  let realised = 0;
  let fills = 0;
  let hits = 0;
  let misses = 0;
  let errors = 0;
  for (const r of workspace.runs.runs ?? []) {
    if (r.status === "submitted") fills += 1;
    if (r.status === "ok") hits += 1;
    if (r.status === "hold") misses += 1;
    if (r.status === "error") errors += 1;
    const out = (r.outputs ?? {}) as Record<string, unknown>;
    const pnl = out.pnl_usd ?? out.realized_usd ?? out.realised_usd;
    if (typeof pnl === "number" && Number.isFinite(pnl)) realised += pnl;
  }
  return { realised_usd: realised, fills, hits, misses, errors };
}

function KpiRow({
  detail,
  workspace,
  pnl,
  lastRun,
}: {
  detail: StrategyDetail;
  workspace: StrategyWorkspaceEnvelope | null;
  pnl: PnlSummary;
  lastRun: StrategyRunRecord | null;
}) {
  const t = useTranslations("strategyDetail");
  const tStrategies = useTranslations("strategies");
  const cadenceHint = useCadenceHint();
  const trading = workspace?.schedules?.trading;
  const tuning = workspace?.schedules?.tuning;
  const tradingHint = cadenceHint(trading?.cron, trading?.every_seconds);
  const tuningHint = cadenceHint(tuning?.cron, tuning?.every_seconds);
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <Kpi
        label={t("kpiStatus")}
        value={strategyStatusLabel(tStrategies, detail.strategy.status)}
        tone={
          detail.strategy.status === "live"
            ? "ok"
            : detail.strategy.status === "paper"
              ? "brand"
              : detail.strategy.status === "paused"
                ? "warn"
                : "neutral"
        }
        delta={
          <>
            {t("modeLabel")} <span className="text-ink-200">{detail.strategy.mode}</span>
          </>
        }
      />
      <Kpi
        label={t("kpiRealisedPnl")}
        value={
          <span className="inline-flex flex-wrap items-center gap-2">
            {pnl.realised_usd === 0
              ? "$0.00"
              : `${pnl.realised_usd >= 0 ? "+" : ""}$${pnl.realised_usd.toFixed(2)}`}
            {/* PnL provenance (compliance): label the execution mode
                this number was realized under. */}
            <ModePill mode={detail.strategy.mode} />
          </span>
        }
        tone={
          pnl.realised_usd > 0
            ? "ok"
            : pnl.realised_usd < 0
              ? "danger"
              : "neutral"
        }
        delta={t("kpiFillsErr", { fills: pnl.fills, errors: pnl.errors })}
      />
      <Kpi
        label={t("kpiTradingCron")}
        value={
          trading
            ? tradingHint ??
              trading.cron ??
              (trading.every_seconds
                ? t("everySeconds", { seconds: trading.every_seconds })
                : t("installed"))
            : t("notInstalled")
        }
        tone={trading?.enabled ? "ok" : "warn"}
        delta={
          trading
            ? `${trading.cron && tradingHint ? `cron: ${trading.cron} · ` : ""}${trading.target ?? "–"}`
            : "–"
        }
      />
      <Kpi
        label={t("kpiReflectionCron")}
        value={
          tuning
            ? tuningHint ??
              tuning.cron ??
              (tuning.every_seconds
                ? t("everySeconds", { seconds: tuning.every_seconds })
                : t("installed"))
            : t("notInstalled")
        }
        tone={tuning?.enabled ? "ok" : "warn"}
        delta={
          lastRun
            ? t("lastRunAt", {
                time: new Date(lastRun.finished_at || lastRun.started_at).toLocaleString(),
              })
            : t("noRunsYet")
        }
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subagent prompt editor
// ---------------------------------------------------------------------------

function SubagentPromptsCard({
  strategyId,
  listed,
  detail,
  files,
  onAfterSave,
  onError,
  onNotice,
  onDirtyChange,
}: {
  strategyId: string;
  listed: string[];
  detail: StrategyDetail;
  files: PackageFile[];
  onAfterSave: () => Promise<void>;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const t = useTranslations("strategyDetail");
  const tCommon = useTranslations("common");
  const initial = useMemo(() => {
    const out: Record<string, string> = {};
    for (const f of files) {
      out[f.rel_path] = f.content ?? "";
    }
    // Surface listed subagents that don't yet have a file.
    for (const name of listed) {
      const rel = `subagents/${name}.agent.md`;
      if (!(rel in out)) out[rel] = "";
    }
    // Surface any prompts/<name>.md that the legacy strategy editor
    // wrote — operators expect these to be edited side-by-side with
    // the per-strategy subagent prompts.
    for (const [name, body] of Object.entries(detail.prompts || {})) {
      const rel = `prompts/${name}.md`;
      if (!(rel in out)) out[rel] = body;
    }
    return out;
  }, [files, listed, detail.prompts]);
  const [edits, setEdits] = useState<Record<string, string>>(initial);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  useEffect(() => {
    // Merge server content into local edits, but never clobber a key the
    // operator has modified (unsaved draft). A background refresh — e.g.
    // after saving a different file — must not silently drop those edits;
    // keys that were just saved match the server content and refresh
    // normally.
    setEdits((prev) => {
      const next: Record<string, string> = {};
      for (const [k, serverRaw] of Object.entries(initial)) {
        const server = serverRaw ?? "";
        const local = prev[k];
        next[k] = local !== undefined && local !== server ? local : server;
      }
      return next;
    });
  }, [initial]);

  const dirty = useMemo(() => {
    const keys = new Set([...Object.keys(initial), ...Object.keys(edits)]);
    return Array.from(keys).filter((k) => (initial[k] ?? "") !== (edits[k] ?? ""));
  }, [initial, edits]);

  // Unsaved-edit guards: the page's tab switch confirm and a
  // browser-level beforeunload both key off this flag.
  useEffect(() => {
    onDirtyChange?.(dirty.length > 0);
  }, [dirty.length, onDirtyChange]);
  useEffect(() => {
    if (dirty.length === 0) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty.length]);

  async function save(rel: string) {
    setBusyKey(rel);
    onError(null);
    try {
      if (rel.startsWith("prompts/") && rel.endsWith(".md")) {
        const promptName = rel.slice("prompts/".length, -".md".length);
        await clientApi.strategyUpdate(strategyId, {
          prompts: { [promptName]: edits[rel] ?? "" },
          reason: `dashboard_prompt_${promptName}`,
        });
      } else {
        await clientApi.strategyFilesWrite(
          strategyId,
          rel,
          edits[rel] ?? "",
          `dashboard_subagent_${rel.replace(/[^a-z0-9]+/gi, "_")}`,
        );
      }
      onNotice(`${t("savedPrefix")} ${rel}`);
      await onAfterSave();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  const orderedKeys = Object.keys(edits).sort();

  return (
    <Card
      title={t("subagentLibraryTitle")}
      description={t("subagentLibraryDesc")}
    >
      {orderedKeys.length === 0 ? (
        <Empty label={t("noSubagentFiles")} />
      ) : (
        <div className="embedded-list-scroll-lg space-y-3">
          {orderedKeys.map((rel) => {
            const body = edits[rel] ?? "";
            const isDirty = dirty.includes(rel);
            return (
              <div
                key={rel}
                className="rounded-lg border border-brand-500/10 bg-ink-900/40 px-3 py-3"
              >
                <div className="flex items-center justify-between gap-2 mb-2">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-mono text-ink-200">
                      {rel}
                    </span>
                    {isDirty ? <Pill tone="warn">{t("unsaved")}</Pill> : null}
                  </div>
                  <button
                    onClick={() => void save(rel)}
                    disabled={busyKey !== null || !isDirty}
                    title={t("saveShortcutHint")}
                    className={`text-xs cursor-pointer ${isDirty ? "btn btn-primary" : "btn btn-ghost"}`}
                  >
                    {busyKey === rel ? tCommon("saving") : tCommon("save")}
                  </button>
                </div>
                <textarea
                  value={body}
                  onChange={(e) =>
                    setEdits((prev) => ({ ...prev, [rel]: e.target.value }))
                  }
                  onKeyDown={(e) => {
                    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
                      e.preventDefault();
                      if (isDirty && busyKey === null) void save(rel);
                    }
                  }}
                  className="input-dark font-mono w-full text-xs"
                  rows={Math.min(20, Math.max(8, body.split("\n").length + 2))}
                  placeholder={
                    rel.startsWith("subagents/")
                      ? t("subagentPlaceholder")
                      : t("promptPlaceholder")
                  }
                />
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Strategy file editor (main.py / strategy.md / tests / yamls)
// ---------------------------------------------------------------------------

function StrategyFilesCard({
  strategyId,
  files,
  onAfterSave,
  onError,
  onNotice,
  onDirtyChange,
}: {
  strategyId: string;
  files: PackageFile[];
  onAfterSave: () => Promise<void>;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const t = useTranslations("strategyDetail");
  const filtered = useMemo(
    () =>
      files.filter(
        (f) =>
          !f.rel_path.startsWith("subagents/") &&
          !f.rel_path.startsWith("prompts/"),
      ),
    [files],
  );
  const [activePath, setActivePath] = useState<string | null>(
    filtered[0]?.rel_path ?? null,
  );
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busyKey, setBusyKey] = useState<string | null>(null);

  useEffect(() => {
    if (!activePath && filtered.length) {
      setActivePath(filtered[0].rel_path);
    }
    if (activePath && !filtered.some((f) => f.rel_path === activePath)) {
      setActivePath(filtered[0]?.rel_path ?? null);
    }
  }, [filtered, activePath]);

  useEffect(() => {
    // Merge server content into local drafts, but never clobber a key the
    // operator has modified (unsaved draft). A background refresh — e.g.
    // after saving a different file — must not silently drop those edits;
    // keys that were just saved match the server content and refresh
    // normally.
    setDrafts((prev) => {
      const next: Record<string, string> = {};
      for (const f of filtered) {
        const server = f.content ?? "";
        const local = prev[f.rel_path];
        next[f.rel_path] = local !== undefined && local !== server ? local : server;
      }
      return next;
    });
  }, [filtered]);

  const active = useMemo(
    () => filtered.find((f) => f.rel_path === activePath) ?? null,
    [filtered, activePath],
  );
  const draftBody = activePath ? (drafts[activePath] ?? "") : "";
  const original = active?.content ?? "";
  const dirty = activePath ? draftBody !== original : false;

  // Unsaved-edit guards: the page's tab switch confirm and a
  // browser-level beforeunload both key off this flag.
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  async function save() {
    if (!activePath) return;
    setBusyKey(activePath);
    onError(null);
    try {
      await clientApi.strategyFilesWrite(
        strategyId,
        activePath,
        drafts[activePath] ?? "",
        `dashboard_edit_${activePath.replace(/[^a-z0-9]+/gi, "_")}`,
      );
      onNotice(`${t("savedPrefix")} ${activePath}`);
      await onAfterSave();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <Card
      title={t("packageFilesTitle")}
      description={t("packageFilesDesc")}
      actions={
        activePath ? (
          <button
            onClick={() => void save()}
            disabled={!dirty || busyKey !== null}
            title={t("saveShortcutHint")}
            className={`text-xs cursor-pointer ${dirty ? "btn btn-primary" : "btn btn-ghost"}`}
          >
            {busyKey === activePath ? t("savingFile") : t("saveFile")}
          </button>
        ) : null
      }
    >
      {filtered.length === 0 ? (
        <Empty label={t("noEditableFiles")} />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-[220px_1fr] gap-3">
          <ul className="embedded-list-scroll-lg space-y-1 text-xs">
            {filtered.map((f) => {
              const isActive = f.rel_path === activePath;
              const isDirty = (drafts[f.rel_path] ?? "") !== (f.content ?? "");
              return (
                <li key={f.rel_path}>
                  <button
                    onClick={() => setActivePath(f.rel_path)}
                    className={`w-full text-left rounded px-2 py-1.5 border transition-colors ${
                      isActive
                        ? "border-brand-500/50 bg-brand-500/10 text-white"
                        : "border-transparent text-ink-300 hover:bg-white/[0.03]"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono truncate">{f.rel_path}</span>
                      {isDirty ? <Pill tone="warn">●</Pill> : null}
                    </div>
                    <div className="text-[10px] text-ink-500 mt-0.5">
                      {f.kind} · {t("bytes", { size: f.size })}
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="min-w-0">
            {!active ? (
              <Empty label={t("pickFile")} />
            ) : active.error === "too_large" ? (
              <Empty
                label={t("tooLarge", { size: active.size })}
              />
            ) : active.error === "decode_failed" ? (
              <Empty label={t("unreadableFile")} />
            ) : (
              <textarea
                value={draftBody}
                onChange={(e) =>
                  setDrafts((prev) => ({
                    ...prev,
                    [active.rel_path]: e.target.value,
                  }))
                }
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
                    e.preventDefault();
                    if (dirty && busyKey === null) void save();
                  }
                }}
                className="input-dark font-mono w-full text-xs"
                rows={26}
                spellCheck={false}
              />
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Manifest / limits / config raw editors (always-present fallback)
// ---------------------------------------------------------------------------

// Common limit keys surfaced as quick-add suggestions inside the
// graphical limits editor. Operators can still add arbitrary keys
// with the "Add field" button, but the suggestions cover the >90%
// case observed across active strategies.
const LIMIT_SUGGESTIONS = [
  "max_position_usd",
  "max_daily_loss_usd",
  "max_open_positions",
  "max_orders_per_minute",
  "max_drawdown_pct",
  "max_leverage",
  "min_balance_usd",
  "kill_on_breach",
];

const CONFIG_SUGGESTIONS = [
  "execution_mode",
  "default_slippage_bps",
  "default_size_usd",
  "min_confidence",
  "cooldown_seconds",
  "rebalance_interval",
];

function ManifestEditorCard({
  detail,
  onAfterSave,
  onError,
  onNotice,
}: {
  detail: StrategyDetail;
  onAfterSave: () => Promise<void>;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
}) {
  const t = useTranslations("strategyDetail");
  const tCommon = useTranslations("common");
  const [config, setConfig] = useState<Record<string, unknown>>(
    () => (detail.config ?? {}) as Record<string, unknown>,
  );
  const [limits, setLimits] = useState<Record<string, unknown>>(
    () => (detail.limits ?? {}) as Record<string, unknown>,
  );
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    setConfig((detail.config ?? {}) as Record<string, unknown>);
    setLimits((detail.limits ?? {}) as Record<string, unknown>);
  }, [detail.config, detail.limits]);

  async function save() {
    setBusy("save");
    onError(null);
    try {
      await clientApi.strategyUpdate(detail.strategy.id, {
        config,
        limits,
        reason: "dashboard_manifest_edit",
      });
      onNotice(t("configLimitsUpdated"));
      await onAfterSave();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card
      title={t("riskLimitsTitle")}
      description={t("riskLimitsDesc")}
      actions={
        <button
          onClick={() => void save()}
          disabled={busy !== null}
          className="btn btn-primary text-xs cursor-pointer"
        >
          {busy === "save" ? tCommon("saving") : tCommon("save")}
        </button>
      }
    >
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <ManifestSection
          label="limits.yml"
          description={t("limitsHint")}
          value={limits}
          onChange={setLimits}
          suggestions={LIMIT_SUGGESTIONS}
          emptyLabel={t("manifestEmpty")}
        />
        <ManifestSection
          label="config.yml"
          description={t("configHint")}
          value={config}
          onChange={setConfig}
          suggestions={CONFIG_SUGGESTIONS}
          emptyLabel={t("manifestEmpty")}
        />
      </div>
    </Card>
  );
}

function ManifestSection({
  label,
  description,
  value,
  onChange,
  suggestions,
  emptyLabel,
}: {
  label: string;
  description: string;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  suggestions: string[];
  emptyLabel: string;
}) {
  const count = Object.keys(value).length;
  return (
    <div>
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <div>
          <div className="text-[11px] text-ink-500 font-medium">
            {label}
          </div>
          <div className="text-[11px] text-ink-400 mt-0.5 max-w-md">
            {description}
          </div>
        </div>
        <span className="text-[10px] font-mono text-ink-500">
          {count} field{count === 1 ? "" : "s"}
        </span>
      </div>
      <KeyValueEditor
        value={value}
        onChange={onChange}
        suggestedKeys={suggestions}
        emptyLabel={emptyLabel}
      />
    </div>
  );
}
