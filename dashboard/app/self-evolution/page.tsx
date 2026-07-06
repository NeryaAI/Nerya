"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Advanced, Card, Empty, ErrorBanner, Json, PageBody, PageHeader, Pill } from "../../components/Page";
import { SwitchIndicator } from "../../components/SwitchControl";
import {
  CheckIcon,
  DiffIcon,
  EvolutionIcon,
  SearchIcon,
  ShieldCheckIcon,
  SparkIcon,
  WrenchIcon,
  XIcon,
} from "../../components/icons";
import { clientApi } from "../../lib/clientApi";
import type {
  EvolutionActionGates,
  EvolutionAsset,
  EvolutionAssetCandidate,
  EvolutionBacktestComparison,
  EvolutionCandidateBacktestPreview,
  EvolutionCandidateValidationPreview,
  EvolutionConfigSnapshot,
  EvolutionEvidenceArtifact,
  EvolutionEvidenceItem,
  EvolutionFitnessVector,
  EvolutionGdiBreakdown,
  EvolutionInboxEnvelope,
  EvolutionInboxEntry,
  EvolutionInboxGroup,
  EvolutionLineageGraph,
  EvolutionOptimizerCandidate,
  EvolutionOptimizerCandidateDecision,
  EvolutionOptimizerFeedbackFeature,
  EvolutionOptimizerFeedbackSummary,
  EvolutionOptimizerReport,
  EvolutionPostApplyMonitor,
  EvolutionPostApplyObservation,
  EvolutionProposalDetail,
  EvolutionProposalFileChange,
  EvolutionProcessArtifact,
  EvolutionProcessTrace,
  EvolutionTimelineEnvelope,
  EvolutionTimelineItem,
  EvolutionWhyReused,
  EvolutionWhyReusedAsset,
  EvolutionWhyReusedSignal,
} from "../../lib/evolutionTypes";

type Tab = "inbox" | "timeline" | "assets" | "proposals";
type CandidateFilter = "all" | "ready" | "blocked" | "positive" | "negative";
type ReplayStepKey = "prompt" | "input" | "output" | "change" | "validation";
type EvidenceDrawerState = {
  ref: string;
  loading: boolean;
  item?: EvolutionEvidenceItem | null;
  error?: string | null;
};
type ReplaySnapshot = {
  canReplay: boolean;
  model: string;
  subagent: string;
  stepState: Record<ReplayStepKey, boolean>;
};
type ReplaySnippet = {
  kind: "prompt" | "input" | "output";
  label: string;
  title: string;
  preview: string;
};
type PrimaryReplayArtifactRef = {
  sectionId: string;
  sectionTitle: string;
  artifact: EvolutionProcessArtifact;
  index: number;
};
type ReplayDigestKind = "subagent_payload" | "subagent_output" | "validation_plan";

const TAB_IDS: Tab[] = ["timeline", "proposals", "inbox", "assets"];
const CANDIDATE_FILTERS: CandidateFilter[] = ["all", "ready", "blocked", "positive", "negative"];
const HISTORY_PAGE_SIZE = 10;

function toneForStatus(status?: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  const s = String(status || "").toLowerCase();
  if (["applied", "approved", "passed", "promoted", "safe", "ok", "candidate", "improved", "complete"].includes(s)) return "ok";
  if (["warn", "warning", "pending_review", "draft", "proposed", "not_run", "missing_baseline", "unknown", "flat"].includes(s)) return "warn";
  if (["critical", "danger", "blocked", "failed", "rejected", "rolled_back", "regressed"].includes(s)) return "danger";
  if (["info", "signal", "validation"].includes(s)) return "brand";
  return "neutral";
}

function stageTone(stage?: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  if (stage === "signal") return "brand";
  if (stage === "proposal" || stage === "validation") return "warn";
  if (stage === "outcome" || stage === "asset") return "ok";
  return "neutral";
}

export default function SelfEvolutionPage() {
  const t = useTranslations("selfEvolution");
  const [tab, setTab] = useState<Tab>("timeline");
  const [envelope, setEnvelope] = useState<EvolutionTimelineEnvelope | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [strategy, setStrategy] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dreamEnabled, setDreamEnabled] = useState(false);
  const [dreamTime, setDreamTime] = useState("03:00");
  const [dreamTimezone, setDreamTimezone] = useState("Asia/Shanghai");
  const [evidenceDrawer, setEvidenceDrawer] = useState<EvidenceDrawerState | null>(null);

  async function load(refreshSignals = false) {
    setBusy(refreshSignals ? "collect" : "load");
    setError(null);
    try {
      if (refreshSignals) {
        await clientApi.evolutionSignals({
          refresh: true,
          strategy_id: strategy.trim() || undefined,
          limit: 200,
        });
      }
      const out = await clientApi.evolutionTimeline({
        strategy_id: strategy.trim() || undefined,
        query: query.trim() || undefined,
        limit: 80,
      });
      const defaultSelectedId = firstReplayableTimelineId(out.timeline) ?? out.timeline[0]?.id ?? null;
      setEnvelope(out);
      setSelectedId((current) =>
        current && out.timeline.some((item) => item.id === current)
          ? current
          : defaultSelectedId,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    void load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const schedule = envelope?.config.periodic_reflection;
    if (!schedule) return;
    setDreamEnabled(Boolean(schedule.enabled));
    setDreamTime(schedule.time || "03:00");
    setDreamTimezone(schedule.timezone || "Asia/Shanghai");
  }, [
    envelope?.config.periodic_reflection?.enabled,
    envelope?.config.periodic_reflection?.time,
    envelope?.config.periodic_reflection?.timezone,
  ]);

  const timeline = envelope?.timeline ?? [];
  const inbox = envelope?.inbox;
  const assets = envelope?.raw.assets ?? [];
  const candidates = envelope?.raw.candidates ?? [];
  const optimizerFeedback = envelope?.raw.optimizer_feedback;
  const proposals = envelope?.raw.proposals ?? [];
  const openProposals = useMemo(
    () =>
      proposals.filter((p) =>
        ["draft", "pending_review", "proposed", "approved"].includes(String(p.state || "")),
      ),
    [proposals],
  );
  const selected = timeline.find((item) => item.id === selectedId) ?? timeline[0] ?? null;

  async function runReflection() {
    setBusy("reflect");
    setError(null);
    try {
      const out = await clientApi.evolutionReflect();
      setNotice(t("reflectionCompleted", { result: String(out.count ?? out.proposals ?? t("done")) }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function saveDreamReflection() {
    setBusy("dream-save");
    setError(null);
    try {
      const out = await clientApi.evolutionReflectionScheduleUpdate({
        enabled: dreamEnabled,
        time: dreamTime,
        timezone: dreamTimezone,
      });
      if (!out.ok) throw new Error(JSON.stringify(out));
      setNotice(t("dreamSaved"));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function runDreamReflectionNow() {
    setBusy("dream-now");
    setError(null);
    try {
      const out = await clientApi.evolutionReflectionRunNow();
      const result = (out.result || {}) as Record<string, unknown>;
      setNotice(t("dreamRunResult", { status: String(result.status || out.ok || t("done")) }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function runTuningDryRun() {
    const sid = strategy.trim();
    if (!sid) {
      setError(t("enterStrategyFirst"));
      return;
    }
    setBusy("tuning");
    setError(null);
    try {
      const out = await clientApi.strategyRuntimeTuningRun({
        strategy_id: sid,
        dry_run: true,
        operator: "dashboard",
        note: "self-evolution dashboard dry-run",
      });
      setNotice(t("tuningResult", { sid, status: out.ok ? t("completed") : t("blockedWord") }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function promote(candidateId: string) {
    setBusy(candidateId);
    setError(null);
    try {
      const out = await clientApi.evolutionAssetPromote(candidateId);
      if (!out.ok) throw new Error(JSON.stringify(out));
      setNotice(t("promoted", { id: candidateId }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function reject(candidateId: string) {
    setBusy(candidateId);
    setError(null);
    try {
      const out = await clientApi.evolutionAssetReject(candidateId, "rejected from dashboard");
      if (!out.ok) throw new Error(JSON.stringify(out));
      setNotice(t("rejected", { id: candidateId }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function validateProposal(proposalId: string, dryRun = true) {
    setBusy(proposalId);
    setError(null);
    try {
      const out = await clientApi.evolutionValidationRun({
        proposal_id: proposalId,
        dry_run: dryRun,
      });
      const status = String(out.status || (out.ok ? t("readyWord") : t("blockedWord")));
      setNotice(
        dryRun
          ? t("validationPlanCheckResult", { id: proposalId, status: out.ok ? t("readyWord") : t("blockedWord") })
          : t("validationRunResult", { id: proposalId, status }),
      );
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function approveProposal(proposalId: string) {
    setBusy(proposalId);
    setError(null);
    try {
      const out = await clientApi.proposalApprove(proposalId);
      if (out.error) throw new Error(String(out.error));
      setNotice(t("proposalApproved", { id: proposalId }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function rejectProposal(proposalId: string) {
    const note = window.prompt(t("rejectProposalPrompt", { id: proposalId }), "rejected from dashboard");
    if (note === null) return;
    setBusy(proposalId);
    setError(null);
    try {
      const out = await clientApi.proposalReject(proposalId, note);
      if (out.error) throw new Error(String(out.error));
      setNotice(t("proposalRejected", { id: proposalId }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function applyProposal(proposalId: string) {
    setBusy(proposalId);
    setError(null);
    try {
      const out = await clientApi.proposalApply(proposalId);
      if (!out.ok) {
        const reason = String(out.reason || out.error || t("applyFailed"));
        throw new Error(reason);
      }
      setNotice(t("proposalApplied", { id: proposalId }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function rollbackProposal(proposalId: string) {
    if (!window.confirm(t("rollbackProposalConfirm", { id: proposalId }))) return;
    setBusy(proposalId);
    setError(null);
    try {
      const out = await clientApi.proposalRollback(proposalId);
      if (!out.ok) {
        const reason = String(out.reason || out.error || t("rollbackFailed"));
        throw new Error(reason);
      }
      setNotice(t("proposalRolledBack", { id: proposalId }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function recordPostApplyObservation(proposalId: string, status: "healthy" | "regressed") {
    const summary = window.prompt(t("postApplyObservationPrompt", { status }), "");
    if (summary === null) return;
    setBusy(proposalId);
    setError(null);
    try {
      const out = await clientApi.proposalPostApplyObservation({
        proposal_id: proposalId,
        status,
        summary,
        source: "dashboard",
        evidence_refs: [`proposal:${proposalId}`],
        operator: "dashboard",
      });
      if (!out.ok) {
        const reason = String(out.reason || out.error || t("postApplyObservationFailed"));
        throw new Error(reason);
      }
      setNotice(t("postApplyObservationRecorded", { id: proposalId, status }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function openEvidence(ref: string) {
    const clean = ref.trim();
    if (!clean) return;
    setEvidenceDrawer({ ref: clean, loading: true });
    try {
      const out = await clientApi.evolutionEvidenceResolve({ refs: [clean] });
      setEvidenceDrawer({
        ref: clean,
        loading: false,
        item: out.items[0] ?? null,
      });
    } catch (e) {
      setEvidenceDrawer({
        ref: clean,
        loading: false,
        error: e instanceof Error ? e.message : String(e),
      });
    }
  }

  return (
    <div>
      <PageHeader
        eyebrow={t("eyebrow")}
        title={t("title")}
        description={t("description")}
        actions={
          <>
            <button className="btn btn-ghost" onClick={() => void runReflection()} disabled={Boolean(busy)}>
              <SparkIcon size={14} />
              {busy === "reflect" ? t("reflecting") : t("runReflection")}
            </button>
            <button className="btn btn-primary" onClick={() => void load(true)} disabled={Boolean(busy)}>
              <EvolutionIcon size={14} />
              {busy === "collect" ? t("collecting") : t("collectSignals")}
            </button>
          </>
        }
      />
      <PageBody>
        {error ? <ErrorBanner error={error} /> : null}
        {notice ? (
          <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-sm text-emerald-200">
            {notice}
          </div>
        ) : null}

        <div className="overflow-x-auto border-b border-brand-500/10">
          {TAB_IDS.map((id) => {
            const labelKey = id === "inbox" ? "tabInbox" : id === "timeline" ? "tabHistory" : id === "assets" ? "tabAssets" : "tabProposals";
            return (
              <button
                key={id}
                className={[
                  "px-3 py-2 text-[12px]",
                  tab === id ? "border-b border-brand-300 text-white" : "text-ink-400",
                ].join(" ")}
                onClick={() => setTab(id)}
              >
                {t(labelKey)}
              </button>
            );
          })}
        </div>

        {tab === "inbox" ? (
          <EvolutionInboxPanel
            inbox={inbox}
            timeline={timeline}
            busy={busy}
            onOpenItem={(itemId) => {
              setSelectedId(itemId);
              setTab("timeline");
            }}
            onOpenProposals={() => setTab("proposals")}
            onValidate={validateProposal}
            onApprove={approveProposal}
            onReject={rejectProposal}
            onApply={applyProposal}
            onRollback={rollbackProposal}
            onPostApplyObservation={recordPostApplyObservation}
          />
        ) : null}

        {tab === "timeline" ? (
          timeline.length ? (
            <TimelineConsole
              items={timeline}
              selected={selected}
              selectedId={selectedId}
              onSelect={setSelectedId}
              busy={busy}
              onEvidenceRef={openEvidence}
              onRollback={rollbackProposal}
              onPostApplyObservation={recordPostApplyObservation}
            />
          ) : (
            <EmptyHistory
              busy={busy}
              onCollect={() => void load(true)}
              onOpenProposals={() => setTab("proposals")}
            />
          )
        ) : null}

        {tab === "assets" ? (
          <AssetsPanel
            assets={assets}
            candidates={candidates}
            optimizerFeedback={optimizerFeedback}
            busy={busy}
            onEvidenceRef={openEvidence}
            onPromote={promote}
            onReject={reject}
          />
        ) : null}

        {tab === "proposals" ? (
          <ProposalsPanel
            proposals={openProposals}
            busy={busy}
            onValidate={validateProposal}
            onApprove={approveProposal}
            onReject={rejectProposal}
            onApply={applyProposal}
            onEvidenceRef={openEvidence}
          />
        ) : null}

        <Advanced
          title={t("replayControlsTitle")}
          description={t("replayControlsHint")}
          storageKey="nerya.evolution.advanced.controls"
        >
          <Filters
            strategy={strategy}
            query={query}
            busy={busy}
            onStrategy={setStrategy}
            onQuery={setQuery}
            onFilter={() => void load(false)}
            onTuning={runTuningDryRun}
          />
        </Advanced>

        <Advanced
          title={t("dreamPanelTitle")}
          description={t("dreamPanelHint")}
          storageKey="nerya.evolution.advanced.dream"
        >
          <DreamReflectionPanel
            schedule={envelope?.config.periodic_reflection}
            enabled={dreamEnabled}
            time={dreamTime}
            timezone={dreamTimezone}
            busy={busy}
            onEnabled={setDreamEnabled}
            onTime={setDreamTime}
            onTimezone={setDreamTimezone}
            onSave={saveDreamReflection}
            onRunNow={runDreamReflectionNow}
          />
        </Advanced>

        <EvidenceDrawer
          state={evidenceDrawer}
          onClose={() => setEvidenceDrawer(null)}
        />
      </PageBody>
    </div>
  );
}

function Filters({
  strategy,
  query,
  busy,
  onStrategy,
  onQuery,
  onFilter,
  onTuning,
}: {
  strategy: string;
  query: string;
  busy: string;
  onStrategy: (value: string) => void;
  onQuery: (value: string) => void;
  onFilter: () => void;
  onTuning: () => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  const hasStrategy = Boolean(strategy.trim());
  return (
    <div className="grid gap-3 border-b border-brand-500/10 pb-4 lg:grid-cols-[minmax(180px,260px)_1fr_auto] lg:items-end">
      <div>
        <label className="block text-[12px] text-ink-400">
          {t("strategy")}
          <input
            value={strategy}
            onChange={(e) => onStrategy(e.target.value)}
            className="input-dark mt-1 font-mono"
            placeholder={t("strategyPlaceholder")}
          />
        </label>
        {hasStrategy ? (
          <button
            type="button"
            onClick={() => void onTuning()}
            disabled={Boolean(busy)}
            className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-brand-300 transition hover:text-brand-200 disabled:opacity-50"
          >
            <WrenchIcon size={12} />
            {busy === "tuning" ? t("running") : t("tuningDryRun")} →
          </button>
        ) : null}
      </div>
      <label className="text-[12px] text-ink-400">
        {t("searchLabel")}
        <input
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          className="input-dark mt-1"
          placeholder={t("searchPlaceholder")}
        />
      </label>
      <div className="flex flex-wrap justify-start gap-2 lg:justify-end">
        <button className="btn btn-ghost" onClick={onFilter} disabled={Boolean(busy)}>
          <SearchIcon size={14} />
          {t("filter")}
        </button>
      </div>
    </div>
  );
}

function DreamReflectionPanel({
  schedule,
  enabled,
  time,
  timezone,
  busy,
  onEnabled,
  onTime,
  onTimezone,
  onSave,
  onRunNow,
}: {
  schedule?: EvolutionConfigSnapshot["periodic_reflection"];
  enabled: boolean;
  time: string;
  timezone: string;
  busy: string;
  onEnabled: (value: boolean) => void;
  onTime: (value: string) => void;
  onTimezone: (value: string) => void;
  onSave: () => Promise<void>;
  onRunNow: () => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  const disabled = Boolean(busy);
  return (
    <details className="rounded-lg border border-[color:var(--line)]" open={enabled}>
      <summary className="cursor-pointer px-4 py-3 text-[13px] font-medium text-ink-200 hover:text-ink-100 flex items-center gap-2">
        <span>{t("dreamTitle")}</span>
        <Pill tone={enabled ? "ok" : "neutral"}>{enabled ? t("enabledStatus") : t("disabledStatus")}</Pill>
        <span className="ml-auto text-[12px] text-ink-500 font-mono">{schedule?.cron || ""}</span>
      </summary>
      <div className="border-t border-[color:var(--line)] px-4 py-3">
      <div className="grid gap-3 lg:grid-cols-[minmax(170px,220px)_160px_minmax(180px,260px)_1fr_auto] lg:items-end">
        <label className="flex min-h-[42px] items-center justify-between gap-3 rounded-lg border border-brand-500/10 bg-ink-950/30 px-3 py-2 text-sm text-ink-200">
          <span>{t("dreamEnabled")}</span>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => onEnabled(event.target.checked)}
            className="sr-only"
            disabled={disabled}
          />
          <SwitchIndicator
            checked={enabled}
            label={t("dreamEnabled")}
            tone="accent"
            size="sm"
            toneWhenOff
          />
        </label>
        <label className="text-[12px] text-ink-300">
          {t("dreamTime")}
          <input
            type="time"
            value={time}
            onChange={(event) => onTime(event.target.value)}
            className="input-dark mt-1 font-mono"
            disabled={disabled}
          />
        </label>
        <label className="text-[12px] text-ink-300">
          {t("dreamTimezone")}
          <input
            value={timezone}
            onChange={(event) => onTimezone(event.target.value)}
            className="input-dark mt-1 font-mono"
            placeholder="Asia/Shanghai"
            disabled={disabled}
          />
        </label>
        <div className="min-w-0 rounded-lg border border-brand-500/10 bg-ink-950/30 px-3 py-2 text-[12px] text-ink-400">
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={enabled ? "ok" : "neutral"}>{enabled ? t("enabledStatus") : t("disabledStatus")}</Pill>
            <Pill tone={schedule?.configured ? "brand" : "neutral"}>
              {schedule?.configured ? t("configuredStatus") : t("notConfiguredStatus")}
            </Pill>
          </div>
          <div className="mt-2 truncate font-mono">{schedule?.cron || t("notScheduled")}</div>
          <div className="mt-1 truncate font-mono">{schedule?.target || "skill:evolution.reflect"}</div>
        </div>
        <div className="flex flex-wrap justify-start gap-2 lg:justify-end">
          <button className="btn btn-ghost" onClick={() => void onRunNow()} disabled={disabled}>
            <SparkIcon size={14} />
            {busy === "dream-now" ? t("reflecting") : t("dreamRunNow")}
          </button>
          <button className="btn btn-primary" onClick={() => void onSave()} disabled={disabled}>
            {busy === "dream-save" ? t("saving") : t("saveDream")}
          </button>
        </div>
      </div>
      </div>
    </details>
  );
}

function EmptyHistory({
  busy,
  onCollect,
  onOpenProposals,
}: {
  busy: string;
  onCollect: () => void;
  onOpenProposals: () => void;
}) {
  const t = useTranslations("selfEvolution");
  return (
    <Card title={t("noHistoryTitle")}>
      <div className="py-6 text-center">
        <div className="text-sm text-ink-300">{t("noMatchTitle")}</div>
        <div className="mt-1 text-[12px] text-ink-500">{t("noMatchSubtitle")}</div>
        <div className="mt-5 flex flex-wrap justify-center gap-2">
          <button className="btn btn-primary" onClick={onCollect} disabled={Boolean(busy)}>
            <EvolutionIcon size={14} />
            {t("collectSignals")}
          </button>
          <button
            type="button"
            onClick={onOpenProposals}
            className="text-[12px] text-brand-300 transition hover:text-brand-200"
          >
            {t("openProposalsBtn")} →
          </button>
        </div>
      </div>
    </Card>
  );
}

function EvolutionInboxPanel({
  inbox,
  timeline,
  busy,
  onOpenItem,
  onOpenProposals,
  onValidate,
  onApprove,
  onReject,
  onApply,
  onRollback,
  onPostApplyObservation,
}: {
  inbox?: EvolutionInboxEnvelope;
  timeline: EvolutionTimelineItem[];
  busy: string;
  onOpenItem: (itemId: string) => void;
  onOpenProposals: () => void;
  onValidate: (id: string, dryRun?: boolean) => Promise<void>;
  onApprove: (id: string) => Promise<void>;
  onReject: (id: string) => Promise<void>;
  onApply: (id: string) => Promise<void>;
  onRollback: (proposalId: string) => Promise<void>;
  onPostApplyObservation: (proposalId: string, status: "healthy" | "regressed") => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  const groups = inbox?.groups ?? [];
  const total = inbox?.total ?? 0;
  const itemIds = new Set(timeline.map((item) => item.id));
  const itemById = new Map(timeline.map((item) => [item.id, item]));
  if (!groups.length || !total) {
    return (
      <Card title={t("inboxTitle")} description={t("inboxDesc")}>
        <div className="py-6 text-center">
          <Empty title={t("inboxNoItems")} subtitle={t("inboxNoItemsSub")} />
          <button
            type="button"
            onClick={onOpenProposals}
            className="mt-4 text-[12px] text-brand-300 transition hover:text-brand-200"
          >
            {t("openProposalsBtn")} →
          </button>
        </div>
      </Card>
    );
  }
  return (
    <section className="space-y-4" data-testid="evolution-inbox-panel">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-brand-500/10 bg-ink-950/30 px-4 py-3">
        <div>
          <h3 className="text-base font-semibold text-ink-50">{t("inboxTitle")}</h3>
          <p className="mt-1 text-[12px] text-ink-500">{t("inboxDesc")}</p>
        </div>
        <Pill tone={total ? "brand" : "neutral"}>{t("inboxTotal", { count: total })}</Pill>
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {groups.map((group) => (
          <InboxGroupCard
            key={group.id}
            group={group}
            itemIds={itemIds}
            itemById={itemById}
            busy={busy}
            onOpenItem={onOpenItem}
            onValidate={onValidate}
            onApprove={onApprove}
            onReject={onReject}
            onApply={onApply}
            onRollback={onRollback}
            onPostApplyObservation={onPostApplyObservation}
          />
        ))}
      </div>
    </section>
  );
}

function InboxGroupCard({
  group,
  itemIds,
  itemById,
  busy,
  onOpenItem,
  onValidate,
  onApprove,
  onReject,
  onApply,
  onRollback,
  onPostApplyObservation,
}: {
  group: EvolutionInboxGroup;
  itemIds: Set<string>;
  itemById: Map<string, EvolutionTimelineItem>;
  busy: string;
  onOpenItem: (itemId: string) => void;
  onValidate: (id: string, dryRun?: boolean) => Promise<void>;
  onApprove: (id: string) => Promise<void>;
  onReject: (id: string) => Promise<void>;
  onApply: (id: string) => Promise<void>;
  onRollback: (proposalId: string) => Promise<void>;
  onPostApplyObservation: (proposalId: string, status: "healthy" | "regressed") => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  const titleById: Record<string, string> = {
    needs_evidence: t("inboxNeedsEvidence"),
    needs_materialization: t("inboxNeedsMaterialization"),
    needs_validation: t("inboxNeedsValidation"),
    needs_approval: t("inboxNeedsApproval"),
    monitoring: t("inboxMonitoring"),
    reusable_learning: t("inboxReusableLearning"),
    negative_learning: t("inboxNegativeLearning"),
  };
  const descriptionById: Record<string, string> = {
    needs_evidence: t("inboxNeedsEvidenceDesc"),
    needs_materialization: t("inboxNeedsMaterializationDesc"),
    needs_validation: t("inboxNeedsValidationDesc"),
    needs_approval: t("inboxNeedsApprovalDesc"),
    monitoring: t("inboxMonitoringDesc"),
    reusable_learning: t("inboxReusableLearningDesc"),
    negative_learning: t("inboxNegativeLearningDesc"),
  };
  return (
    <section className="min-w-0 overflow-hidden rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={inboxTone(group.tone)}>{group.count}</Pill>
            <h4 className="text-sm font-semibold text-ink-100">{titleById[group.id] ?? group.id}</h4>
          </div>
          <div className="mt-1 text-[12px] text-ink-500">{descriptionById[group.id] ?? ""}</div>
        </div>
        {group.stage ? <Pill tone={stageTone(group.stage)}>{group.stage}</Pill> : null}
      </div>
      {group.items.length ? (
        <div className="mt-3 space-y-2">
          {group.items.slice(0, 6).map((entry) => {
            const itemId = String(entry.item_id || "");
            const canOpen = Boolean(itemId && itemIds.has(itemId));
            const timelineItem = itemById.get(itemId) ?? null;
            const proposalId = String(entry.proposal_id || "");
            return (
              <div
                key={entry.id}
                className="min-w-0 overflow-hidden rounded-md border border-brand-500/10 bg-ink-900/45 px-3 py-2 text-sm"
              >
                <button
                  type="button"
                  disabled={!canOpen}
                  onClick={() => canOpen && onOpenItem(itemId)}
                  className="block min-w-0 w-full text-left transition hover:text-brand-100 disabled:cursor-default disabled:hover:text-inherit"
                >
                  <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
                    <Pill tone={toneForStatus(entry.status)}>{entry.status || t("unknown")}</Pill>
                    {entry.strategy_id ? <Pill tone="neutral">{entry.strategy_id}</Pill> : null}
                    <span className="ml-0 font-mono text-[11px] text-ink-500 sm:ml-auto">{formatTime(entry.ts)}</span>
                  </div>
                  <div className="mt-2 font-medium text-ink-100">{entry.title}</div>
                  {entry.summary ? <div className="mt-1 line-clamp-2 text-[12px] text-ink-400">{entry.summary}</div> : null}
                  <div className="mt-2 flex min-w-0 max-w-full flex-wrap gap-1.5">
                    {entry.proposal_id ? <Pill tone="warn">proposal:{shortId(entry.proposal_id)}</Pill> : null}
                    {entry.validation_plan_id ? <Pill tone="brand">validation:{shortId(entry.validation_plan_id)}</Pill> : null}
                    {entry.evidence_refs?.length ? (
                      <Pill tone="neutral">{t("inboxEvidenceCount", { count: entry.evidence_refs.length })}</Pill>
                    ) : null}
                  </div>
                  {entry.reasons?.length ? (
                    <div className="mt-2">
                      <TokenList values={entry.reasons.slice(0, 3)} empty="" />
                    </div>
                  ) : null}
                  {entry.next_step ? <div className="mt-2 text-[12px] text-ink-500">{entry.next_step}</div> : null}
                  <InboxReplayTracePreview item={timelineItem} />
                </button>
                <InboxEntryActions
                  groupId={group.id}
                  item={timelineItem}
                  itemId={itemId}
                  canOpen={canOpen}
                  proposalId={proposalId}
                  status={String(entry.status || "")}
                  busy={busy}
                  onOpenItem={onOpenItem}
                  onValidate={onValidate}
                  onApprove={onApprove}
                  onReject={onReject}
                  onApply={onApply}
                  onRollback={onRollback}
                  onPostApplyObservation={onPostApplyObservation}
                />
              </div>
            );
          })}
          {group.items.length > 6 ? (
            <div className="text-[12px] text-ink-500">{t("inboxMore", { count: group.items.length - 6 })}</div>
          ) : null}
        </div>
      ) : (
        <div className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/30 px-3 py-4 text-sm text-ink-500">
          {t("inboxEmptyGroup")}
        </div>
      )}
    </section>
  );
}

function InboxReplayTracePreview({
  item,
}: {
  item: EvolutionTimelineItem | null;
}) {
  const t = useTranslations("selfEvolution");
  if (!item) return null;
  const replay = replaySnapshotForItem(item, t("exactModelMissing"));
  if (!replay.canReplay) return null;
  const snippets = replaySnippetsForProcess(item.process, {
    prompt: t("runTracePrompt"),
    input: t("runTraceInput"),
    output: t("runTraceOutput"),
  });
  return (
    <div className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/35 p-2" data-testid="inbox-run-trace-preview">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-medium text-ink-500">{t("runTracePreview")}</div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Pill tone="brand">{replay.model}</Pill>
          {replay.subagent ? <Pill tone="neutral">{replay.subagent}</Pill> : null}
        </div>
      </div>
      <ReplaySnippetGrid snippets={snippets} />
    </div>
  );
}

function ReplaySnippetGrid({ snippets }: { snippets: ReplaySnippet[] }) {
  if (!snippets.length) return null;
  return (
    <div className="mt-2 grid gap-2 lg:grid-cols-3" data-testid="replay-snippet-preview">
      {snippets.map((snippet) => (
        <div key={snippet.kind} className="min-w-0 rounded border border-brand-500/10 bg-ink-950/45 p-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <Pill tone={snippet.kind === "output" ? "ok" : "brand"}>{snippet.label}</Pill>
            <span className="min-w-0 truncate text-[11px] font-medium text-ink-300">{snippet.title}</span>
          </div>
          <pre className="mt-1.5 max-h-24 overflow-hidden whitespace-pre-wrap break-words text-[11px] leading-relaxed text-ink-400">
            {snippet.preview}
          </pre>
        </div>
      ))}
    </div>
  );
}

function InboxEntryActions({
  groupId,
  item,
  itemId,
  canOpen,
  proposalId,
  status,
  busy,
  onOpenItem,
  onValidate,
  onApprove,
  onReject,
  onApply,
  onRollback,
  onPostApplyObservation,
}: {
  groupId: string;
  item: EvolutionTimelineItem | null;
  itemId: string;
  canOpen: boolean;
  proposalId: string;
  status: string;
  busy: string;
  onOpenItem: (itemId: string) => void;
  onValidate: (id: string, dryRun?: boolean) => Promise<void>;
  onApprove: (id: string) => Promise<void>;
  onReject: (id: string) => Promise<void>;
  onApply: (id: string) => Promise<void>;
  onRollback: (proposalId: string) => Promise<void>;
  onPostApplyObservation: (proposalId: string, status: "healthy" | "regressed") => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  const replay = inboxReplayCue(item, t("exactModelMissing"));
  if (!proposalId && !replay) return null;
  const isBusy = busy === proposalId;
  const openState = ["draft", "pending_review", "proposed", "approved"].includes(status);
  const applied = status === "applied";
  return (
    <div className="mt-3 flex max-w-full flex-col gap-2 border-t border-brand-500/10 pt-2 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
      {replay ? (
        <button
          type="button"
          data-testid="inbox-replay-run"
          className="btn btn-ghost max-w-full px-3 py-1.5 text-[12px] w-full sm:w-auto"
          disabled={!canOpen}
          onClick={() => canOpen && onOpenItem(itemId)}
        >
          <SearchIcon size={13} />
          <span>{t("inboxReplayRun")}</span>
          <span className="max-w-[11rem] truncate font-mono text-[11px] text-ink-500">{replay.model}</span>
          {replay.subagent ? <span className="max-w-[8rem] truncate font-mono text-[11px] text-ink-500">{replay.subagent}</span> : null}
        </button>
      ) : <span />}
      <div className="flex max-w-full flex-wrap justify-start gap-2 sm:justify-end">
        {proposalId && groupId === "needs_validation" ? (
          <>
            <button className="btn btn-ghost px-3 py-1.5 text-[12px] w-full sm:w-auto" disabled={isBusy} onClick={() => void onValidate(proposalId, true)}>
              <ShieldCheckIcon size={13} />
              {t("checkPlan")}
            </button>
            <button className="btn btn-primary px-3 py-1.5 text-[12px] w-full sm:w-auto" disabled={isBusy} onClick={() => void onValidate(proposalId, false)}>
              <ShieldCheckIcon size={13} />
              {isBusy ? t("validating") : t("runValidation")}
            </button>
          </>
        ) : null}
        {proposalId && (groupId === "needs_approval" || status === "approved") ? (
          <>
            {status !== "approved" ? (
              <button className="btn btn-ghost px-3 py-1.5 text-[12px] w-full sm:w-auto" disabled={isBusy} onClick={() => void onApprove(proposalId)}>
                <CheckIcon size={13} />
                {isBusy ? t("approving") : t("approveProposal")}
              </button>
            ) : null}
            {status === "approved" ? (
              <button className="btn btn-primary px-3 py-1.5 text-[12px] w-full sm:w-auto" disabled={isBusy} onClick={() => void onApply(proposalId)}>
                <CheckIcon size={13} />
                {isBusy ? t("applying") : t("applyProposal")}
              </button>
            ) : null}
          </>
        ) : null}
        {proposalId && groupId === "monitoring" && applied ? (
          <>
            <button className="btn btn-ghost px-3 py-1.5 text-[12px] w-full sm:w-auto" disabled={isBusy} onClick={() => void onPostApplyObservation(proposalId, "healthy")}>
              <CheckIcon size={13} />
              {t("recordHealthy")}
            </button>
            <button className="btn btn-ghost px-3 py-1.5 text-[12px] w-full sm:w-auto" disabled={isBusy} onClick={() => void onPostApplyObservation(proposalId, "regressed")}>
              <XIcon size={13} />
              {t("recordRegression")}
            </button>
          </>
        ) : null}
        {proposalId && groupId === "negative_learning" && applied ? (
          <button className="btn btn-primary px-3 py-1.5 text-[12px] w-full sm:w-auto" disabled={isBusy} onClick={() => void onRollback(proposalId)}>
            <XIcon size={13} />
            {isBusy ? t("rollingBack") : t("rollbackProposal")}
          </button>
        ) : null}
        {proposalId && openState ? (
          <button className="btn btn-ghost px-3 py-1.5 text-[12px] w-full sm:w-auto" disabled={isBusy} onClick={() => void onReject(proposalId)}>
            <XIcon size={13} />
            {isBusy ? t("rejecting") : t("rejectProposal")}
          </button>
        ) : null}
      </div>
    </div>
  );
}

function inboxReplayCue(item: EvolutionTimelineItem | null, missingModel: string) {
  if (!item) return null;
  const replay = replaySnapshotForItem(item, missingModel);
  if (!replay.canReplay) return null;
  return { model: replay.model, subagent: replay.subagent };
}

function inboxTone(tone?: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  if (tone === "ok" || tone === "warn" || tone === "danger" || tone === "brand" || tone === "neutral") {
    return tone;
  }
  return "neutral";
}

function TimelineConsole({
  items,
  selected,
  selectedId,
  onSelect,
  busy,
  onEvidenceRef,
  onRollback,
  onPostApplyObservation,
}: {
  items: EvolutionTimelineItem[];
  selected: EvolutionTimelineItem | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  busy: string;
  onEvidenceRef: (ref: string) => void;
  onRollback: (proposalId: string) => Promise<void>;
  onPostApplyObservation: (proposalId: string, status: "healthy" | "regressed") => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  const [historyOpen, setHistoryOpen] = useState(false);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-brand-500/10 bg-ink-950/30 px-3 py-2">
        <div className="min-w-0">
          <div className="text-[11px] font-medium text-ink-500">{t("historyTitle", { count: items.length })}</div>
          <div className="mt-0.5 truncate text-[12px] text-ink-400">
            {selected?.title || t("selectRow")}
          </div>
        </div>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => setHistoryOpen(true)}
        >
          <SearchIcon size={14} />
          {t("openHistoryDrawer")}
        </button>
      </div>
      <div className="min-w-0" data-testid="timeline-detail-panel">
        <TimelineDetail
          item={selected}
          busy={busy}
          onEvidenceRef={onEvidenceRef}
          onRollback={onRollback}
          onPostApplyObservation={onPostApplyObservation}
        />
      </div>
      {historyOpen ? (
        <TimelineHistoryDrawer
          items={items}
          selectedId={selectedId}
          onSelect={(id) => {
            onSelect(id);
            setHistoryOpen(false);
          }}
          onClose={() => setHistoryOpen(false)}
        />
      ) : null}
    </div>
  );
}

function TimelineHistoryDrawer({
  items,
  selectedId,
  onSelect,
  onClose,
}: {
  items: EvolutionTimelineItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const t = useTranslations("selfEvolution");
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-ink-950/60 backdrop-blur-sm" data-testid="timeline-history-drawer">
      <button
        type="button"
        className="absolute inset-0 cursor-default"
        aria-label={t("closeHistoryDrawer")}
        onClick={onClose}
      />
      <aside className="relative z-10 flex h-full w-full max-w-xl flex-col border-l border-brand-500/15 bg-ink-950 shadow-2xl">
        <TimelineHistoryPanel
          items={items}
          selectedId={selectedId}
          onSelect={onSelect}
          onClose={onClose}
          variant="drawer"
        />
      </aside>
    </div>
  );
}

function TimelineHistoryPanel({
  items,
  selectedId,
  onSelect,
  onClose,
  variant = "card",
}: {
  items: EvolutionTimelineItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onClose?: () => void;
  variant?: "card" | "drawer";
}) {
  const t = useTranslations("selfEvolution");
  const [page, setPage] = useState(1);
  const pageCount = Math.max(1, Math.ceil(items.length / HISTORY_PAGE_SIZE));
  const safePage = Math.min(page, pageCount);
  const startIndex = (safePage - 1) * HISTORY_PAGE_SIZE;
  const pageItems = items.slice(startIndex, startIndex + HISTORY_PAGE_SIZE);
  const endIndex = Math.min(items.length, startIndex + pageItems.length);

  useEffect(() => {
    setPage(1);
  }, [items.length]);

  useEffect(() => {
    if (!selectedId) return;
    const selectedIndex = items.findIndex((item) => item.id === selectedId);
    if (selectedIndex < 0) return;
    setPage(Math.floor(selectedIndex / HISTORY_PAGE_SIZE) + 1);
  }, [items, selectedId]);

  return (
    <section
      className={
        variant === "drawer"
          ? "flex h-full min-w-0 flex-col overflow-hidden"
          : "card card-hover min-w-0 overflow-hidden xl:flex xl:h-full xl:min-h-0 xl:flex-col xl:self-stretch"
      }
      data-testid="timeline-history-panel"
    >
      <div className={variant === "drawer" ? "flex items-start justify-between gap-3 border-b border-brand-500/10 px-5 py-4" : "card-head"}>
        <div className="min-w-0">
          <h3 className="card-title break-words">{t("historyTitle", { count: items.length })}</h3>
          <p className="card-subtle mt-1 break-words">{t("historyDesc")}</p>
        </div>
        {onClose ? (
          <button
            type="button"
            className="btn btn-ghost px-2"
            aria-label={t("closeHistoryDrawer")}
            onClick={onClose}
          >
            <XIcon size={16} />
          </button>
        ) : null}
      </div>
      <div className="flex min-h-0 flex-1 flex-col px-5 py-4">
        <div className="embedded-list-scroll-lg min-h-0 flex-1 divide-y divide-brand-500/10 xl:max-h-none">
          {pageItems.map((item) => (
            <TimelineRow
              key={item.id}
              item={item}
              selected={selectedId === item.id}
              onSelect={() => onSelect(item.id)}
            />
          ))}
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-brand-500/10 pt-3 text-[12px] text-ink-400">
          <span className="font-mono">
            {t("historyPageStatus", {
              start: items.length ? startIndex + 1 : 0,
              end: endIndex,
              total: items.length,
            })}
          </span>
          <div className="flex items-center gap-2">
            <button
              className="btn btn-ghost px-3 py-1.5 text-[12px]"
              onClick={() => setPage((value) => Math.max(1, value - 1))}
              disabled={safePage <= 1}
            >
              {t("prevPage")}
            </button>
            <span className="font-mono text-ink-500">{safePage}/{pageCount}</span>
            <button
              className="btn btn-ghost px-3 py-1.5 text-[12px]"
              onClick={() => setPage((value) => Math.min(pageCount, value + 1))}
              disabled={safePage >= pageCount}
            >
              {t("nextPage")}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}

function TimelineRow({
  item,
  selected,
  onSelect,
}: {
  item: EvolutionTimelineItem;
  selected: boolean;
  onSelect: () => void;
}) {
  const t = useTranslations("selfEvolution");
  const replay = replaySnapshotForItem(item, t("exactModelMissing"));
  return (
    <button
      className={[
        "block w-full py-3 text-left text-sm transition",
        selected ? "bg-brand-500/10 px-3" : "px-1 hover:bg-white/[0.03]",
      ].join(" ")}
      onClick={onSelect}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={stageTone(item.stage)}>{item.stage}</Pill>
        <Pill tone={toneForStatus(item.status)}>{item.status || t("unknown")}</Pill>
        {item.strategy_id ? <Pill tone="neutral">{item.strategy_id}</Pill> : null}
        <span className="ml-auto font-mono text-[11px] text-ink-500">{formatTime(item.ts)}</span>
      </div>
      <div className="mt-2 font-medium text-ink-100">{item.title}</div>
      <div className="mt-1 line-clamp-2 text-ink-400">{item.summary || item.why || t("noSummary")}</div>
      {replay.canReplay ? (
        <div className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/35 p-2" data-testid="timeline-replay-strip">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] font-medium text-ink-500">{t("runTracePreview")}</span>
            <Pill tone="brand">{replay.model}</Pill>
            {replay.subagent ? <Pill tone="neutral">{replay.subagent}</Pill> : null}
          </div>
          <ReplayStepStrip stepState={replay.stepState} compact />
        </div>
      ) : null}
      <div className="mt-2 flex flex-wrap gap-2 font-mono text-[11px] text-ink-500">
        {item.proposal_id ? <span>proposal:{shortId(item.proposal_id)}</span> : null}
        {item.validation_plan_id ? <span>validation:{shortId(item.validation_plan_id)}</span> : null}
        {item.evidence_refs?.slice(0, 2).map((ref) => <span key={ref}>{ref}</span>)}
      </div>
    </button>
  );
}

function TimelineDetail({
  item,
  busy,
  onEvidenceRef,
  onRollback,
  onPostApplyObservation,
}: {
  item: EvolutionTimelineItem | null;
  busy: string;
  onEvidenceRef: (ref: string) => void;
  onRollback: (proposalId: string) => Promise<void>;
  onPostApplyObservation: (proposalId: string, status: "healthy" | "regressed") => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  if (!item) {
    return (
      <Card title={t("timelineDetail")}>
        <Empty title={t("noItemSelected")} subtitle={t("selectRow")} />
      </Card>
    );
  }
  const process = item.process;
  const proposalId = String(item.proposal_id || "");
  const applied = Boolean(proposalId && (item.status === "applied" || item.outcome === "applied"));
  return (
    <Card
      title={t("timelineDetail")}
      actions={<Pill tone={toneForStatus(item.status)}>{item.status || t("unknown")}</Pill>}
    >
      <div className="space-y-4 text-sm">
        <AgentRunReplayPanel item={item} process={process} onEvidenceRef={onEvidenceRef} />
        <RunResultSummary
          proposalId={item.proposal_id}
          validationPlanId={item.validation_plan_id}
          validationStatus={item.validation_status}
          blockedReasons={item.blocked_reasons}
          evidenceRefs={item.evidence_refs}
          nextStep={item.next_step}
          onEvidenceRef={onEvidenceRef}
        />
        {applied ? (
          <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-[11px] font-medium text-ink-500">{t("proposalLifecycle")}</div>
                <div className="mt-1 text-[12px] text-ink-400">{t("proposalLifecycleDesc")}</div>
              </div>
              <Pill tone="ok">{t("applied")}</Pill>
            </div>
            <div className="mt-3 flex flex-wrap justify-end gap-2">
              <button
                className="btn btn-ghost"
                disabled={busy === proposalId}
                onClick={() => void onPostApplyObservation(proposalId, "healthy")}
              >
                <CheckIcon size={14} />
                {t("recordHealthy")}
              </button>
              <button
                className="btn btn-ghost"
                disabled={busy === proposalId}
                onClick={() => void onPostApplyObservation(proposalId, "regressed")}
              >
                <XIcon size={14} />
                {t("recordRegression")}
              </button>
              <button
                className="btn btn-primary"
                disabled={busy === proposalId}
                onClick={() => void onRollback(proposalId)}
              >
                <XIcon size={14} />
                {busy === proposalId ? t("rollingBack") : t("rollbackProposal")}
              </button>
            </div>
          </section>
        ) : null}
        <DebugDetails>
          <Json value={item.raw ?? item} />
        </DebugDetails>
      </div>
    </Card>
  );
}

type AgentFlowRole = "trigger" | "agent_input" | "agent_output" | "file_change" | "validation" | "learning";

type AgentFlowEntry = {
  id: string;
  role: AgentFlowRole;
  label: string;
  title: string;
  detail: string;
  path?: string;
  badges?: string[];
  artifact?: EvolutionProcessArtifact;
};

type AgentRunSubject = {
  id: string;
  title?: string;
  summary?: string;
  why?: string;
  strategy_id?: string | null;
  proposal_id?: string | null;
  validation_plan_id?: string | null;
};

type AgentFlowLabels = {
  runReplayTitle: string;
  runReplayDesc: string;
  runSubagent: string;
  runModel: string;
  runTier: string;
  runTokens: string;
  runCost: string;
  runWallTime: string;
  runModelCalls: string;
  notRecorded: string;
  exactModelMissing: string;
  trigger: string;
  prompt: string;
  input: string;
  agentInput: string;
  agentOutput: string;
  fileChange: string;
  validation: string;
  learning: string;
  evidence: string;
  promptTitle: string;
  inputTitle: string;
  outputTitle: string;
  validationTitle: string;
  fileTitle: string;
  fileSkill: string;
  fileMarkdown: string;
  stepRecorded: string;
  stepMissing: string;
  recommendationOnly: string;
  recommendationOnlyDesc: string;
  proposedWrite: (path: string) => string;
};

function AgentRunReplayPanel({
  item,
  process,
  onEvidenceRef,
}: {
  item?: AgentRunSubject | null;
  process?: EvolutionProcessTrace;
  onEvidenceRef?: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const labels: AgentFlowLabels = {
    runReplayTitle: t("agentRunReplay"),
    runReplayDesc: t("agentRunReplayDesc"),
    runSubagent: t("runSubagent"),
    runModel: t("runModel"),
    runTier: t("runTier"),
    runTokens: t("runTokens"),
    runCost: t("runCost"),
    runWallTime: t("runWallTime"),
    runModelCalls: t("runModelCalls"),
    notRecorded: t("notRecorded"),
    exactModelMissing: t("exactModelMissing"),
    trigger: t("flowTrigger"),
    prompt: t("flowPrompt"),
    input: t("flowInput"),
    agentInput: t("flowAgentInput"),
    agentOutput: t("flowAgentOutput"),
    fileChange: t("flowFileChange"),
    validation: t("flowValidation"),
    learning: t("flowLearning"),
    evidence: t("flowEvidence"),
    promptTitle: t("flowPromptTitle"),
    inputTitle: t("flowInputTitle"),
    outputTitle: t("flowOutputTitle"),
    validationTitle: t("flowValidationTitle"),
    fileTitle: t("flowFileTitle"),
    fileSkill: t("flowFileSkill"),
    fileMarkdown: t("flowFileMarkdown"),
    stepRecorded: t("stepRecorded"),
    stepMissing: t("stepMissing"),
    recommendationOnly: t("recommendationOnly"),
    recommendationOnlyDesc: t("recommendationOnlyDesc"),
    proposedWrite: (path: string) => t("flowProposedWrite", { path }),
  };
  const artifactRefs = primaryReplayArtifactRefs(process, labels.evidence).slice(0, 32);
  const run = process?.run ?? null;
  const stepState = replayStepStateForProcess(process);
  const exactModel = String(run?.model || "");
  const provider = String(run?.provider || "");
  const modelDisplay = exactModel ? `${provider ? `${provider}/` : ""}${exactModel}` : labels.exactModelMissing;
  const baseId = item?.id || "agent-run";
  const entries: AgentFlowEntry[] = [];
  if (item) {
    entries.push({
      id: `${baseId}:trigger`,
      role: "trigger",
      label: labels.trigger,
      title: item.title || t("flowRecord"),
      detail: item.why || item.summary || t("noTriggerExplanation"),
      badges: [
        item.strategy_id ? `strategy:${item.strategy_id}` : "",
        item.proposal_id ? `proposal:${shortId(item.proposal_id)}` : "",
        item.validation_plan_id ? `validation:${shortId(item.validation_plan_id)}` : "",
      ].filter(Boolean),
    });
  }

  for (const { sectionId, sectionTitle, artifact, index } of artifactRefs) {
    const role = flowRoleForArtifact(artifact, sectionId);
    const path = displayPathForArtifact(artifact);
    entries.push({
      id: `${baseId}:${sectionId}:${artifact.id}:${index}`,
      role,
      label: flowLabelForArtifact(role, artifact, sectionId, labels),
      title: flowTitle(role, artifact, sectionTitle, path, labels),
      detail: String(artifact.preview || ""),
      path,
      badges: [
        artifact.kind ? String(artifact.kind) : "",
        artifact.language ? String(artifact.language) : "",
        typeof artifact.size === "number" ? formatBytes(artifact.size) : "",
        artifact.redacted ? "redacted" : "",
        artifact.truncated ? "truncated" : "",
      ].filter(Boolean),
      artifact,
    });
  }

  const orderedEntries = entries
    .map((entry, index) => ({ entry, index }))
    .sort((a, b) => flowOrder(a.entry.role) - flowOrder(b.entry.role) || a.index - b.index);

  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3" data-testid="agent-run-replay">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] text-ink-500 font-medium">{labels.runReplayTitle}</div>
          <div className="mt-1 text-[12px] text-ink-400">{labels.runReplayDesc}</div>
        </div>
        <Pill tone={exactModel ? "brand" : "warn"}>{modelDisplay}</Pill>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {run?.subagent ? <RunMeta label={labels.runSubagent} value={String(run.subagent)} /> : null}
        {run?.tier ? <RunMeta label={labels.runTier} value={String(run.tier)} /> : null}
        {run?.tokens != null ? <RunMeta label={labels.runTokens} value={formatRunTokens(run.tokens, labels.notRecorded)} /> : null}
        {run?.wall_ms != null ? <RunMeta label={labels.runWallTime} value={formatDurationMs(run.wall_ms, labels.notRecorded)} /> : null}
        {run?.usd != null ? <RunMeta label={labels.runCost} value={formatRunCost(run.usd, labels.notRecorded)} /> : null}
        {exactModel ? <RunMeta label={labels.runModel} value={modelDisplay} wide /> : null}
      </div>
      {run?.model_calls?.length ? (
        <ModelCallList
          calls={run.model_calls}
          title={labels.runModelCalls}
          fallback={labels.notRecorded}
          onEvidenceRef={onEvidenceRef}
        />
      ) : null}
      <ReplayStepStrip
        stepState={stepState}
        recordedLabel={labels.stepRecorded}
        missingLabel={labels.stepMissing}
      />
      {!stepState.change ? (
        <div className="mt-2 flex flex-wrap items-start gap-2 rounded-md border border-amber-300/20 bg-amber-300/10 px-2.5 py-2 text-[12px] text-amber-100">
          <Pill tone="warn">{labels.recommendationOnly}</Pill>
          <span className="min-w-0 flex-1 break-words text-amber-100/85">{labels.recommendationOnlyDesc}</span>
        </div>
      ) : null}
      <ol className="mt-4 space-y-3">
        {orderedEntries.map(({ entry }, index) => (
          <AgentFlowBubble key={entry.id} entry={entry} index={index + 1} />
        ))}
      </ol>
      {!artifactRefs.length ? (
        <div className="mt-3 text-sm text-ink-500">{t("flowNoArtifacts")}</div>
      ) : null}
    </section>
  );
}

function RunMeta({
  label,
  value,
  wide = false,
}: {
  label: string;
  value: string;
  wide?: boolean;
}) {
  return (
    <div className={["min-w-0 rounded-md border border-brand-500/10 bg-ink-950/45 px-2.5 py-2", wide ? "sm:col-span-2 xl:col-span-2" : ""].join(" ")}>
      <div className="text-[11px] font-medium text-ink-500">{label}</div>
      <div className="mt-1 break-words font-mono text-[12px] text-ink-100">{value}</div>
    </div>
  );
}

function ModelCallList({
  calls,
  title,
  fallback,
  onEvidenceRef,
}: {
  calls: Array<Record<string, unknown>>;
  title: string;
  fallback: string;
  onEvidenceRef?: (ref: string) => void;
}) {
  const visibleCalls = calls.slice(0, 4);
  return (
    <div className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/45 p-2.5">
      <div className="mb-2 text-[11px] font-medium text-ink-500">{title}</div>
      <div className="grid gap-2 md:grid-cols-2">
        {visibleCalls.map((call, index) => {
          const provider = String(call.provider || "");
          const model = String(call.model || fallback);
          const titleText = provider && model !== fallback ? `${provider}/${model}` : model;
          const iteration = call.iteration !== undefined ? `#${String(call.iteration)}` : `#${index}`;
          const source = String(call.source || "");
          const evidenceRef = String(call.evidence_ref || "");
          return (
            <div key={`${titleText}:${index}`} className="min-w-0 rounded border border-brand-500/10 bg-ink-950/50 px-2 py-1.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <Pill tone="brand">{iteration}</Pill>
                <span className="break-all font-mono text-[12px] text-ink-100">{titleText}</span>
              </div>
              <div className="mt-1 flex flex-wrap gap-2 font-mono text-[11px] text-ink-500">
                {call.tier ? <span>{String(call.tier)}</span> : null}
                <span>{formatRunTokens(call.tokens, fallback)}</span>
                <span>{formatRunCost(call.usd, fallback)}</span>
                {source ? <span>{source}</span> : null}
              </div>
              {evidenceRef ? (
                <div className="mt-1.5">
                  <TokenList values={[evidenceRef]} empty="" onSelect={onEvidenceRef} />
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ReplayStepStrip({
  stepState,
  compact = false,
  recordedLabel,
  missingLabel,
}: {
  stepState: Record<ReplayStepKey, boolean>;
  compact?: boolean;
  recordedLabel?: string;
  missingLabel?: string;
}) {
  const t = useTranslations("selfEvolution");
  const steps: Array<{ key: ReplayStepKey; label: string }> = [
    { key: "prompt", label: t("runTracePrompt") },
    { key: "input", label: t("runTraceInput") },
    { key: "output", label: t("runTraceOutput") },
    { key: "change", label: t("flowFileChange") },
    { key: "validation", label: t("flowValidation") },
  ];
  return (
    <div
      className={[
        "flex flex-wrap gap-1.5",
        compact ? "mt-2" : "mt-3 rounded-md border border-brand-500/10 bg-ink-950/45 p-2.5",
      ].join(" ")}
      data-testid="replay-step-strip"
    >
      {steps.map((step) => {
        const present = Boolean(stepState[step.key]);
        return (
          <span
            key={step.key}
            className={[
              "inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[11px]",
              present
                ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-100"
                : "border-ink-700/70 bg-ink-950/40 text-ink-500",
            ].join(" ")}
          >
            {step.label}
            {!compact ? (
              <span className={present ? "text-emerald-100/70" : "text-ink-500/80"}>
                {present ? (recordedLabel || t("stepRecorded")) : (missingLabel || t("stepMissing"))}
              </span>
            ) : null}
          </span>
        );
      })}
    </div>
  );
}

function AgentFlowBubble({ entry, index }: { entry: AgentFlowEntry; index: number }) {
  const isAgentOutput = entry.role === "agent_output" || entry.role === "file_change";
  const decisionContext = entry.artifact ? parseDecisionContextArtifact(entry.artifact) : null;
  const runtimeFeedback = entry.artifact ? parseRuntimeFeedbackArtifact(entry.artifact) : null;
  const digestKind = entry.artifact ? replayDigestKind(entry.artifact) : null;
  return (
    <li className={["flex", isAgentOutput ? "justify-end" : "justify-start"].join(" ")}>
      <div
        className={[
          "min-w-0 max-w-full rounded-lg border px-3 py-2.5 text-sm",
          isAgentOutput ? "w-[92%] border-accent-500/20 bg-accent-500/[0.06]" : "w-[92%] border-brand-500/15 bg-ink-900/45",
        ].join(" ")}
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full border border-brand-500/15 bg-ink-950/60 px-1.5 font-mono text-[10px] text-ink-400">
            {index}
          </span>
          <Pill tone={flowTone(entry.role)}>{entry.label}</Pill>
          <span className="min-w-0 break-words font-medium text-ink-100">{entry.title}</span>
        </div>
        {entry.path ? (
          <div className="mt-1 break-all font-mono text-[11px] text-ink-500">{entry.path}</div>
        ) : null}
        {entry.badges?.length ? (
          <div className="mt-2">
            <TokenList values={entry.badges} empty="" />
          </div>
        ) : null}
        {decisionContext ? (
          <DecisionContextDigest context={decisionContext} raw={entry.detail} />
        ) : runtimeFeedback ? (
          <RuntimeFeedbackDigest feedback={runtimeFeedback} raw={entry.detail} />
        ) : digestKind === "subagent_payload" ? (
          <SubagentPayloadDigest raw={entry.detail} />
        ) : digestKind === "subagent_output" ? (
          <SubagentOutputDigest raw={entry.detail} />
        ) : digestKind === "validation_plan" ? (
          <ValidationPlanDigest raw={entry.detail} />
        ) : entry.role === "file_change" && entry.artifact ? (
          <ProposedFileDigest artifact={entry.artifact} path={entry.path} raw={entry.detail} />
        ) : entry.detail && isJsonLikePreview(entry.detail) ? (
          <GenericJsonDigest raw={entry.detail} />
        ) : entry.detail ? (
          <pre className="embedded-scroll mt-2 max-h-64 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
            {entry.detail}
          </pre>
        ) : null}
      </div>
    </li>
  );
}

function GenericJsonDigest({ raw }: { raw: string }) {
  const t = useTranslations("selfEvolution");
  const parsed = parseJsonPreviewObject(raw);
  const rows = genericJsonPreviewRows(raw, parsed);
  return (
    <div className="mt-2 space-y-2">
      <div className="rounded-md border border-brand-500/15 bg-ink-950/55 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium text-ink-500">{t("jsonPreviewSummary")}</span>
          <Pill tone={parsed ? "ok" : "warn"}>{parsed ? "parsed" : "preview"}</Pill>
        </div>
        <div className="mt-3">
          <ReplayDigestRows title={t("jsonPreviewFields")} rows={rows} empty={t("missing")} />
        </div>
      </div>
      <RawReplayDetails
        title={t("jsonPreviewRaw")}
        description={t("jsonPreviewRawDesc")}
        raw={raw}
        testId="json-preview-debug"
      />
    </div>
  );
}

function replayDigestKind(artifact: EvolutionProcessArtifact): ReplayDigestKind | null {
  if (artifact.id === "subagent_payload") return "subagent_payload";
  if (artifact.id === "subagent_output") return "subagent_output";
  if (artifact.id === "validation_plan") return "validation_plan";
  return null;
}

function ProposedFileDigest({
  artifact,
  path,
  raw,
}: {
  artifact: EvolutionProcessArtifact;
  path?: string;
  raw: string;
}) {
  const t = useTranslations("selfEvolution");
  const meta = artifact.metadata ?? {};
  const displayPath = path || displayPathForArtifact(artifact) || textValue(artifact.title);
  const rows = [
    ["kind", textValue(artifact.kind)] as const,
    ...(artifact.language ? [["language", String(artifact.language)] as const] : []),
    ...(typeof artifact.size === "number" ? [["size", formatBytes(artifact.size)] as const] : []),
    ...(meta.operation ? [["operation", String(meta.operation)] as const] : []),
    ...(meta.scope ? [["scope", String(meta.scope)] as const] : []),
  ].filter(([, value]) => value);

  return (
    <div className="mt-2 space-y-2">
      <div className="rounded-md border border-accent-500/15 bg-ink-950/55 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium text-ink-500">{t("fileChangeSummary")}</span>
          {artifact.truncated ? <Pill tone="warn">{t("truncated")}</Pill> : null}
          {artifact.redacted ? <Pill tone="neutral">redacted</Pill> : null}
        </div>
        {displayPath ? (
          <div className="mt-2 break-all font-mono text-[12px] text-ink-100">{displayPath}</div>
        ) : null}
        <div className="mt-3">
          <ReplayDigestRows title={t("fileMetadata")} rows={rows} empty={t("missing")} />
        </div>
      </div>
      <RawReplayDetails
        title={t("rawProposedFile")}
        description={t("rawProposedFileDesc")}
        raw={raw}
        testId="proposed-file-debug"
      />
    </div>
  );
}

function parseDecisionContextArtifact(artifact: EvolutionProcessArtifact): Record<string, unknown> | null {
  if (artifact.id !== "strategy_decision_context") return null;
  try {
    const parsed = JSON.parse(String(artifact.preview || "{}"));
    const context = objectRecord(parsed);
    return Object.keys(context).length ? context : null;
  } catch {
    return null;
  }
}

function parseRuntimeFeedbackArtifact(artifact: EvolutionProcessArtifact): Record<string, unknown> | null {
  if (artifact.id !== "runtime_feedback") return null;
  try {
    const parsed = JSON.parse(String(artifact.preview || "{}"));
    const feedback = objectRecord(parsed);
    return Object.keys(feedback).length ? feedback : null;
  } catch {
    return null;
  }
}

function SubagentPayloadDigest({ raw }: { raw: string }) {
  const t = useTranslations("selfEvolution");
  const payload = parseJsonPreviewObject(raw);
  const read = (key: string) => payload?.[key] ?? extractJsonPreviewField(raw, key);
  const objectives = stringList(read("objectives"));
  const allowedTargets = stringList(read("allowed_targets"));
  const forbiddenTargets = stringList(read("forbidden_targets"));
  const guardrails = objectRecord(read("guardrails"));
  const contract = objectRecord(read("materializable_output_contract"));
  const selectedAssets = objectRecord(read("selected_assets"));
  const genes = recordList(selectedAssets.genes);
  const capsules = recordList(selectedAssets.capsules);
  const contractVersion = textValue(contract.version);
  const runId = textValue(read("run_id"));
  const strategyId = textValue(read("strategy_id"));
  const inputRows = ["strategy_id", "market_regime", "prompt", "run_id"]
    .map((key) => {
      const value = read(key);
      return value === undefined || value === null || value === "" ? null : [key, formatCompactValue(value)] as const;
    })
    .filter((row): row is readonly [string, string] => Boolean(row));

  return (
    <div className="mt-2 space-y-2">
      <div className="rounded-md border border-brand-500/15 bg-ink-950/55 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium text-ink-500">{t("subagentPayloadSummary")}</span>
          {strategyId ? <Pill tone="brand">{strategyId}</Pill> : null}
          {runId ? <Pill tone="neutral">{shortId(runId)}</Pill> : null}
          {objectives.length ? <Pill tone="neutral">{t("objectiveCount", { count: objectives.length })}</Pill> : null}
          {contractVersion ? <Pill tone="neutral">{contractVersion}</Pill> : null}
        </div>
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          <ReplayDigestRows title={t("payloadFields")} rows={inputRows} empty={t("missing")} />
          <ReplayDigestList title={t("payloadObjectives")} values={objectives} empty={t("missing")} />
          <ReplayDigestRows
            title={t("payloadGuardrails")}
            rows={compactDecisionRows(
              guardrails,
              ["max_patch_files", "max_position_size_change_pct", "require_backtest", "require_shadow_run", "require_operator_approval"],
              6,
            )}
            empty={t("missing")}
          />
          <ReplayDigestList title={t("payloadAllowedTargets")} values={allowedTargets} empty={t("missing")} />
          <ReplayDigestList title={t("payloadForbiddenTargets")} values={forbiddenTargets} empty={t("missing")} tone="danger" />
          {(genes.length || capsules.length) ? (
            <ReplayDigestRows
              title={t("payloadSelectedAssets")}
              rows={[
                [t("selectedGenes"), String(genes.length)] as const,
                [t("selectedCapsules"), String(capsules.length)] as const,
              ]}
              empty={t("missing")}
            />
          ) : null}
          {contractVersion ? (
            <ReplayDigestRows
              title={t("payloadOutputContract")}
              rows={[
                ["version", contractVersion] as const,
                ["required", formatCompactValue(contract.required_for_applyable_changes)],
              ]}
              empty={t("missing")}
            />
          ) : null}
        </div>
      </div>
      <RawReplayDetails
        title={t("subagentPayloadDebug")}
        description={t("subagentPayloadDebugDesc")}
        raw={raw}
        testId="subagent-payload-debug"
      />
    </div>
  );
}

function SubagentOutputDigest({ raw }: { raw: string }) {
  const t = useTranslations("selfEvolution");
  const output = parseJsonPreviewObject(raw);
  const read = (key: string) => output?.[key] ?? extractJsonPreviewField(raw, key);
  const summary = compactLongText(textValue(read("summary")), 360);
  const evidence = recordList(read("evidence"));
  const changes = recordList(read("proposed_changes"));
  const riskFlags = stringList(read("risk_flags"));
  const expectedEffect = objectRecord(read("expected_effect"));
  const validationPlan = stringList(read("validation_plan"));
  const dataCoverage = objectRecord(read("data_coverage"));
  const toolsUsed = stringList(dataCoverage.tools_used);
  const toolErrors = stringList(dataCoverage.tool_errors);

  return (
    <div className="mt-2 space-y-2">
      <div className="rounded-md border border-accent-500/15 bg-ink-950/55 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium text-ink-500">{t("subagentOutputSummary")}</span>
          <Pill tone={changes.length ? "ok" : "neutral"}>{t("changeCount", { count: changes.length })}</Pill>
          <Pill tone={evidence.length ? "brand" : "neutral"}>{t("evidenceCount", { count: evidence.length })}</Pill>
          {riskFlags.length ? <Pill tone="danger">{t("riskFlagCount", { count: riskFlags.length })}</Pill> : null}
        </div>
        {summary ? <div className="mt-2 text-[12px] leading-relaxed text-ink-300">{summary}</div> : null}
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          <ReplayDigestList
            title={t("outputProposedChanges")}
            values={changes.slice(0, 4).map((change) => [textValue(change.file), textValue(change.kind)].filter(Boolean).join(" / "))}
            empty={t("missing")}
          />
          <ReplayDigestRows
            title={t("outputExpectedEffect")}
            rows={compactDecisionRows(expectedEffect, ["return", "drawdown", "risk", "latency"], 4)}
            empty={t("missing")}
          />
          <ReplayDigestList title={t("outputValidationPlan")} values={validationPlan.slice(0, 4)} empty={t("missing")} />
          <ReplayDigestList title={t("outputRiskFlags")} values={riskFlags} empty={t("none")} tone="danger" />
          {(toolsUsed.length || toolErrors.length) ? (
            <ReplayDigestRows
              title={t("outputDataCoverage")}
              rows={[
                ...(toolsUsed.length ? [["tools", toolsUsed.slice(0, 3).join(", ")] as const] : []),
                ...(toolErrors.length ? [["errors", String(toolErrors.length)] as const] : []),
              ]}
              empty={t("missing")}
            />
          ) : null}
        </div>
        {evidence.length ? (
          <div className="mt-3 space-y-2">
            <div className="text-[11px] font-medium text-ink-500">{t("outputEvidence")}</div>
            {evidence.slice(0, 3).map((item, index) => (
              <div key={`${textValue(item.source)}:${index}`} className="rounded border border-brand-500/10 bg-ink-900/45 p-2">
                <div className="flex flex-wrap items-center gap-1.5">
                  {item.source ? <Pill tone="neutral">{String(item.source)}</Pill> : null}
                </div>
                <div className="mt-1 text-[12px] leading-relaxed text-ink-300">
                  {compactLongText(textValue(item.finding || item.summary), 220)}
                </div>
              </div>
            ))}
          </div>
        ) : null}
      </div>
      <RawReplayDetails
        title={t("subagentOutputDebug")}
        description={t("subagentOutputDebugDesc")}
        raw={raw}
        testId="subagent-output-debug"
      />
    </div>
  );
}

function ValidationPlanDigest({ raw }: { raw: string }) {
  const t = useTranslations("selfEvolution");
  const plan = parseJsonPreviewObject(raw) ?? {};
  const steps = recordList(plan.steps);
  const blockedReasons = stringList(plan.blocked_reasons);
  const status = textValue(plan.status);
  const strategyId = textValue(plan.strategy_id);
  const safeToRun = typeof plan.safe_to_run === "boolean" ? plan.safe_to_run : null;
  const requiredCount = steps.filter((step) => Boolean(step.required)).length;
  const manualCount = steps.filter((step) => String(step.type || "").includes("manual")).length;
  const commandCount = steps.filter((step) => textValue(step.command)).length;

  return (
    <div className="mt-2 space-y-2">
      <div className="rounded-md border border-amber-300/20 bg-ink-950/55 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium text-ink-500">{t("validationPlanSummary")}</span>
          {status ? <Pill tone={toneForStatus(status)}>{status}</Pill> : null}
          {safeToRun !== null ? <Pill tone={safeToRun ? "ok" : "danger"}>{safeToRun ? t("safe") : t("blockedStatus")}</Pill> : null}
          {strategyId ? <Pill tone="neutral">{strategyId}</Pill> : null}
        </div>
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          <ReplayDigestRows
            title={t("validationStepSummary")}
            rows={[
              [t("steps"), String(steps.length)] as const,
              [t("required"), String(requiredCount)] as const,
              [t("manual"), String(manualCount)] as const,
              [t("commands"), String(commandCount)] as const,
            ]}
            empty={t("missing")}
          />
          <ReplayDigestList title={t("validationBlockedReasons")} values={blockedReasons} empty={t("none")} tone="danger" />
        </div>
        {steps.length ? (
          <div className="mt-3 space-y-2">
            <div className="text-[11px] font-medium text-ink-500">{t("validationSteps")}</div>
            {steps.slice(0, 5).map((step, index) => {
              const stepStatus = textValue(step.status || "not_run");
              const command = textValue(step.command);
              const summary = textValue(step.summary || step.description || step.type);
              return (
                <div key={`${textValue(step.type)}:${index}`} className="rounded border border-brand-500/10 bg-ink-900/45 p-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Pill tone={toneForStatus(stepStatus)}>{stepStatus}</Pill>
                    {step.type ? <Pill tone="neutral">{String(step.type)}</Pill> : null}
                    {step.required ? <Pill tone="warn">{t("required")}</Pill> : null}
                  </div>
                  {command ? (
                    <div className="mt-1 break-words font-mono text-[11px] text-ink-300">{command}</div>
                  ) : summary ? (
                    <div className="mt-1 text-[12px] text-ink-300">{summary}</div>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : null}
      </div>
      <RawReplayDetails
        title={t("validationPlanDebug")}
        description={t("validationPlanDebugDesc")}
        raw={raw}
        testId="validation-plan-debug"
      />
    </div>
  );
}

function DecisionContextDigest({
  context,
  raw,
}: {
  context: Record<string, unknown>;
  raw: string;
}) {
  const t = useTranslations("selfEvolution");
  const market = objectRecord(context.market_context);
  const marketItems = Array.isArray(market.items)
    ? market.items.map((row) => objectRecord(row)).filter((row) => Object.keys(row).length)
    : [];
  const firstMarket = marketItems[0] ?? {};
  const features = objectRecord(firstMarket.features);
  const trade = objectRecord(context.trade_metrics);
  const risk = objectRecord(context.risk_metrics);
  const news = objectRecord(context.news_context);
  const markets = stringList(market.markets).length
    ? stringList(market.markets)
    : stringList(firstMarket.market);
  const timeframe = String(market.timeframe || firstMarket.timeframe || "");
  const candlesCount = finiteNumber(firstMarket.candles_count);
  const newsItems = Array.isArray(news.items)
    ? news.items.map((row) => objectRecord(row)).filter((row) => Object.keys(row).length)
    : [];
  const newsSymbols = stringList(news.symbols);
  const newsCount = finiteNumber(news.count) ?? newsItems.length;

  return (
    <div className="mt-2 space-y-2">
      <div className="rounded-md border border-brand-500/15 bg-ink-950/55 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium text-ink-500">{t("decisionContextUsed")}</span>
          {markets.slice(0, 4).map((marketName) => <Pill key={marketName} tone="brand">{marketName}</Pill>)}
          {timeframe ? <Pill tone="neutral">{timeframe}</Pill> : null}
          {candlesCount !== null ? <Pill tone="neutral">{t("decisionCandles", { count: candlesCount })}</Pill> : null}
        </div>
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          <DecisionContextBlock
            title={t("decisionMarketContext")}
            rows={compactDecisionRows(features, ["atr_pct", "realized_volatility", "trend_strength", "adx", "return_pct"], 5)}
            empty={markets.length || timeframe ? "" : t("missing")}
          />
          <DecisionContextBlock
            title={t("decisionTradeHistory")}
            rows={compactDecisionRows(trade, ["pnl_total_usd", "max_drawdown_usd", "win_rate", "closed", "avg_slippage"], 5)}
            empty={t("missing")}
          />
          <DecisionContextBlock
            title={t("decisionRiskContext")}
            rows={compactDecisionRows(risk, ["risk_rejects", "risk_blocks", "decision_holds", "risk_rows"], 5)}
            empty={t("missing")}
          />
          <DecisionContextBlock
            title={t("decisionNewsContext")}
            rows={[
              ...(newsCount ? [["count", formatCompactValue(newsCount)] as const] : []),
              ...(newsSymbols.length ? [["symbols", newsSymbols.slice(0, 4).join(", ")] as const] : []),
              ...(newsItems[0]?.title ? [["latest", formatCompactValue(newsItems[0].title)] as const] : []),
            ]}
            empty={t("missing")}
          />
        </div>
      </div>
      {raw ? (
        <DebugDetails
          title={t("decisionRawContext")}
          description={t("decisionRawContextDesc")}
          testId="decision-context-debug"
        >
          <pre className="embedded-scroll max-h-64 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
            {raw}
          </pre>
        </DebugDetails>
      ) : null}
    </div>
  );
}

function ReplayDigestList({
  title,
  values,
  empty,
  tone = "neutral",
}: {
  title: string;
  values: string[];
  empty: string;
  tone?: "neutral" | "danger";
}) {
  const clean = uniqueStrings(values.map((value) => compactLongText(value, 80))).slice(0, 8);
  return (
    <div className="min-w-0 rounded border border-brand-500/10 bg-ink-950/45 p-2">
      <div className="text-[11px] font-medium text-ink-500">{title}</div>
      <div className="mt-1.5">
        <TokenList values={clean} empty={empty} tone={tone} />
      </div>
    </div>
  );
}

function ReplayDigestRows({
  title,
  rows,
  empty,
}: {
  title: string;
  rows: ReadonlyArray<readonly [string, string]>;
  empty: string;
}) {
  return <DecisionContextBlock title={title} rows={rows} empty={empty} />;
}

function RawReplayDetails({
  raw,
  title,
  description,
  testId,
}: {
  raw: string;
  title: string;
  description: string;
  testId: string;
}) {
  if (!raw) return null;
  return (
    <DebugDetails title={title} description={description} testId={testId}>
      <pre className="embedded-scroll max-h-64 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
        {raw}
      </pre>
    </DebugDetails>
  );
}

function RuntimeFeedbackDigest({
  feedback,
  raw,
}: {
  feedback: Record<string, unknown>;
  raw: string;
}) {
  const t = useTranslations("selfEvolution");
  const total = finiteNumber(feedback.post_apply_observation_count) ?? finiteNumber(feedback.recent_count);
  const recentCount = finiteNumber(feedback.recent_count);
  const negative = finiteNumber(feedback.negative_count);
  const healthy = finiteNumber(feedback.healthy_count);
  const observing = finiteNumber(feedback.observing_count);
  const lastObserved = String(feedback.last_observed_at || "");
  const evidenceRefs = stringList(feedback.evidence_refs);
  const recentObservations = Array.isArray(feedback.recent_observations)
    ? feedback.recent_observations.map((row) => objectRecord(row)).filter((row) => Object.keys(row).length)
    : [];
  const weighted = {
    ...feedback,
    count: total ?? recentCount ?? recentObservations.length,
  };

  return (
    <div className="mt-2 space-y-2">
      <div className="rounded-md border border-brand-500/15 bg-ink-950/55 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium text-ink-500">{t("runtimeFeedbackUsed")}</span>
          {total !== null ? <Pill tone="neutral">{t("runtimeFeedbackTotal", { count: total })}</Pill> : null}
          {recentCount !== null ? <Pill tone="brand">{t("runtimeFeedbackRecent", { count: recentCount })}</Pill> : null}
          {negative !== null ? <Pill tone={negative ? "danger" : "neutral"}>{t("runtimeFeedbackNegative", { count: negative })}</Pill> : null}
          {healthy !== null ? <Pill tone={healthy ? "ok" : "neutral"}>{t("runtimeFeedbackHealthy", { count: healthy })}</Pill> : null}
          {observing !== null ? <Pill tone="neutral">{t("runtimeFeedbackObserving", { count: observing })}</Pill> : null}
          {lastObserved ? <Pill tone="neutral">{formatTime(lastObserved)}</Pill> : null}
        </div>
        <GdiRuntimeEvidencePanel weighted={weighted} />
        {recentObservations.length ? (
          <div className="mt-3">
            <div className="mb-1.5 text-[11px] font-medium text-ink-500">{t("runtimeFeedbackRecentObservations")}</div>
            <div className="space-y-2">
              {recentObservations.slice(-3).reverse().map((observation, index) => (
                <RuntimeFeedbackObservation key={`${String(observation.id || observation.run_id || index)}:${index}`} observation={observation} />
              ))}
            </div>
          </div>
        ) : null}
        {evidenceRefs.length ? (
          <div className="mt-3">
            <div className="mb-1.5 text-[11px] font-medium text-ink-500">{t("runtimeFeedbackEvidence")}</div>
            <TokenList values={evidenceRefs.slice(0, 6)} empty="" />
          </div>
        ) : null}
      </div>
      {raw ? (
        <DebugDetails
          title={t("runtimeFeedbackDebug")}
          description={t("runtimeFeedbackDebugDesc")}
          testId="runtime-feedback-debug"
        >
          <pre className="embedded-scroll max-h-64 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
            {raw}
          </pre>
        </DebugDetails>
      ) : null}
    </div>
  );
}

function RuntimeFeedbackObservation({
  observation,
}: {
  observation: Record<string, unknown>;
}) {
  const status = String(observation.status || "");
  const source = String(observation.source || "");
  const runId = String(observation.run_id || "");
  const observedAt = String(observation.observed_at || "");
  const summary = String(observation.summary || "");
  const metrics = objectRecord(observation.metrics);
  const metricRows = compactDecisionRows(metrics, ["mode", "run_status", "result_status", "verdict", "total_return_pct", "max_drawdown_pct"], 5);
  return (
    <div className="rounded border border-brand-500/10 bg-ink-950/45 p-2">
      <div className="flex flex-wrap items-center gap-1.5">
        {status ? <Pill tone={toneForStatus(status)}>{status}</Pill> : null}
        {source ? <Pill tone="neutral">{source}</Pill> : null}
        {runId ? <span className="break-all font-mono text-[11px] text-ink-500">{runId}</span> : null}
        {observedAt ? <span className="font-mono text-[11px] text-ink-500">{formatTime(observedAt)}</span> : null}
      </div>
      {summary ? <div className="mt-1.5 text-[12px] text-ink-300">{summary}</div> : null}
      {metricRows.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {metricRows.map(([key, value]) => (
            <Pill key={key} tone="neutral">{key} {value}</Pill>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function DecisionContextBlock({
  title,
  rows,
  empty,
}: {
  title: string;
  rows: ReadonlyArray<readonly [string, string]>;
  empty: string;
}) {
  if (!rows.length) {
    return (
      <div className="min-w-0 rounded border border-brand-500/10 bg-ink-950/45 p-2">
        <div className="text-[11px] font-medium text-ink-500">{title}</div>
        {empty ? <div className="mt-1 text-[12px] text-ink-500">{empty}</div> : null}
      </div>
    );
  }
  return (
    <div className="min-w-0 rounded border border-brand-500/10 bg-ink-950/45 p-2">
      <div className="text-[11px] font-medium text-ink-500">{title}</div>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {rows.map(([key, value]) => (
          <Pill key={key} tone="neutral">{key} {value}</Pill>
        ))}
      </div>
    </div>
  );
}

function compactDecisionRows(
  source: Record<string, unknown>,
  preferred: string[],
  limit: number,
): Array<readonly [string, string]> {
  const rows: Array<readonly [string, string]> = [];
  for (const key of preferred) {
    const value = source[key];
    if (value === undefined || value === null || value === "") continue;
    rows.push([key, formatCompactValue(value)]);
  }
  if (rows.length >= limit) return rows.slice(0, limit);
  for (const [key, value] of Object.entries(source)) {
    if (rows.some(([existing]) => existing === key)) continue;
    if (value === undefined || value === null || value === "" || typeof value === "object") continue;
    rows.push([key, formatCompactValue(value)]);
    if (rows.length >= limit) break;
  }
  return rows;
}

function primaryReplayArtifactRefs(
  process?: EvolutionProcessTrace,
  fallbackSectionTitle = "",
): PrimaryReplayArtifactRef[] {
  const sectionArtifacts = (process?.sections ?? []).flatMap((section) =>
    (section.artifacts ?? []).map((artifact, index) => ({
      sectionId: section.id,
      sectionTitle: section.title,
      artifact,
      index,
    })),
  );
  const replaySectionArtifacts = sectionArtifacts.filter(({ sectionId, artifact }) =>
    isPrimaryReplayArtifact(sectionId, artifact),
  );
  if (replaySectionArtifacts.length) return replaySectionArtifacts;
  return (process?.artifacts ?? [])
    .filter((artifact) => isPrimaryReplayArtifact("artifacts", artifact))
    .map((artifact, index) => ({
      sectionId: "artifacts",
      sectionTitle: fallbackSectionTitle,
      artifact,
      index,
    }));
}

function firstReplayableTimelineId(timeline: EvolutionTimelineItem[]) {
  return timeline.find((item) => replaySnapshotForItem(item, "").canReplay)?.id ?? null;
}

function replaySnippetsForProcess(
  process: EvolutionProcessTrace | undefined,
  labels: Record<ReplaySnippet["kind"], string>,
): ReplaySnippet[] {
  const snippets: ReplaySnippet[] = [];
  const seen = new Set<ReplaySnippet["kind"]>();
  for (const { sectionId, artifact } of primaryReplayArtifactRefs(process)) {
    const kind = replaySnippetKind(sectionId, artifact);
    if (!kind || seen.has(kind)) continue;
    const preview = compactReplayPreview(String(artifact.preview || ""));
    if (!preview) continue;
    seen.add(kind);
    snippets.push({
      kind,
      label: labels[kind],
      title: artifact.title || labels[kind],
      preview,
    });
    if (seen.size === 3) break;
  }
  return snippets;
}

function replaySnippetKind(sectionId: string, artifact: EvolutionProcessArtifact): ReplaySnippet["kind"] | null {
  const kind = String(artifact.kind || "").toLowerCase();
  if (kind === "prompt") return "prompt";
  if (kind === "input" || sectionId === "inputs" || sectionId === "strategy_decision_context" || sectionId === "runtime_feedback") return "input";
  if (kind === "output" || sectionId === "subagent_output") return "output";
  return null;
}

function compactReplayPreview(text: string) {
  const cleaned = text.trim();
  if (!cleaned) return "";
  return cleaned.length > 520 ? `${cleaned.slice(0, 520).trimEnd()}...` : cleaned;
}

function replaySnapshotForItem(item: EvolutionTimelineItem, missingModel: string): ReplaySnapshot {
  const process = item.process;
  const artifactRefs = primaryReplayArtifactRefs(process);
  const run = process?.run ?? null;
  const provider = String(run?.provider || "");
  const model = String(run?.model || "");
  const modelDisplay = model ? `${provider ? `${provider}/` : ""}${model}` : missingModel;
  const stepState = replayStepStateForProcess(process);
  const hasRunMeta = Boolean(run?.model || run?.subagent || run?.model_calls?.length);
  const canReplay = Boolean(
    process
      && (hasRunMeta || artifactRefs.length || Object.values(stepState).some(Boolean)),
  );
  return {
    canReplay,
    model: modelDisplay,
    subagent: String(run?.subagent || ""),
    stepState,
  };
}

function replayStepStateForProcess(process?: EvolutionProcessTrace): Record<ReplayStepKey, boolean> {
  const artifactRefs = primaryReplayArtifactRefs(process);
  const roles = artifactRefs.map(({ sectionId, artifact }) => ({
    sectionId,
    artifact,
    role: flowRoleForArtifact(artifact, sectionId),
    kind: String(artifact.kind || "").toLowerCase(),
  }));
  const stepState: Record<ReplayStepKey, boolean> = {
    prompt: Boolean(
      process?.has_prompt
        || roles.some(({ kind }) => kind === "prompt"),
    ),
    input: Boolean(
      process?.has_inputs
        || roles.some(({ role, kind, sectionId }) =>
          role === "agent_input" && (kind === "input" || sectionId === "inputs" || sectionId === "strategy_decision_context"),
        ),
    ),
    output: Boolean(
      process?.has_outputs
        || roles.some(({ role }) => role === "agent_output"),
    ),
    change: Boolean(
      process?.has_file_changes
        || roles.some(({ role }) => role === "file_change"),
    ),
    validation: Boolean(
      process?.has_validation
        || roles.some(({ role }) => role === "validation"),
    ),
  };
  return stepState;
}

function flowOrder(role: AgentFlowRole) {
  if (role === "trigger") return 0;
  if (role === "agent_input") return 1;
  if (role === "agent_output") return 2;
  if (role === "file_change") return 3;
  if (role === "validation") return 4;
  return 5;
}

function isPrimaryReplayArtifact(sectionId: string, artifact: EvolutionProcessArtifact) {
  if (isProposedChangeArtifact(artifact)) return true;
  if (sectionId === "proposal_files" || sectionId === "generated_docs") return false;
  const scope = String(artifact.metadata?.scope || "").toLowerCase();
  if (scope === "proposal") return false;
  const kind = String(artifact.kind || "").toLowerCase();
  if (["prompt", "input", "output", "validation", "change"].includes(kind)) {
    return true;
  }
  return [
    "prompt_inputs",
    "inputs",
    "strategy_decision_context",
    "runtime_feedback",
    "subagent_output",
    "validation",
    "backtest_comparison",
  ].includes(sectionId);
}

function flowRoleForArtifact(artifact: EvolutionProcessArtifact, sectionId: string): AgentFlowRole {
  const kind = String(artifact.kind || "").toLowerCase();
  if (isProposedChangeArtifact(artifact)) return "file_change";
  if (kind === "prompt" || kind === "input" || sectionId === "prompt_inputs" || sectionId === "inputs") {
    return "agent_input";
  }
  if (kind === "output" || sectionId === "subagent_output") return "agent_output";
  if (kind === "validation" || sectionId === "validation") return "validation";
  if (kind === "proposal" || kind === "document" || sectionId === "proposal_files" || sectionId === "generated_docs") {
    return "file_change";
  }
  return "learning";
}

function flowLabel(
  role: AgentFlowRole,
  labels: AgentFlowLabels,
) {
  if (role === "trigger") return labels.trigger;
  if (role === "agent_input") return labels.agentInput;
  if (role === "agent_output") return labels.agentOutput;
  if (role === "file_change") return labels.fileChange;
  if (role === "validation") return labels.validation;
  return labels.learning;
}

function flowLabelForArtifact(
  role: AgentFlowRole,
  artifact: EvolutionProcessArtifact,
  sectionId: string,
  labels: AgentFlowLabels,
) {
  if (role === "agent_input") {
    const kind = String(artifact.kind || "").toLowerCase();
    if (kind === "prompt") return labels.prompt;
    if (kind === "input" || sectionId === "inputs" || sectionId === "strategy_decision_context" || sectionId === "runtime_feedback") return labels.input;
  }
  return flowLabel(role, labels);
}

function flowTitle(
  role: AgentFlowRole,
  artifact: EvolutionProcessArtifact,
  sectionTitle: string,
  path: string | undefined,
  labels: AgentFlowLabels,
) {
  if (role === "agent_input") {
    return artifact.title || (artifact.kind === "input" ? labels.inputTitle : labels.promptTitle);
  }
  if (role === "agent_output") return artifact.title || labels.outputTitle;
  if (role === "validation") return labels.validationTitle;
  if (role === "file_change") {
    if (path) return labels.proposedWrite(path);
    if (artifact.title === "SKILL.md") return labels.fileSkill;
    if (String(artifact.title || "").toLowerCase().endsWith(".md")) return labels.fileMarkdown;
    return labels.fileTitle;
  }
  return artifact.title || sectionTitle || labels.evidence;
}

function flowTone(role: AgentFlowRole): "neutral" | "ok" | "warn" | "danger" | "brand" {
  if (role === "agent_input" || role === "trigger") return "brand";
  if (role === "agent_output" || role === "file_change") return "ok";
  if (role === "validation") return "warn";
  return "neutral";
}

function isProposedChangeArtifact(artifact: EvolutionProcessArtifact) {
  const meta = artifact.metadata ?? {};
  if (String(meta.scope || "") === "after" || String(meta.operation || "") === "proposed_write") {
    return true;
  }
  const normalized = String(artifact.path || "").replace(/\\/g, "/");
  return /(^|\/)after\//.test(normalized);
}

function displayPathForArtifact(artifact: EvolutionProcessArtifact) {
  const meta = artifact.metadata ?? {};
  if (typeof meta.workspace_path === "string" && meta.workspace_path) {
    return meta.workspace_path;
  }
  if (typeof meta.proposal_path === "string" && meta.proposal_path) {
    return meta.proposal_path;
  }
  const raw = String(artifact.path || "");
  if (!raw) return undefined;
  const normalized = raw.replace(/\\/g, "/");
  const match = normalized.match(/(?:^|\/)after\/(.+)$/);
  if (match?.[1]) return match[1];
  return raw;
}

function EvidenceDrawer({
  state,
  onClose,
}: {
  state: EvidenceDrawerState | null;
  onClose: () => void;
}) {
  const t = useTranslations("selfEvolution");
  if (!state) return null;
  const item = state.item ?? null;
  const artifacts = item?.artifacts ?? [];
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-ink-950/60 backdrop-blur-sm">
      <button
        type="button"
        className="absolute inset-0 cursor-default"
        aria-label={t("closeEvidence")}
        onClick={onClose}
      />
      <aside className="relative z-10 flex h-full w-full max-w-2xl flex-col border-l border-brand-500/15 bg-ink-950 shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-brand-500/10 px-5 py-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Pill tone={item?.resolved ? "ok" : item ? "danger" : "brand"}>
                {item?.type || t("evidence")}
              </Pill>
              <span className="font-mono text-[11px] text-ink-500">{state.ref}</span>
            </div>
            <h3 className="mt-2 break-words text-lg font-semibold text-ink-50">
              {item?.title || t("evidenceDrawerTitle")}
            </h3>
            {item?.summary ? <p className="mt-1 text-sm text-ink-400">{item.summary}</p> : null}
          </div>
          <button
            type="button"
            className="btn btn-ghost px-2"
            aria-label={t("closeEvidence")}
            onClick={onClose}
          >
            <XIcon size={16} />
          </button>
        </div>
        <div className="embedded-scroll min-h-0 flex-1 space-y-4 px-5 py-4">
          {state.loading ? (
            <div className="rounded-lg border border-brand-500/10 bg-ink-900/45 p-4 text-sm text-ink-300">
              {t("evidenceLoading")}
            </div>
          ) : null}
          {state.error ? <ErrorBanner error={state.error} /> : null}
          {!state.loading && !state.error && !item ? (
            <div className="rounded-lg border border-danger/20 bg-danger/10 p-4 text-sm text-danger">
              {t("evidenceNoItem")}
            </div>
          ) : null}
          {item ? (
            <>
              <div className="grid gap-2 sm:grid-cols-2">
                <DetailMini label={t("evidenceRef")} value={item.ref} />
                <DetailMini label={t("evidenceType")} value={item.type || t("unknown")} />
                {item.path ? <DetailMini label={t("evidencePath")} value={item.path} /> : null}
                {!item.resolved ? (
                  <DetailMini label={t("evidenceReason")} value={item.reason || item.summary || t("unknown")} />
                ) : null}
              </div>
              <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-[11px] font-medium text-ink-500">{t("evidenceArtifacts")}</div>
                  <Pill tone={artifacts.length ? "brand" : "neutral"}>{artifacts.length}</Pill>
                </div>
                {artifacts.length ? (
                  <div className="mt-3 space-y-3">
                    {artifacts.map((artifact, index) => (
                      <EvidenceArtifactPreview
                        key={`${artifact.path ?? artifact.title}:${index}`}
                        artifact={artifact}
                      />
                    ))}
                  </div>
                ) : (
                  <div className="mt-3 text-sm text-ink-500">{t("noEvidenceArtifacts")}</div>
                )}
              </section>
              {item.record !== undefined ? (
                <DebugDetails
                  title={t("evidenceDebugRecord")}
                  description={t("evidenceDebugRecordDesc")}
                  testId="evidence-debug-record"
                >
                  <Json value={item.record} />
                </DebugDetails>
              ) : null}
            </>
          ) : null}
        </div>
      </aside>
    </div>
  );
}

function EvidenceArtifactPreview({ artifact }: { artifact: EvolutionEvidenceArtifact }) {
  const preview = String(artifact.preview || "");
  return (
    <div className="rounded-md border border-brand-500/15 bg-ink-900/50 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={artifactTone(artifact.kind)}>{artifact.kind || "artifact"}</Pill>
        <span className="break-words font-medium text-ink-100">{artifact.title}</span>
        {artifact.language ? <span className="font-mono text-[11px] text-ink-500">{artifact.language}</span> : null}
        {typeof artifact.size === "number" ? <span className="font-mono text-[11px] text-ink-500">{formatBytes(artifact.size)}</span> : null}
        {artifact.redacted ? <Pill tone="neutral">redacted</Pill> : null}
        {artifact.truncated ? <Pill tone="warn">truncated</Pill> : null}
      </div>
      {artifact.path ? (
        <div className="mt-1 break-all font-mono text-[11px] text-ink-500">{artifact.path}</div>
      ) : null}
      {preview ? (
        <pre className="embedded-scroll mt-2 max-h-72 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
          {preview}
        </pre>
      ) : null}
    </div>
  );
}

function artifactTone(kind?: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  const k = String(kind || "").toLowerCase();
  if (k === "prompt" || k === "input") return "brand";
  if (k === "validation" || k === "proposal") return "warn";
  if (k === "output" || k === "document" || k === "change") return "ok";
  return "neutral";
}

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatMetric(value: number) {
  if (!Number.isFinite(value)) return String(value);
  const abs = Math.abs(value);
  if (abs >= 100) return value.toFixed(1);
  if (abs >= 10) return value.toFixed(2);
  return value.toFixed(4);
}

function formatRunTokens(value: unknown, fallback: string) {
  const n = finiteNumber(value);
  if (n == null) return fallback;
  return Math.round(n).toLocaleString("en-US");
}

function formatRunCost(value: unknown, fallback: string) {
  const n = finiteNumber(value);
  if (n == null) return fallback;
  return `$${n.toFixed(n >= 0.01 ? 3 : 5)}`;
}

function formatDurationMs(value: unknown, fallback: string) {
  const n = finiteNumber(value);
  if (n == null) return fallback;
  if (n < 1000) return `${Math.round(n)}ms`;
  return `${(n / 1000).toFixed(1)}s`;
}

function formatScorePercent(value: number) {
  if (!Number.isFinite(value)) return "n/a";
  return `${Math.round(value * 100)}%`;
}

function formatWeight(value: number) {
  if (!Number.isFinite(value)) return "n/a";
  const abs = Math.abs(value);
  if (abs >= 100) return value.toFixed(1);
  return value.toFixed(2);
}

function finiteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function objectRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

function recordList(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => objectRecord(entry)).filter((entry) => Object.keys(entry).length);
}

function numericRecord(value: unknown): Record<string, number> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out: Record<string, number> = {};
  for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
    const numeric = finiteNumber(entry);
    if (numeric !== null) out[key] = numeric;
  }
  return out;
}

function stringList(value: unknown): string[] {
  if (typeof value === "string") return value.trim() ? [value.trim()] : [];
  if (!Array.isArray(value)) return [];
  return value
    .map((entry) => String(entry ?? "").trim())
    .filter(Boolean);
}

function uniqueStrings(values: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const value of values) {
    const text = String(value || "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
  }
  return out;
}

function textValue(value: unknown) {
  return String(value ?? "").trim();
}

function compactLongText(value: string, limit: number) {
  const text = textValue(value).replace(/\s+/g, " ");
  if (!text || text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 3)).trimEnd()}...`;
}

function parseJsonPreviewObject(text: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(text);
    const record = objectRecord(parsed);
    return Object.keys(record).length ? record : null;
  } catch {
    return null;
  }
}

function isJsonLikePreview(text: string) {
  const trimmed = text.trim();
  return trimmed.startsWith("{") || trimmed.startsWith("[");
}

function genericJsonPreviewRows(
  raw: string,
  parsed: Record<string, unknown> | null,
): Array<readonly [string, string]> {
  const preferred = [
    "strategy_id",
    "run_id",
    "status",
    "source",
    "created_at",
    "proposal_id",
    "validation_status",
    "outcome",
  ];
  const parsedRows = parsed ? compactDecisionRows(parsed, preferred, 8) : [];
  if (parsedRows.length) return parsedRows;

  const rows: Array<readonly [string, string]> = [];
  const seen = new Set<string>();
  const re = /"([A-Za-z0-9_]+)"\s*:\s*("(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?|true|false|null)/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(raw)) && rows.length < 8) {
    const key = match[1] || "";
    if (!key || seen.has(key)) continue;
    seen.add(key);
    try {
      rows.push([key, compactLongText(String(JSON.parse(match[2] || "\"\"")), 48)]);
    } catch {
      rows.push([key, compactLongText(match[2] || "", 48)]);
    }
  }
  return rows;
}

function extractJsonPreviewField(text: string, key: string): unknown {
  const match = new RegExp(`"${escapeRegExp(key)}"\\s*:`).exec(text);
  if (!match) return undefined;
  const snippet = jsonValueSnippet(text, match.index + match[0].length);
  if (!snippet) return undefined;
  try {
    return JSON.parse(snippet);
  } catch {
    return undefined;
  }
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function jsonValueSnippet(text: string, start: number): string | null {
  let i = start;
  while (i < text.length && /\s/.test(text[i] || "")) i += 1;
  const first = text[i];
  if (!first) return null;
  if (first === "\"") {
    let escaped = false;
    for (let j = i + 1; j < text.length; j += 1) {
      const ch = text[j];
      if (escaped) {
        escaped = false;
        continue;
      }
      if (ch === "\\") {
        escaped = true;
        continue;
      }
      if (ch === "\"") return text.slice(i, j + 1);
    }
    return null;
  }
  if (first === "{" || first === "[") {
    const stack: string[] = [];
    let inString = false;
    let escaped = false;
    for (let j = i; j < text.length; j += 1) {
      const ch = text[j];
      if (inString) {
        if (escaped) {
          escaped = false;
        } else if (ch === "\\") {
          escaped = true;
        } else if (ch === "\"") {
          inString = false;
        }
        continue;
      }
      if (ch === "\"") {
        inString = true;
        continue;
      }
      if (ch === "{") stack.push("}");
      if (ch === "[") stack.push("]");
      if (ch === "}" || ch === "]") {
        if (stack.pop() !== ch) return null;
        if (!stack.length) return text.slice(i, j + 1);
      }
    }
    return null;
  }
  let end = i;
  while (end < text.length && !",}]\n\r".includes(text[end] || "")) end += 1;
  const snippet = text.slice(i, end).trim();
  return snippet || null;
}

function compactContextValues(value: unknown): string[] {
  if (typeof value === "number" || typeof value === "boolean") {
    return [formatCompactValue(value)];
  }
  if (typeof value === "string" || Array.isArray(value)) {
    return stringList(value);
  }
  return [];
}

function triggerContextLabel(key: string, t: (key: string) => string) {
  const labels: Record<string, string> = {
    signal_kinds: t("triggerSignals"),
    selected_gene_ids: t("selectedGenes"),
    selected_capsule_ids: t("selectedCapsules"),
    market_regimes: t("marketRegimes"),
    markets: t("markets"),
    timeframes: t("timeframes"),
    data_quality: t("dataQuality"),
    evidence_refs: t("evidence"),
  };
  return labels[key] ?? key;
}

function scoreBarWidth(value: number) {
  if (!Number.isFinite(value)) return "0%";
  return `${Math.max(0, Math.min(100, Math.round(value * 100)))}%`;
}

function compactEntries(value?: Record<string, unknown>, limit = 6): Array<[string, unknown]> {
  if (!value || typeof value !== "object") return [];
  return Object.entries(value)
    .filter(([, entry]) => entry !== undefined && entry !== null && typeof entry !== "object")
    .slice(0, limit);
}

function compactBacktestEntries(value?: Record<string, unknown>): Array<[string, unknown]> {
  if (!value || typeof value !== "object") return [];
  const preferred = [
    "verdict",
    "coverage_ok",
    "total_return_pct",
    "max_drawdown_pct",
    "sharpe_ratio",
    "profit_factor",
    "total_trades",
  ];
  return preferred
    .filter((key) => value[key] !== undefined && value[key] !== null)
    .map((key) => [key, value[key]]);
}

function formatCompactValue(value: unknown) {
  if (typeof value === "number") return formatMetric(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  const text = String(value ?? "");
  return text.length > 36 ? `${text.slice(0, 33)}...` : text;
}

function RunResultSummary({
  proposalId,
  state,
  target,
  validationPlanId,
  validationStatus,
  blockedReasons,
  evidenceRefs,
  nextStep,
  fileChanges,
  backtestComparison,
  onEvidenceRef,
}: {
  proposalId?: string | null;
  state?: string | null;
  target?: string | null;
  validationPlanId?: string | null;
  validationStatus?: string | null;
  blockedReasons?: string[] | null;
  evidenceRefs?: string[] | null;
  nextStep?: string | null;
  fileChanges?: EvolutionProposalFileChange[];
  backtestComparison?: EvolutionBacktestComparison | null;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const changes = fileChanges ?? [];
  const blockers = blockedReasons ?? [];
  const evidence = evidenceRefs ?? [];
  const validationText = validationStatus || validationPlanId || "";
  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3" data-testid="run-result-summary">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] font-medium text-ink-500">{t("runResult")}</div>
          <div className="mt-1 text-[12px] text-ink-400">{t("runResultDesc")}</div>
        </div>
        {state ? <Pill tone={toneForStatus(state)}>{state}</Pill> : null}
      </div>
      <div className="mt-3 grid gap-2 md:grid-cols-2">
        <RunResultCell label={t("generatedProposal")}>
          {proposalId ? (
            <span className="font-mono text-ink-100">{proposalId}</span>
          ) : (
            <span className="text-ink-500">{t("noLinkedProposal")}</span>
          )}
        </RunResultCell>
        {target ? (
          <RunResultCell label={t("target")}>
            <span className="break-all font-mono text-ink-100">{target}</span>
          </RunResultCell>
        ) : null}
        <RunResultCell label={t("validationResult")}>
          {validationText ? (
            <div className="space-y-1">
              {validationStatus ? <Pill tone={toneForStatus(validationStatus)}>{validationStatus}</Pill> : null}
              {validationPlanId ? <div className="font-mono text-[12px] text-ink-300">{validationPlanId}</div> : null}
              <TokenList values={blockers} tone="danger" empty="" />
            </div>
          ) : (
            <span className="text-ink-500">{t("noValidationPlan")}</span>
          )}
        </RunResultCell>
        <RunResultCell label={t("evidence")}>
          <TokenList values={evidence} empty={t("noEvidence")} onSelect={onEvidenceRef} />
        </RunResultCell>
        {fileChanges ? (
          <RunResultCell label={t("changedFiles")}>
            {changes.length ? (
              <div className="space-y-1">
                <Pill tone="ok">{t("changedFilesCount", { count: changes.length })}</Pill>
                <div className="flex flex-wrap gap-1.5">
                  {changes.slice(0, 4).map((change) => (
                    <span key={change.path} className="break-all font-mono text-[11px] text-ink-400">
                      {change.path}
                    </span>
                  ))}
                </div>
              </div>
            ) : (
              <span className="text-ink-500">{t("noFileChanges")}</span>
            )}
          </RunResultCell>
        ) : null}
        {backtestComparison ? (
          <RunResultCell label={t("backtestComparison")}>
            <div className="space-y-1">
              <Pill tone={toneForStatus(backtestComparison.status)}>
                {backtestComparison.status || t("unknown")}
              </Pill>
              {backtestComparison.summary ? (
                <div className="text-[12px] text-ink-400">{backtestComparison.summary}</div>
              ) : null}
            </div>
          </RunResultCell>
        ) : null}
        {nextStep ? (
          <RunResultCell label={t("nextStep")}>
            <span className="text-ink-300">{nextStep}</span>
          </RunResultCell>
        ) : null}
      </div>
    </section>
  );
}

function RunResultCell({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="min-w-0 rounded-md border border-brand-500/10 bg-ink-950/45 px-2.5 py-2">
      <div className="text-[11px] font-medium text-ink-500">{label}</div>
      <div className="mt-1 min-w-0 text-[12px] text-ink-300">{children}</div>
    </div>
  );
}

function SupportingEvidenceDetails({
  count,
  children,
}: {
  count: number;
  children: ReactNode;
}) {
  const t = useTranslations("selfEvolution");
  return (
    <details className="border-t border-brand-500/10 pt-3" data-testid="supporting-evidence-details">
      <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 text-[12px] text-ink-300 marker:hidden">
        <span className="font-medium">{t("supportingEvidence")}</span>
        <span className="flex flex-wrap items-center gap-2">
          <span className="text-ink-500">{t("supportingEvidenceDesc")}</span>
          <Pill tone="neutral">{count}</Pill>
        </span>
      </summary>
      <div className="mt-3 space-y-3">{children}</div>
    </details>
  );
}

function SupportingEvidenceStack({
  lineageGraph,
  optimizerReport,
  actionGates,
  fitnessVector,
  whyReused,
  postApplyMonitor,
  assetOutcome,
  onEvidenceRef,
}: {
  lineageGraph?: EvolutionLineageGraph | null;
  optimizerReport?: EvolutionOptimizerReport | null;
  actionGates?: EvolutionActionGates | null;
  fitnessVector?: EvolutionFitnessVector | null;
  whyReused?: EvolutionWhyReused | null;
  postApplyMonitor?: EvolutionPostApplyMonitor | null;
  assetOutcome?: ReactNode;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const count = [
    lineageGraph,
    optimizerReport,
    actionGates,
    fitnessVector,
    whyReused,
    postApplyMonitor,
    assetOutcome,
  ].filter(Boolean).length;
  if (!count) return null;
  const whyCount = [lineageGraph, whyReused, assetOutcome].filter(Boolean).length;
  const selectionCount = [optimizerReport].filter(Boolean).length;
  const qualityCount = [actionGates, fitnessVector, postApplyMonitor].filter(Boolean).length;
  return (
    <SupportingEvidenceDetails count={count}>
      <SupportingEvidenceGroup
        title={t("supportingWhyChanged")}
        description={t("supportingWhyChangedDesc")}
        count={whyCount}
      >
        <LineageGraphPanel graph={lineageGraph} onEvidenceRef={onEvidenceRef} />
        <WhyReusedPanel why={whyReused} onEvidenceRef={onEvidenceRef} />
        {assetOutcome}
      </SupportingEvidenceGroup>
      <SupportingEvidenceGroup
        title={t("supportingCandidateChoice")}
        description={t("supportingCandidateChoiceDesc")}
        count={selectionCount}
      >
        <CandidateOptimizerPanel report={optimizerReport} onEvidenceRef={onEvidenceRef} />
      </SupportingEvidenceGroup>
      <SupportingEvidenceGroup
        title={t("supportingQualityGates")}
        description={t("supportingQualityGatesDesc")}
        count={qualityCount}
      >
        <ActionGatesPanel gates={actionGates} onEvidenceRef={onEvidenceRef} />
        {fitnessVector ? (
          <FitnessVectorPanel vector={fitnessVector} onEvidenceRef={onEvidenceRef} />
        ) : null}
        {postApplyMonitor ? (
          <PostApplyHistoryPanel monitor={postApplyMonitor} onEvidenceRef={onEvidenceRef} />
        ) : null}
      </SupportingEvidenceGroup>
    </SupportingEvidenceDetails>
  );
}

function SupportingEvidenceGroup({
  title,
  description,
  count,
  children,
}: {
  title: string;
  description: string;
  count: number;
  children: ReactNode;
}) {
  if (!count) return null;
  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/25 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] font-medium text-ink-500">{title}</div>
          <div className="mt-1 text-[12px] text-ink-400">{description}</div>
        </div>
        <Pill tone="neutral">{count}</Pill>
      </div>
      <div className="mt-3 space-y-3">{children}</div>
    </section>
  );
}

function DebugDetails({
  children,
  title,
  description,
  testId = "debug-details",
}: {
  children: ReactNode;
  title?: string;
  description?: string;
  testId?: string;
}) {
  const t = useTranslations("selfEvolution");
  const label = title || t("debugDetails");
  const desc = description || t("debugDetailsDesc");
  return (
    <details className="rounded-lg border border-ink-700/50 bg-ink-950/20 p-3" data-testid={testId}>
      <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 text-[12px] text-ink-400 marker:hidden">
        <span className="font-medium">{label}</span>
        <span className="text-ink-500">{desc}</span>
      </summary>
      <div className="mt-3">{children}</div>
    </details>
  );
}

function TokenList({
  values,
  empty,
  tone = "neutral",
  onSelect,
}: {
  values: string[];
  empty: string;
  tone?: "neutral" | "danger";
  onSelect?: (value: string) => void;
}) {
  if (!values.length) {
    return empty ? <span className="text-ink-500">{empty}</span> : null;
  }
  const color = tone === "danger" ? "border-danger/25 text-danger" : "border-brand-500/10 text-ink-300";
  const interactive = Boolean(onSelect);
  return (
    <div className="flex min-w-0 max-w-full flex-wrap gap-1.5">
      {values.map((value) =>
        interactive ? (
          <button
            key={value}
            type="button"
            className={`max-w-full rounded border bg-ink-950/50 px-2 py-1 text-left font-mono text-[11px] break-all transition hover:border-brand-300/60 hover:text-brand-100 ${color}`}
            onClick={() => onSelect?.(value)}
          >
            {value}
          </button>
        ) : (
          <span key={value} className={`max-w-full rounded border bg-ink-950/50 px-2 py-1 font-mono text-[11px] break-all ${color}`}>
            {value}
          </span>
        ),
      )}
    </div>
  );
}

function LineageGraphPanel({
  graph,
  onEvidenceRef,
}: {
  graph?: EvolutionLineageGraph | null;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  if (!graph || !Array.isArray(graph.nodes) || !graph.nodes.length) return null;
  const nodes = graph.nodes;
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const groups = lineageGraphGroups(t)
    .map((group) => ({
      ...group,
      nodes: nodes.filter((node) => group.types.includes(node.type)),
    }))
    .filter((group) => group.nodes.length);
  const leftover = nodes.filter((node) => !groups.some((group) => group.nodes.includes(node)));
  if (leftover.length) {
    groups.push({ id: "other", title: t("lineageGraphOther"), types: [], nodes: leftover });
  }
  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] font-medium text-ink-500">{t("lineageGraph")}</div>
          <div className="mt-1 text-[12px] text-ink-400">{t("lineageGraphDesc")}</div>
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          <Pill tone="brand">{t("lineageNodeCount", { count: nodes.length })}</Pill>
          <Pill tone="neutral">{t("lineageEdgeCount", { count: edges.length })}</Pill>
          {graph.truncated ? <Pill tone="warn">{t("lineageTruncated")}</Pill> : null}
        </div>
      </div>

      <details
        className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/25 p-2.5"
        data-testid="lineage-graph-details"
      >
        <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 text-[12px] text-ink-300 marker:hidden">
          <span className="font-medium">{t("lineageGraphDetails")}</span>
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-ink-500">{t("lineageGraphDetailsDesc")}</span>
            <Pill tone="neutral">{groups.length}</Pill>
          </span>
        </summary>
        <div className="mt-3 grid gap-2 xl:grid-cols-5">
          {groups.map((group) => (
            <div key={group.id} className="min-w-0 rounded-md border border-brand-500/10 bg-ink-950/35 p-2.5">
              <div className="mb-2 text-[11px] font-medium text-ink-500">{group.title}</div>
              <div className="space-y-2">
                {group.nodes.slice(0, 8).map((node) => (
                  <LineageNodeCard key={node.id} node={node} onEvidenceRef={onEvidenceRef} />
                ))}
                {group.nodes.length > 8 ? (
                  <div className="text-[11px] text-ink-500">+{group.nodes.length - 8}</div>
                ) : null}
              </div>
            </div>
          ))}
        </div>

        {edges.length ? (
          <div className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/35 p-2.5">
            <div className="mb-2 text-[11px] font-medium text-ink-500">{t("lineageConnections")}</div>
            <div className="grid gap-1.5 md:grid-cols-2">
              {edges.slice(0, 12).map((edge) => {
                const source = nodeById.get(edge.source);
                const target = nodeById.get(edge.target);
                return (
                  <div key={edge.id} className="min-w-0 rounded border border-white/5 bg-ink-950/40 px-2 py-1.5 text-[12px] text-ink-400">
                    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                      <span className="min-w-0 break-words text-ink-300">{source?.label || edge.source}</span>
                      <span className="text-ink-600">-&gt;</span>
                      <span className="min-w-0 break-words text-ink-300">{target?.label || edge.target}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <Pill tone={toneForStatus(edge.status || edge.type)}>{edge.label || edge.type}</Pill>
                      {edge.evidence_refs?.length ? (
                        <button
                          type="button"
                          className="font-mono text-[11px] text-brand-300 hover:text-brand-100"
                          onClick={() => onEvidenceRef(edge.evidence_refs?.[0] || "")}
                        >
                          {t("lineageEvidence", { count: edge.evidence_refs.length })}
                        </button>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ) : null}

        {graph.warnings?.length ? (
          <div className="mt-3">
            <TokenList values={graph.warnings} empty="" tone="danger" />
          </div>
        ) : null}
      </details>
    </section>
  );
}

function LineageNodeCard({
  node,
  onEvidenceRef,
}: {
  node: EvolutionLineageGraph["nodes"][number];
  onEvidenceRef: (ref: string) => void;
}) {
  return (
    <div className="min-w-0 rounded border border-white/5 bg-ink-950/40 px-2 py-1.5">
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <Pill tone={toneForStatus(node.status || node.type)}>{node.type}</Pill>
        <span className="min-w-0 break-words text-[12px] font-medium text-ink-100">{node.label}</span>
      </div>
      {node.summary ? (
        <div className="mt-1 line-clamp-2 text-[11px] text-ink-500">{node.summary}</div>
      ) : null}
      {node.evidence_refs?.length ? (
        <div className="mt-2">
          <TokenList values={node.evidence_refs.slice(0, 2)} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </div>
  );
}

function lineageGraphGroups(t: (key: string) => string) {
  return [
    { id: "trigger", title: t("lineageTrigger"), types: ["signal", "event"] },
    { id: "reuse", title: t("lineageReuse"), types: ["gene", "capsule", "negative_capsule"] },
    { id: "change", title: t("lineageDiff"), types: ["proposal", "file_change", "action_gates"] },
    { id: "validation", title: t("lineageValidation"), types: ["validation_plan", "validation_run", "validation_step", "backtest_comparison"] },
    { id: "outcome", title: t("lineageOutcome"), types: ["approval", "apply", "rollback", "rejection", "post_apply_monitor", "post_apply_observation"] },
  ];
}

function CandidateOptimizerPanel({
  report,
  onEvidenceRef,
}: {
  report?: EvolutionOptimizerReport | null;
  onEvidenceRef?: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  if (!report || !Array.isArray(report.candidates) || !report.candidates.length) return null;
  const candidates = report.candidates.slice(0, 8);
  const selectedId = String(report.selected_candidate_id || "");
  const selectedIndex = finiteNumber(report.selected_index);
  const feedback = report.outcome_feedback ?? {};
  const feedbackSamples = finiteNumber(feedback.sample_count);
  const selected = candidates.find((candidate, index) => (
    String(candidate.candidate_id || "") === selectedId
    || (selectedIndex !== null && index === selectedIndex)
  ));
  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3" data-testid="candidate-optimizer-panel">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] font-medium text-ink-500">{t("candidateOptimizer")}</div>
          <div className="mt-1 text-[12px] text-ink-400">
            {report.selection_reason || t("candidateOptimizerDesc")}
          </div>
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          <Pill tone="brand">{t("optimizerCandidates", { count: Number(report.candidate_count ?? candidates.length) })}</Pill>
          <Pill tone="neutral">{t("optimizerEvaluated", { count: Number(report.evaluated_count ?? candidates.length) })}</Pill>
          {feedbackSamples !== null && feedbackSamples > 0 ? (
            <Pill tone="ok">{t("optimizerFeedbackSamples", { count: feedbackSamples })}</Pill>
          ) : null}
          {report.truncated ? <Pill tone="warn">{t("optimizerTruncated")}</Pill> : null}
        </div>
      </div>

      <CandidateValidationPreviewSummary preview={report.validation_preview} />
      <CandidateBacktestPreviewSummary preview={report.backtest_preview} />

      {selected ? (
        <div className="mt-3 rounded-md border border-accent-500/20 bg-accent-500/[0.06] p-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone="ok">{t("optimizerSelected")}</Pill>
            <span className="break-all font-mono text-[12px] text-ink-100">
              {selected.candidate_id || t("unknown")}
            </span>
            {typeof report.selected_score === "number" ? (
              <Pill tone="neutral">{t("optimizerScore", { score: formatCompactValue(report.selected_score) })}</Pill>
            ) : null}
          </div>
          {selected.summary ? <div className="mt-1.5 text-[12px] text-ink-300">{selected.summary}</div> : null}
        </div>
      ) : null}

      <details
        className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/25 p-2.5"
        data-testid="candidate-selection-details"
      >
        <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 text-[12px] text-ink-300 marker:hidden">
          <span className="font-medium">{t("optimizerSelectionDetails")}</span>
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-ink-500">{t("optimizerSelectionDetailsDesc")}</span>
            <Pill tone="neutral">{candidates.length}</Pill>
          </span>
        </summary>
        <div className="mt-3 grid gap-2">
          {candidates.map((candidate, index) => (
            <CandidateOptimizerRow
              key={`${candidate.candidate_id ?? index}:${index}`}
              candidate={candidate}
              selected={
                String(candidate.candidate_id || "") === selectedId
                || (selectedIndex !== null && index === selectedIndex)
              }
              onEvidenceRef={onEvidenceRef}
            />
          ))}
        </div>
      </details>
    </section>
  );
}

function CandidateOptimizerRow({
  candidate,
  selected,
  onEvidenceRef,
}: {
  candidate: EvolutionOptimizerCandidate;
  selected: boolean;
  onEvidenceRef?: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const status = String(candidate.status || "unknown");
  const counts = [
    ["accepted", candidate.accepted_count],
    ["materialized", candidate.materialized_count],
    ["dropped", candidate.dropped_count],
    ["unmaterialized", candidate.unmaterialized_count],
  ].filter(([, value]) => typeof value === "number" && Number.isFinite(value));
  const validationTypes = stringList(candidate.validation_types);
  const blocked = stringList(candidate.blocked_reasons);
  const risks = stringList(candidate.risk_flags);
  const reasons = stringList(candidate.reasons);
  const files = stringList(candidate.materialized_files);
  const warnings = stringList(candidate.warnings);
  const feedback = candidate.outcome_feedback ?? {};
  const feedbackDelta = finiteNumber(feedback.score_delta);
  const matchedFeedback = Array.isArray(feedback.matched_features)
    ? feedback.matched_features
    : [];
  const assetCandidate = objectRecord(candidate.asset_candidate);
  const assetCandidateId = String(assetCandidate.id || "");
  const assetCandidateRefs = stringList(assetCandidate.evidence_refs);
  const assetCandidateGates = objectRecord(assetCandidate.promotion_gates);
  const assetCandidateState = String(assetCandidate.state || "");
  return (
    <div
      className={[
        "rounded-md border p-2.5",
        selected ? "border-accent-500/25 bg-accent-500/[0.04]" : "border-brand-500/10 bg-ink-950/35",
      ].join(" ")}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={selected ? "ok" : toneForStatus(status)}>
          {selected ? t("optimizerSelected") : status}
        </Pill>
        <span className="break-all font-mono text-[12px] text-ink-100">
          {candidate.candidate_id || `candidate_${Number(candidate.index ?? 0) + 1}`}
        </span>
        {typeof candidate.score === "number" ? (
          <Pill tone="neutral">{t("optimizerScore", { score: formatCompactValue(candidate.score) })}</Pill>
        ) : null}
        {candidate.validation_status ? (
          <Pill tone={toneForStatus(candidate.validation_status)}>{candidate.validation_status}</Pill>
        ) : null}
        {feedbackDelta !== null && feedbackDelta !== 0 ? (
          <Pill tone={feedbackDelta > 0 ? "ok" : "danger"}>
            {t("optimizerFeedbackDelta", { delta: formatCompactValue(feedbackDelta) })}
          </Pill>
        ) : null}
        {assetCandidateId ? (
          <Pill tone={assetCandidate.safe_to_promote === false ? "warn" : "ok"}>
            {t("optimizerAssetCandidate")}
          </Pill>
        ) : null}
      </div>
      {candidate.summary ? <div className="mt-1.5 text-[12px] text-ink-300">{candidate.summary}</div> : null}
      {assetCandidateId ? (
        <div className="mt-2 rounded-md border border-accent-500/15 bg-accent-500/[0.05] p-2">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <span className="text-[11px] font-medium text-ink-500">{t("optimizerAssetCandidate")}</span>
            <span className="break-all font-mono text-[11px] text-ink-200">{assetCandidateId}</span>
            {assetCandidate.preview_type ? <Pill tone="neutral">{String(assetCandidate.preview_type)}</Pill> : null}
            {assetCandidate.preview_status ? (
              <Pill tone={toneForStatus(String(assetCandidate.preview_status))}>{String(assetCandidate.preview_status)}</Pill>
            ) : null}
            {assetCandidateState ? (
              <Pill tone={toneForStatus(assetCandidateState)}>{assetCandidateState}</Pill>
            ) : null}
            {assetCandidateGates.selector_eligible === false ? (
              <Pill tone="neutral">{t("candidateNotSelectorEligible")}</Pill>
            ) : null}
          </div>
          {assetCandidate.promoted_ref || assetCandidate.rejected_reason ? (
            <div className="mt-1 break-all font-mono text-[11px] text-ink-500">
              {assetCandidate.promoted_ref
                ? `${t("promotedRef")}: ${String(assetCandidate.promoted_ref)}`
                : `${t("rejectedReason")}: ${String(assetCandidate.rejected_reason)}`}
            </div>
          ) : null}
          {assetCandidateRefs.length ? (
            <div className="mt-1.5">
              <TokenList values={assetCandidateRefs.slice(0, 4)} empty="" onSelect={onEvidenceRef} />
            </div>
          ) : null}
        </div>
      ) : null}
      {counts.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {counts.map(([key, value]) => (
            <Pill key={key} tone={key === "dropped" || key === "unmaterialized" ? "warn" : "neutral"}>
              {t(`optimizerCount.${key}`, { count: Number(value) })}
            </Pill>
          ))}
        </div>
      ) : null}
      <div className="mt-2 grid gap-2 md:grid-cols-2">
        {validationTypes.length ? (
          <OptimizerTokenGroup label={t("optimizerValidation")} tokens={validationTypes} />
        ) : null}
        {files.length ? (
          <OptimizerTokenGroup label={t("optimizerFiles")} tokens={files.slice(0, 6)} />
        ) : null}
        {blocked.length ? (
          <OptimizerTokenGroup label={t("blockers")} tokens={blocked.slice(0, 6)} danger />
        ) : null}
        {risks.length ? (
          <OptimizerTokenGroup label={t("optimizerRiskFlags")} tokens={risks.slice(0, 6)} danger />
        ) : null}
        {warnings.length ? (
          <OptimizerTokenGroup label={t("warnings")} tokens={warnings.slice(0, 6)} />
        ) : null}
        {reasons.length ? (
          <OptimizerTokenGroup label={t("optimizerReasons")} tokens={reasons.slice(0, 8)} />
        ) : null}
        {feedbackDelta !== null && matchedFeedback.length ? (
          <OptimizerFeedbackCalibrationSummary feedback={feedback} />
        ) : null}
        {matchedFeedback.length ? (
          <OptimizerFeedbackGroup rows={matchedFeedback.slice(0, 6)} onEvidenceRef={onEvidenceRef} />
        ) : null}
        {candidate.validation_preview ? (
          <CandidateValidationPreviewGroup
            preview={candidate.validation_preview}
            onEvidenceRef={onEvidenceRef}
          />
        ) : null}
        {candidate.backtest_preview ? (
          <CandidateBacktestPreviewGroup
            preview={candidate.backtest_preview}
            onEvidenceRef={onEvidenceRef}
          />
        ) : null}
      </div>
    </div>
  );
}

function CandidateValidationPreviewSummary({
  preview,
}: {
  preview?: EvolutionOptimizerReport["validation_preview"];
}) {
  const t = useTranslations("selfEvolution");
  if (!preview || typeof preview !== "object") return null;
  const previewed = finiteNumber(preview.previewed_count);
  const passed = finiteNumber(preview.passed_count);
  const failed = finiteNumber(preview.failed_count);
  const executed = stringList(preview.executed_step_types);
  if (!previewed && !passed && !failed && !executed.length) return null;
  return (
    <div className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
      <div className="mb-1.5 text-[11px] font-medium text-ink-500">{t("optimizerValidationPreview")}</div>
      <div className="flex flex-wrap gap-1.5">
        {previewed !== null ? <Pill tone="brand">{t("optimizerPreviewed", { count: previewed })}</Pill> : null}
        {passed !== null ? <Pill tone="ok">{t("optimizerPreviewPassed", { count: passed })}</Pill> : null}
        {failed !== null ? <Pill tone={failed > 0 ? "danger" : "neutral"}>{t("optimizerPreviewFailed", { count: failed })}</Pill> : null}
      </div>
      {executed.length ? (
        <div className="mt-2">
          <TokenList values={executed} empty="" />
        </div>
      ) : null}
    </div>
  );
}

function CandidateBacktestPreviewSummary({
  preview,
}: {
  preview?: EvolutionOptimizerReport["backtest_preview"];
}) {
  const t = useTranslations("selfEvolution");
  if (!preview || typeof preview !== "object") return null;
  const previewed = finiteNumber(preview.previewed_count);
  const passed = finiteNumber(preview.passed_count);
  const failed = finiteNumber(preview.failed_count);
  const noData = finiteNumber(preview.no_data_count);
  if (!previewed && !passed && !failed && !noData) return null;
  return (
    <div className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
      <div className="mb-1.5 text-[11px] font-medium text-ink-500">{t("optimizerBacktestPreview")}</div>
      <div className="flex flex-wrap gap-1.5">
        {previewed !== null ? <Pill tone="brand">{t("optimizerPreviewed", { count: previewed })}</Pill> : null}
        {passed !== null ? <Pill tone="ok">{t("optimizerPreviewPassed", { count: passed })}</Pill> : null}
        {failed !== null ? <Pill tone={failed > 0 ? "danger" : "neutral"}>{t("optimizerPreviewFailed", { count: failed })}</Pill> : null}
        {noData !== null && noData > 0 ? <Pill tone="warn">{t("optimizerPreviewNoData", { count: noData })}</Pill> : null}
      </div>
    </div>
  );
}

function CandidateValidationPreviewGroup({
  preview,
  onEvidenceRef,
}: {
  preview: EvolutionCandidateValidationPreview;
  onEvidenceRef?: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const status = String(preview.status || "unknown");
  const delta = finiteNumber(preview.score_delta);
  const executed = stringList(preview.executed_step_types);
  const deferred = stringList(preview.deferred_step_types);
  const blockers = stringList(preview.blocked_reasons);
  const refs = stringList(preview.evidence_refs);
  const validation = preview.validation ?? {};
  const validationBlockers = Array.isArray(validation.blockers) ? validation.blockers : [];
  const blockerCodes = validationBlockers
    .map((row) => String(row.code || row.message || "").trim())
    .filter(Boolean)
    .slice(0, 6);
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] font-medium text-ink-500">{t("optimizerValidationPreview")}</span>
        <Pill tone={toneForStatus(status)}>{status}</Pill>
        {delta !== null && delta !== 0 ? (
          <Pill tone={delta > 0 ? "ok" : "danger"}>
            {t("optimizerPreviewDelta", { delta: formatCompactValue(delta) })}
          </Pill>
        ) : null}
      </div>
      {preview.reason ? <div className="text-[12px] text-ink-500">{preview.reason}</div> : null}
      <div className="mt-2 grid gap-2">
        {executed.length ? <OptimizerTokenGroup label={t("optimizerPreviewExecuted")} tokens={executed} /> : null}
        {deferred.length ? <OptimizerTokenGroup label={t("optimizerPreviewDeferred")} tokens={deferred} /> : null}
        {blockers.length ? <OptimizerTokenGroup label={t("blockers")} tokens={blockers} danger /> : null}
        {blockerCodes.length ? <OptimizerTokenGroup label={t("optimizerPreviewValidation")} tokens={blockerCodes} danger /> : null}
      </div>
      {refs.length ? (
        <div className="mt-2">
          <TokenList values={refs} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </div>
  );
}

function OptimizerTokenGroup({
  label,
  tokens,
  danger = false,
}: {
  label: string;
  tokens: string[];
  danger?: boolean;
}) {
  if (!tokens.length) return null;
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
      <div className="mb-1.5 text-[11px] font-medium text-ink-500">{label}</div>
      <TokenList values={tokens} empty="" tone={danger ? "danger" : "neutral"} />
    </div>
  );
}

function OptimizerFeedbackCalibrationSummary({
  feedback,
}: {
  feedback: NonNullable<EvolutionOptimizerCandidate["outcome_feedback"]>;
}) {
  const t = useTranslations("selfEvolution");
  const rawDelta = finiteNumber(feedback.raw_score_delta);
  const appliedDelta = finiteNumber(feedback.score_delta);
  const scale = finiteNumber(feedback.calibration_scale);
  const status = String(feedback.calibration_status || "");
  const confidence = String(feedback.calibration_confidence || "");
  const warnings = stringList(feedback.calibration_warnings);
  if (rawDelta === null && scale === null && !status && !confidence && !warnings.length) {
    return null;
  }
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2" data-testid="optimizer-feedback-calibration">
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] font-medium text-ink-500">{t("optimizerFeedbackCalibration")}</span>
        {status ? <Pill tone={toneForStatus(status)}>{status}</Pill> : null}
        {confidence ? <Pill tone={toneForOptimizerConfidence(confidence)}>{t("optimizerCalibrationConfidence", { value: confidence })}</Pill> : null}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {rawDelta !== null ? (
          <Pill tone={rawDelta < 0 ? "danger" : "neutral"}>
            {t("optimizerFeedbackRawDelta", { delta: formatCompactValue(rawDelta) })}
          </Pill>
        ) : null}
        {appliedDelta !== null ? (
          <Pill tone={appliedDelta < 0 ? "danger" : appliedDelta > 0 ? "ok" : "neutral"}>
            {t("optimizerFeedbackAppliedDelta", { delta: formatCompactValue(appliedDelta) })}
          </Pill>
        ) : null}
        {scale !== null ? (
          <Pill tone={scale >= 0.75 ? "ok" : scale >= 0.4 ? "warn" : "danger"}>
            {t("optimizerFeedbackScale", { scale: formatCompactValue(scale) })}
          </Pill>
        ) : null}
      </div>
      {warnings.length ? (
        <div className="mt-2">
          <TokenList values={warnings.slice(0, 5)} empty="" tone="danger" />
        </div>
      ) : null}
    </div>
  );
}

function OptimizerFeedbackGroup({
  rows,
  onEvidenceRef,
}: {
  rows: NonNullable<NonNullable<EvolutionOptimizerCandidate["outcome_feedback"]>["matched_features"]>;
  onEvidenceRef?: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  if (!rows.length) return null;
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
      <div className="mb-1.5 text-[11px] font-medium text-ink-500">{t("optimizerOutcomeFeedback")}</div>
      <div className="space-y-1.5">
        {rows.map((row, index) => {
          const net = finiteNumber(row.net);
          const feature = String(row.feature || t("unknown"));
          const sources = Object.entries(objectRecord(row.sources))
            .map(([source, count]) => [source, finiteNumber(count)] as const)
            .filter(([, count]) => count !== null && count > 0);
          const examples = Array.isArray(row.examples)
            ? row.examples.filter((example): example is Record<string, unknown> => (
              example !== null && typeof example === "object" && !Array.isArray(example)
            )).slice(0, 2)
            : [];
          return (
            <div key={`${feature}:${index}`} className="min-w-0 rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
              <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                <Pill tone={net !== null && net < 0 ? "danger" : "ok"}>
                  {net !== null ? formatCompactValue(net) : t("unknown")}
                </Pill>
                <span className="min-w-0 break-all font-mono text-[11px] text-ink-300">{feature}</span>
              </div>
              {sources.length ? (
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {sources.map(([source, count]) => (
                    <Pill key={source} tone={source === "asset_candidate_decision" ? "brand" : "neutral"}>
                      {source === "asset_candidate_decision"
                        ? t("optimizerFeedbackSourceDecision")
                        : source === "proposal_outcome"
                          ? t("optimizerFeedbackSourceProposal")
                          : source || t("unknown")} {count}
                    </Pill>
                  ))}
                </div>
              ) : null}
              {examples.length ? (
                <div className="mt-2 space-y-1.5">
                  {examples.map((example, exampleIndex) => (
                    <OptimizerFeedbackExample
                      key={`${feature}:example:${exampleIndex}`}
                      example={example}
                      onEvidenceRef={onEvidenceRef}
                    />
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function OptimizerFeedbackExample({
  example,
  onEvidenceRef,
}: {
  example: Record<string, unknown>;
  onEvidenceRef?: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const source = String(example.source || "");
  const assetCandidateId = String(example.asset_candidate_id || "");
  const proposalId = String(example.proposal_id || "");
  const candidateId = String(example.candidate_id || "");
  const decision = String(example.decision || example.state || "");
  const policy = String(example.feedback_policy || "");
  const score = finiteNumber(example.feedback_score);
  const weighting = objectRecord(example.feedback_weighting);
  const decayWeight = finiteNumber(weighting.decay_weight);
  const halfLifeDays = finiteNumber(weighting.half_life_days);
  const featureSourceCap = finiteNumber(weighting.feature_source_cap);
  const refs = stringList(example.evidence_refs);
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-900/35 p-2">
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span className="text-[11px] font-medium text-ink-500">
          {source === "asset_candidate_decision"
            ? t("optimizerDecisionSample")
            : t("optimizerProposalSample")}
        </span>
        {assetCandidateId ? (
          <span className="break-all font-mono text-[11px] text-ink-200">{assetCandidateId}</span>
        ) : null}
        {!assetCandidateId && proposalId ? (
          <span className="break-all font-mono text-[11px] text-ink-200">{proposalId}</span>
        ) : null}
        {candidateId ? <Pill tone="neutral">{candidateId}</Pill> : null}
        {decision ? <Pill tone={toneForStatus(decision)}>{decision}</Pill> : null}
        {score !== null && score !== 0 ? (
          <Pill tone={score > 0 ? "ok" : "danger"}>{formatCompactValue(score)}</Pill>
        ) : null}
        {decayWeight !== null ? (
          <Pill tone={decayWeight < 0.5 ? "warn" : "neutral"}>
            {t("optimizerFeedbackDecay", { weight: formatCompactValue(decayWeight) })}
          </Pill>
        ) : null}
      </div>
      {policy ? (
        <div className="mt-1 break-all font-mono text-[11px] text-ink-500">
          {t("optimizerFeedbackPolicy")}: {policy}
        </div>
      ) : null}
      {halfLifeDays !== null || featureSourceCap !== null ? (
        <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-ink-500">
          {halfLifeDays !== null ? <span>{t("optimizerFeedbackHalfLife", { days: formatCompactValue(halfLifeDays) })}</span> : null}
          {featureSourceCap !== null ? <span>{t("optimizerFeedbackCap", { cap: formatCompactValue(featureSourceCap) })}</span> : null}
        </div>
      ) : null}
      {refs.length ? (
        <div className="mt-1.5">
          <TokenList values={refs.slice(0, 3)} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </div>
  );
}

function ActionGatesPanel({
  gates,
  onEvidenceRef,
}: {
  gates?: EvolutionActionGates | null;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  if (!gates) return null;
  const blockers = gates.blockers ?? [];
  const warnings = gates.warnings ?? [];
  const materialization = gates.materialization ?? {};
  const evidence = gates.evidence ?? {};
  const validation = gates.validation ?? {};
  const afterCount = finiteNumber(materialization.after_file_count);
  const evidenceCount = finiteNumber(evidence.count);
  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] font-medium text-ink-500">{t("actionGates")}</div>
          <div className="mt-1 text-[12px] text-ink-400">
            {gates.can_apply ? t("actionGatesReady") : t("actionGatesBlocked")}
          </div>
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          <Pill tone={gates.can_apply ? "ok" : "danger"}>
            {gates.can_apply ? t("canApply") : t("cannotApply")}
          </Pill>
          {gates.state ? <Pill tone={toneForStatus(gates.state)}>{gates.state}</Pill> : null}
        </div>
      </div>

      {blockers.length || warnings.length ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {blockers.length ? (
            <div className="rounded-md border border-danger/15 bg-danger/5 p-2">
              <div className="mb-2 text-[11px] font-medium text-danger">{t("blockers")}</div>
              <TokenList values={blockers} tone="danger" empty="" />
            </div>
          ) : null}
          {warnings.length ? (
            <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
              <div className="mb-2 text-[11px] font-medium text-ink-500">{t("warnings")}</div>
              <TokenList values={warnings} empty="" />
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="mt-3 grid gap-2 md:grid-cols-3">
        <GateMini
          label={t("gateMaterialization")}
          ok={!materialization.required || (!materialization.advisory_only && Number(afterCount ?? 0) > 0)}
          value={t("afterFilesCount", { count: Number(afterCount ?? 0) })}
          tokens={stringList(materialization.paths).slice(0, 4)}
        />
        <GateMini
          label={t("gateEvidence")}
          ok={!evidence.required || Number(evidenceCount ?? 0) > 0}
          value={t("evidenceRefsCount", { count: Number(evidenceCount ?? 0) })}
          tokens={stringList(evidence.refs).slice(0, 4)}
          onToken={onEvidenceRef}
        />
        <GateMini
          label={t("gateValidation")}
          ok={Boolean(validation.ok)}
          value={String(validation.status || validation.reason || validation.source || t("missing"))}
          tokens={stringList(validation.evidence_refs).slice(0, 4)}
          onToken={onEvidenceRef}
        />
      </div>
      {validation.reason ? (
        <div className="mt-2 text-[12px] text-ink-500">{validation.reason}</div>
      ) : null}
    </section>
  );
}

function GateMini({
  label,
  ok,
  value,
  tokens,
  onToken,
}: {
  label: string;
  ok: boolean;
  value: string;
  tokens?: string[];
  onToken?: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium text-ink-500">{label}</span>
        <Pill tone={ok ? "ok" : "warn"}>{ok ? t("gateOk") : t("gateBlocked")}</Pill>
      </div>
      <div className="mt-1.5 text-[12px] text-ink-300">{value}</div>
      {tokens?.length ? (
        <div className="mt-2">
          <TokenList values={tokens} empty="" onSelect={onToken} />
        </div>
      ) : null}
    </div>
  );
}

function FitnessVectorPanel({
  vector,
  onEvidenceRef,
}: {
  vector: EvolutionFitnessVector;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const dimensions = Array.isArray(vector.dimensions) ? vector.dimensions : [];
  const blockers = vector.blockers ?? [];
  const warnings = vector.warnings ?? [];
  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] font-medium text-ink-500">{t("fitnessVector")}</div>
          {vector.summary ? <div className="mt-1 text-[12px] text-ink-400">{vector.summary}</div> : null}
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          <Pill tone={toneForStatus(vector.status)}>{vector.status}</Pill>
          {typeof vector.ready_for_approval === "boolean" ? (
            <Pill tone={vector.ready_for_approval ? "ok" : "warn"}>
              {vector.ready_for_approval ? t("approvalReady") : t("approvalNotReady")}
            </Pill>
          ) : null}
        </div>
      </div>

      {blockers.length || warnings.length ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {blockers.length ? (
            <div className="rounded-md border border-danger/15 bg-danger/5 p-2">
              <div className="mb-2 text-[11px] font-medium text-danger">{t("blockers")}</div>
              <TokenList values={blockers} tone="danger" empty="" />
            </div>
          ) : null}
          {warnings.length ? (
            <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
              <div className="mb-2 text-[11px] font-medium text-ink-500">{t("warnings")}</div>
              <TokenList values={warnings} empty="" />
            </div>
          ) : null}
        </div>
      ) : null}

      {dimensions.length ? (
        <div className="mt-3 grid gap-2">
          {dimensions.map((dimension) => (
            <div key={dimension.id} className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-ink-100">{dimension.label || dimension.id}</span>
                <Pill tone={toneForStatus(dimension.status)}>{dimension.status}</Pill>
                {typeof dimension.score === "number" ? (
                  <Pill tone="neutral">{t("score", { score: formatScorePercent(dimension.score) })}</Pill>
                ) : null}
              </div>
              {dimension.summary ? <div className="mt-1.5 text-[12px] text-ink-400">{dimension.summary}</div> : null}
              {dimension.blockers?.length ? (
                <div className="mt-2">
                  <TokenList values={dimension.blockers} tone="danger" empty="" />
                </div>
              ) : null}
              {dimension.warnings?.length ? (
                <div className="mt-2">
                  <TokenList values={dimension.warnings} empty="" />
                </div>
              ) : null}
              {dimension.evidence_refs?.length ? (
                <div className="mt-2">
                  <TokenList values={dimension.evidence_refs} empty="" onSelect={onEvidenceRef} />
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : (
        <div className="mt-3 text-sm text-ink-500">{t("noFitnessDimensions")}</div>
      )}

      {vector.evidence_refs?.length ? (
        <div className="mt-3">
          <div className="mb-2 text-[11px] font-medium text-ink-500">{t("fitnessEvidence")}</div>
          <TokenList values={vector.evidence_refs} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </section>
  );
}

function PostApplyHistoryPanel({
  monitor,
  onEvidenceRef,
}: {
  monitor: EvolutionPostApplyMonitor;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const observations = Array.isArray(monitor.observations) ? monitor.observations : [];
  const rows = observations.length ? [...observations].reverse() : [];
  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] font-medium text-ink-500">{t("postApplyHistory")}</div>
          {monitor.summary ? <div className="mt-1 text-[12px] text-ink-400">{monitor.summary}</div> : null}
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          <Pill tone={toneForStatus(monitor.status)}>{monitor.status}</Pill>
          <Pill tone={rows.length ? "brand" : "neutral"}>
            {t("observationsCount", { count: rows.length })}
          </Pill>
        </div>
      </div>
      {monitor.observed_at ? (
        <div className="mt-2 font-mono text-[11px] text-ink-500">
          {formatTime(monitor.observed_at)}
        </div>
      ) : null}
      <GdiRuntimeEvidencePanel weighted={monitor.weighted_summary} />
      {rows.length ? (
        <div className="mt-3 space-y-2">
          {rows.map((observation, index) => (
            <PostApplyObservationRow
              key={`${observation.id ?? observation.journal_ref ?? index}:${index}`}
              observation={observation}
              onEvidenceRef={onEvidenceRef}
            />
          ))}
        </div>
      ) : (
        <div className="mt-3 text-sm text-ink-500">{t("noPostApplyObservations")}</div>
      )}
      {monitor.evidence_refs?.length ? (
        <div className="mt-3">
          <div className="mb-2 text-[11px] font-medium text-ink-500">{t("postApplyEvidence")}</div>
          <TokenList values={monitor.evidence_refs} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </section>
  );
}

function PostApplyObservationRow({
  observation,
  onEvidenceRef,
}: {
  observation: EvolutionPostApplyObservation;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const metrics = compactEntries(observation.metrics, 6);
  const backtest = compactBacktestEntries(observation.backtest_result);
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={toneForStatus(observation.status)}>{observation.status || t("unknown")}</Pill>
        {observation.source ? <Pill tone="brand">{observation.source}</Pill> : null}
        {observation.run_id ? <Pill tone="neutral">{observation.run_id}</Pill> : null}
        <span className="ml-auto font-mono text-[11px] text-ink-500">
          {formatTime(observation.observed_at)}
        </span>
      </div>
      {observation.summary ? <div className="mt-2 text-[12px] text-ink-300">{observation.summary}</div> : null}
      {backtest.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {backtest.map(([key, value]) => (
            <Pill key={key} tone="neutral">{key}: {formatCompactValue(value)}</Pill>
          ))}
        </div>
      ) : null}
      {metrics.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {metrics.map(([key, value]) => (
            <Pill key={key} tone="neutral">{key}: {formatCompactValue(value)}</Pill>
          ))}
        </div>
      ) : null}
      {observation.evidence_refs?.length ? (
        <div className="mt-2">
          <TokenList values={observation.evidence_refs} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </div>
  );
}

function WhyReusedPanel({
  why,
  onEvidenceRef,
}: {
  why?: EvolutionWhyReused | null;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  if (!why) return null;
  const counts = why.counts ?? {};
  const genes = Array.isArray(why.genes) ? why.genes : [];
  const capsules = Array.isArray(why.capsules) ? why.capsules : [];
  const negativeCapsules = Array.isArray(why.negative_capsules) ? why.negative_capsules : [];
  const signals = Array.isArray(why.selection_signals) ? why.selection_signals : [];
  const diff = why.proposal_diff ?? {};
  const validation = why.validation ?? null;
  const postApply = why.post_apply ?? null;
  const contextRows = Object.entries(why.trigger_context ?? {})
    .map(([key, value]) => [key, compactContextValues(value)] as const)
    .filter(([, values]) => values.length)
    .slice(0, 6);
  const countPills = [
    ["genes", counts.genes],
    ["capsules", counts.capsules],
    ["negative_capsules", counts.negative_capsules],
    ["selection_signals", counts.selection_signals],
  ].filter(([, value]) => typeof value === "number" && Number.isFinite(value));

  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] font-medium text-ink-500">{t("whyReused")}</div>
          {why.summary ? <div className="mt-1 text-[12px] text-ink-400">{why.summary}</div> : null}
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          {countPills.map(([key, value]) => (
            <Pill key={key} tone={key === "negative_capsules" ? "danger" : "brand"}>
              {t(`whyReusedCount.${key}`, { count: Number(value) })}
            </Pill>
          ))}
        </div>
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-3">
        <WhyReusedConnectionCard
          title={t("proposalDiff")}
          status={diff.advisory_only ? t("advisoryOnly") : diff.materialized ? t("materialized") : t("missing")}
          tone={diff.advisory_only ? "warn" : diff.materialized ? "ok" : "neutral"}
          summary={t("changedFilesCount", { count: Number(diff.change_count ?? 0) })}
          tokens={stringList(diff.paths)}
        />
        <WhyReusedConnectionCard
          title={t("validation")}
          status={validation?.status || validation?.backtest_status || t("missing")}
          tone={toneForStatus(validation?.status || validation?.backtest_status)}
          summary={validation?.summary || validation?.backtest_summary || ""}
          tokens={validation?.evidence_refs ?? []}
          onEvidenceRef={onEvidenceRef}
        />
        <WhyReusedConnectionCard
          title={t("postApplyHistory")}
          status={postApply?.status || t("missing")}
          tone={toneForStatus(postApply?.status)}
          summary={postApply?.summary || (
            typeof postApply?.observation_count === "number"
              ? t("observationsCount", { count: postApply.observation_count })
              : ""
          )}
          tokens={postApply?.evidence_refs ?? []}
          onEvidenceRef={onEvidenceRef}
        />
      </div>

      {contextRows.length ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {contextRows.map(([key, values]) => (
            <div key={key} className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2.5">
              <div className="mb-1 text-[11px] font-medium text-ink-500">{triggerContextLabel(key, t)}</div>
              <TokenList values={values} empty="" />
            </div>
          ))}
        </div>
      ) : null}

      {signals.length ? (
        <div className="mt-3">
          <div className="mb-2 text-[11px] font-medium text-ink-500">{t("selectionSignals")}</div>
          <div className="grid gap-2">
            {signals.slice(0, 4).map((signal, index) => (
              <WhyReusedSignalRow
                key={`${signal.id ?? signal.kind ?? index}:${index}`}
                signal={signal}
                onEvidenceRef={onEvidenceRef}
              />
            ))}
          </div>
        </div>
      ) : null}

      {genes.length || capsules.length || negativeCapsules.length ? (
        <div className="mt-3 grid gap-2 lg:grid-cols-2">
          {[...genes, ...capsules].slice(0, 6).map((asset, index) => (
            <WhyReusedAssetRow
              key={`${asset.kind ?? "asset"}:${asset.id ?? index}:${index}`}
              asset={asset}
              onEvidenceRef={onEvidenceRef}
            />
          ))}
          {negativeCapsules.slice(0, 3).map((asset, index) => (
            <WhyReusedAssetRow
              key={`negative:${asset.id ?? index}:${index}`}
              asset={asset}
              onEvidenceRef={onEvidenceRef}
              caution
            />
          ))}
        </div>
      ) : null}

      {why.evidence_refs?.length ? (
        <div className="mt-3">
          <div className="mb-2 text-[11px] font-medium text-ink-500">{t("reuseEvidence")}</div>
          <TokenList values={why.evidence_refs} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </section>
  );
}

function WhyReusedConnectionCard({
  title,
  status,
  tone,
  summary,
  tokens,
  onEvidenceRef,
}: {
  title: string;
  status: string;
  tone: "neutral" | "ok" | "warn" | "danger" | "brand";
  summary?: string;
  tokens?: string[];
  onEvidenceRef?: (ref: string) => void;
}) {
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium text-ink-500">{title}</span>
        <Pill tone={tone}>{status}</Pill>
      </div>
      {summary ? <div className="mt-1.5 text-[12px] text-ink-400">{summary}</div> : null}
      {tokens?.length ? (
        <div className="mt-2">
          <TokenList values={tokens.slice(0, 4)} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </div>
  );
}

function WhyReusedSignalRow({
  signal,
  onEvidenceRef,
}: {
  signal: EvolutionWhyReusedSignal;
  onEvidenceRef: (ref: string) => void;
}) {
  const confidence = finiteNumber(signal.confidence);
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        {signal.kind ? <Pill tone="brand">{signal.kind}</Pill> : null}
        {signal.severity ? <Pill tone={toneForStatus(signal.severity)}>{signal.severity}</Pill> : null}
        {confidence !== null ? <Pill tone="neutral">{formatScorePercent(confidence)}</Pill> : null}
      </div>
      {signal.summary ? <div className="mt-1.5 text-[12px] text-ink-300">{signal.summary}</div> : null}
      {signal.evidence_refs?.length ? (
        <div className="mt-2">
          <TokenList values={signal.evidence_refs} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </div>
  );
}

function WhyReusedAssetRow({
  asset,
  onEvidenceRef,
  caution = false,
}: {
  asset: EvolutionWhyReusedAsset;
  onEvidenceRef: (ref: string) => void;
  caution?: boolean;
}) {
  const t = useTranslations("selfEvolution");
  const gdiScore = finiteNumber(asset.gdi_score);
  const relevanceScore = finiteNumber(asset.relevance_score);
  const matchedContextRows = Object.entries(asset.matched_context ?? {})
    .map(([key, value]) => [key, compactContextValues(value)] as const)
    .filter(([, values]) => values.length)
    .slice(0, 2);
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={caution || asset.polarity === "negative" ? "danger" : "ok"}>
          {asset.kind || t("asset")}
        </Pill>
        {asset.id ? <span className="font-mono text-[11px] text-ink-500">{asset.id}</span> : null}
        {gdiScore !== null ? <Pill tone="brand">{t("gdiScore", { score: formatScorePercent(gdiScore) })}</Pill> : null}
        {relevanceScore !== null ? (
          <Pill tone={relevanceScore >= 0.65 ? "ok" : relevanceScore >= 0.4 ? "neutral" : "warn"}>
            {t("relevanceScore", { score: formatScorePercent(relevanceScore) })}
          </Pill>
        ) : null}
      </div>
      {asset.summary ? <div className="mt-1.5 text-[12px] text-ink-300">{asset.summary}</div> : null}
      {asset.rationale ? <div className="mt-1 text-[12px] text-ink-500">{asset.rationale}</div> : null}
      {asset.matched_signals?.length ? (
        <div className="mt-2">
          <div className="mb-1 text-[11px] font-medium text-ink-500">{t("matchedSignals")}</div>
          <TokenList values={asset.matched_signals} empty="" />
        </div>
      ) : null}
      {matchedContextRows.length ? (
        <div className="mt-2 grid gap-2 md:grid-cols-2">
          {matchedContextRows.map(([key, values]) => (
            <div key={key} className="rounded border border-white/5 bg-ink-950/40 p-2">
              <div className="mb-1 text-[11px] font-medium text-ink-500">{triggerContextLabel(key, t)}</div>
              <TokenList values={values} empty="" />
            </div>
          ))}
        </div>
      ) : null}
      {asset.evidence_refs?.length ? (
        <div className="mt-2">
          <TokenList values={asset.evidence_refs} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </div>
  );
}

function GdiMiniPanel({ gdi }: { gdi: EvolutionGdiBreakdown }) {
  const t = useTranslations("selfEvolution");
  const labels: Record<string, string> = {
    intrinsic: t("intrinsic"),
    usage: t("usage"),
    human: t("human"),
    freshness: t("freshness"),
    relevance: t("relevance"),
  };
  const components = Object.entries(gdi.components ?? {}).filter(([, value]) =>
    typeof value === "number" && Number.isFinite(value),
  );
  return (
    <div className="mt-2 rounded-md border border-brand-500/10 bg-ink-950/35 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium text-ink-500">{t("gdi")}</span>
        {typeof gdi.score === "number" ? <Pill tone="brand">{formatScorePercent(gdi.score)}</Pill> : null}
        {gdi.polarity ? (
          <Pill tone={gdi.polarity === "negative" ? "danger" : "ok"}>{gdi.polarity}</Pill>
        ) : null}
        {typeof gdi.usage_count === "number" ? (
          <Pill tone="neutral">{t("usageCount", { count: gdi.usage_count })}</Pill>
        ) : null}
        {gdi.post_apply_status ? (
          <Pill tone={toneForStatus(gdi.post_apply_status)}>
            {t("postApplyStatus", { status: gdi.post_apply_status })}
          </Pill>
        ) : null}
      </div>
      {gdi.rationale ? <div className="mt-2 text-[12px] text-ink-400">{gdi.rationale}</div> : null}
      {components.length || gdi.matched_signals?.length || gdi.relevance || gdi.post_apply_weighted ? (
        <details className="mt-3 border-t border-brand-500/10 pt-3" data-testid="gdi-details">
          <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 text-[12px] text-ink-300 marker:hidden">
            <span className="font-medium">{t("gdiDetails")}</span>
            <span className="text-ink-500">{t("gdiDetailsDesc")}</span>
          </summary>
          <div className="mt-3 space-y-3">
            {components.length ? (
              <div className="grid gap-2 md:grid-cols-2">
                {components.map(([key, value]) => (
                  <ScoreBar key={key} label={labels[key] ?? key} value={value} />
                ))}
              </div>
            ) : null}
            {gdi.matched_signals?.length ? (
              <div>
                <div className="mb-1 text-[11px] font-medium text-ink-500">{t("matchedSignals")}</div>
                <TokenList values={gdi.matched_signals} empty="" />
              </div>
            ) : null}
            <GdiTriggerRelevancePanel relevance={gdi.relevance} />
            <GdiRuntimeEvidencePanel weighted={gdi.post_apply_weighted} />
          </div>
        </details>
      ) : null}
    </div>
  );
}

function GdiTriggerRelevancePanel({
  relevance,
}: {
  relevance?: EvolutionGdiBreakdown["relevance"];
}) {
  const t = useTranslations("selfEvolution");
  if (!relevance) return null;
  const score = finiteNumber(relevance.score);
  const matchedSignals = stringList(relevance.matched_signals);
  const triggerSignals = stringList(relevance.trigger_signal_kinds);
  const source = typeof relevance.source === "string" ? relevance.source : "";
  const geneId = typeof relevance.gene_id === "string" ? relevance.gene_id : "";
  const contextRows = Object.entries(relevance.matched_context ?? {})
    .map(([key, values]) => [key, stringList(values)] as const)
    .filter(([, values]) => values.length)
    .slice(0, 4);

  if (
    score === null
    && !matchedSignals.length
    && !triggerSignals.length
    && !source
    && !geneId
    && !contextRows.length
  ) {
    return null;
  }

  return (
    <div className="mt-3 border-t border-brand-500/10 pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium text-ink-500">{t("triggerRelevance")}</span>
        {score !== null ? <Pill tone={score >= 0.65 ? "ok" : score >= 0.4 ? "neutral" : "warn"}>{formatScorePercent(score)}</Pill> : null}
        {source ? <Pill tone="neutral">{t("relevanceSource", { source })}</Pill> : null}
        {geneId ? <Pill tone="brand">{geneId}</Pill> : null}
      </div>
      {matchedSignals.length ? (
        <div className="mt-2">
          <div className="mb-1 text-[11px] font-medium text-ink-500">{t("matchedSignals")}</div>
          <TokenList values={matchedSignals} empty="" />
        </div>
      ) : null}
      {contextRows.length ? (
        <div className="mt-2 grid gap-2 md:grid-cols-2">
          {contextRows.map(([key, values]) => (
            <div key={key} className="rounded border border-white/5 bg-ink-950/40 p-2">
              <div className="mb-1 text-[11px] font-medium text-ink-500">{triggerContextLabel(key, t)}</div>
              <TokenList values={values} empty="" />
            </div>
          ))}
        </div>
      ) : null}
      {triggerSignals.length && triggerSignals.join("|") !== matchedSignals.join("|") ? (
        <div className="mt-2">
          <div className="mb-1 text-[11px] font-medium text-ink-500">{t("triggerSignals")}</div>
          <TokenList values={triggerSignals} empty="" />
        </div>
      ) : null}
    </div>
  );
}

type WeightedEvidenceRow = {
  key: string;
  raw?: number;
  weighted?: number;
};

function GdiRuntimeEvidencePanel({
  weighted,
}: {
  weighted?: EvolutionGdiBreakdown["post_apply_weighted"];
}) {
  const t = useTranslations("selfEvolution");
  if (!weighted) return null;
  const rawCount = finiteNumber(weighted.count);
  const negative = finiteNumber(weighted.weighted_negative_count);
  const healthy = finiteNumber(weighted.weighted_healthy_count);
  const observing = finiteNumber(weighted.weighted_observing_count);
  const halfLife = finiteNumber(weighted.decay?.half_life_days);
  const sourceCap = finiteNumber(weighted.decay?.source_weight_cap);
  const statusRows = weightedEvidenceRows(weighted.by_status, weighted.weighted_by_status).slice(0, 5);
  const sourceRows = weightedEvidenceRows(weighted.by_source, weighted.weighted_by_source).slice(0, 5);

  if (
    rawCount === null
    && negative === null
    && healthy === null
    && observing === null
    && !statusRows.length
    && !sourceRows.length
  ) {
    return null;
  }

  return (
    <div className="mt-3 border-t border-brand-500/10 pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium text-ink-500">{t("runtimeEvidence")}</span>
        {rawCount !== null ? <Pill tone="neutral">{t("rawObservations", { count: rawCount })}</Pill> : null}
        {negative !== null ? <Pill tone={negative >= 0.5 ? "danger" : "neutral"}>{t("weightedNegative", { value: formatWeight(negative) })}</Pill> : null}
        {healthy !== null ? <Pill tone={healthy >= 0.5 ? "ok" : "neutral"}>{t("weightedHealthy", { value: formatWeight(healthy) })}</Pill> : null}
        {observing !== null ? <Pill tone="brand">{t("weightedObserving", { value: formatWeight(observing) })}</Pill> : null}
      </div>
      {halfLife !== null || sourceCap !== null ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {sourceCap !== null ? <Pill tone="neutral">{t("sourceCap", { value: formatWeight(sourceCap) })}</Pill> : null}
          {halfLife !== null ? <Pill tone="neutral">{t("halfLifeDays", { value: formatWeight(halfLife) })}</Pill> : null}
        </div>
      ) : null}
      {statusRows.length || sourceRows.length ? (
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <WeightedEvidenceList title={t("statusWeights")} rows={statusRows} />
          <WeightedEvidenceList title={t("sourceWeights")} rows={sourceRows} />
        </div>
      ) : null}
    </div>
  );
}

function weightedEvidenceRows(
  rawValue: unknown,
  weightedValue: unknown,
): WeightedEvidenceRow[] {
  const raw = numericRecord(rawValue);
  const weighted = numericRecord(weightedValue);
  const keys = new Set([...Object.keys(raw), ...Object.keys(weighted)]);
  return Array.from(keys)
    .map((key) => ({ key, raw: raw[key], weighted: weighted[key] }))
    .sort((a, b) => Math.max(b.weighted ?? 0, b.raw ?? 0) - Math.max(a.weighted ?? 0, a.raw ?? 0));
}

function WeightedEvidenceList({
  title,
  rows,
}: {
  title: string;
  rows: WeightedEvidenceRow[];
}) {
  const t = useTranslations("selfEvolution");
  if (!rows.length) return null;
  return (
    <div className="min-w-0">
      <div className="mb-1.5 text-[11px] font-medium text-ink-500">{title}</div>
      <div className="space-y-1">
        {rows.map((row) => (
          <div key={row.key} className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-2 border-b border-brand-500/10 pb-1 last:border-b-0 last:pb-0">
            <span className="min-w-0 truncate font-mono text-[11px] text-ink-300">{row.key}</span>
            {typeof row.raw === "number" ? (
              <span className="font-mono text-[11px] text-ink-500">{t("rawShort", { value: formatWeight(row.raw) })}</span>
            ) : null}
            {typeof row.weighted === "number" ? (
              <span className="font-mono text-[11px] text-ink-100">{t("weightedShort", { value: formatWeight(row.weighted) })}</span>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

type CandidateViewModel = {
  candidate: EvolutionAssetCandidate;
  previewType: string;
  previewStatus: string;
  selectedByOptimizer: boolean;
  evidenceRefs: string[];
  gates: Record<string, unknown>;
  gateChecks: Array<Record<string, unknown>>;
  gateBlockers: string[];
  gateWarnings: string[];
  canPromote: boolean;
  positive: boolean;
  negative: boolean;
};

function candidateViewModel(candidate: EvolutionAssetCandidate): CandidateViewModel {
  const payload = objectRecord(candidate.payload);
  const metadata = objectRecord(payload.metadata);
  const previewType = String(metadata.preview_type || "");
  const previewStatus = String(metadata.preview_status || "");
  const outcomeScore = finiteNumber(payload.outcome_score);
  const selectedByOptimizer = Boolean(metadata.selected_by_optimizer);
  const evidenceRefs = stringList(candidate.evidence_refs);
  const gates = objectRecord(candidate.promotion_gates);
  const gateChecks = Array.isArray(gates.checks)
    ? gates.checks.map((row) => objectRecord(row)).filter((row) => row.id || row.summary)
    : [];
  const gateBlockers = stringList(gates.blockers);
  const gateWarnings = stringList(gates.warnings);
  const canPromote = candidate.safe_to_promote && gates.can_promote !== false;
  const negative = previewStatus === "failed" || (outcomeScore !== null && outcomeScore < 0);
  const positive = !negative && (previewStatus === "passed" || (outcomeScore !== null && outcomeScore > 0));
  return {
    candidate,
    previewType,
    previewStatus,
    selectedByOptimizer,
    evidenceRefs,
    gates,
    gateChecks,
    gateBlockers,
    gateWarnings,
    canPromote,
    positive,
    negative,
  };
}

function candidateFilterCounts(rows: CandidateViewModel[]): Record<CandidateFilter, number> {
  return {
    all: rows.length,
    ready: rows.filter((row) => row.canPromote).length,
    blocked: rows.filter((row) => !row.canPromote).length,
    positive: rows.filter((row) => row.positive).length,
    negative: rows.filter((row) => row.negative).length,
  };
}

function candidateMatchesFilter(row: CandidateViewModel, filter: CandidateFilter): boolean {
  if (filter === "ready") return row.canPromote;
  if (filter === "blocked") return !row.canPromote;
  if (filter === "positive") return row.positive;
  if (filter === "negative") return row.negative;
  return true;
}

function ScoreBar({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-2 text-[11px]">
        <span className="text-ink-500">{label}</span>
        <span className="font-mono text-ink-300">{formatScorePercent(value)}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-ink-900">
        <div className="h-full rounded-full bg-brand-400/70" style={{ width: scoreBarWidth(value) }} />
      </div>
    </div>
  );
}

function AssetsPanel({
  assets,
  candidates,
  optimizerFeedback,
  busy,
  onEvidenceRef,
  onPromote,
  onReject,
}: {
  assets: EvolutionAsset[];
  candidates: EvolutionAssetCandidate[];
  optimizerFeedback?: EvolutionOptimizerFeedbackSummary | null;
  busy: string;
  onEvidenceRef: (ref: string) => void;
  onPromote: (id: string) => Promise<void>;
  onReject: (id: string) => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  const [candidateFilter, setCandidateFilter] = useState<CandidateFilter>("all");
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null);
  const candidateRows = candidates.map(candidateViewModel);
  const candidateCounts = candidateFilterCounts(candidateRows);
  const visibleCandidateRows = candidateRows.filter((row: CandidateViewModel) =>
    candidateMatchesFilter(row, candidateFilter),
  );
  const selectedCandidateRow = candidateRows.find((row) => row.candidate.id === selectedCandidateId) ?? null;
  return (
    <>
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_420px]">
        <Card
          title={t("genesAndCapsules", { count: assets.length })}
          description={t("genesDesc")}
        >
          {!assets.length ? (
            <Empty title={t("noAssetsMatch")} subtitle={t("noAssetsSubtitle")} />
          ) : (
            <div className="embedded-list-scroll-lg divide-y divide-brand-500/10">
              {assets.map((asset) => (
                <div key={`${asset.kind}:${asset.id}`} className="py-3 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <Pill tone={asset.kind === "gene" ? "brand" : "ok"}>{asset.kind}</Pill>
                    <span className="font-mono text-ink-100">{asset.id}</span>
                    {asset.category ? <span className="text-[11px] text-ink-500">{asset.category}</span> : null}
                    {typeof asset.confidence === "number" ? <Pill tone="neutral">{Math.round(asset.confidence * 100)}%</Pill> : null}
                  </div>
                  {asset.summary ? <div className="mt-1 text-ink-300">{asset.summary}</div> : null}
                  {asset.gdi ? <GdiMiniPanel gdi={asset.gdi} /> : null}
                  <div className="mt-2">
                    <TokenList values={(asset.evidence_refs ?? []) as string[]} empty="" onSelect={onEvidenceRef} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
        <div className="min-w-0 space-y-5">
          <OptimizerFeedbackSummaryPanel feedback={optimizerFeedback} onEvidenceRef={onEvidenceRef} />
          <Card title={t("candidates", { count: candidates.length })} description={t("candidatesDesc")}>
            {!candidates.length ? (
              <Empty title={t("noCandidates")} subtitle={t("noCandidatesSub")} />
            ) : (
              <div className="space-y-3">
                <div className="flex flex-wrap gap-1.5" data-testid="candidate-filter-controls">
                  {CANDIDATE_FILTERS.map((filter) => (
                    <button
                      key={filter}
                      type="button"
                      aria-pressed={candidateFilter === filter}
                      className={[
                        "rounded-md border px-2 py-1 text-[11px] font-medium transition",
                        candidateFilter === filter
                          ? "border-accent-500/40 bg-accent-500/15 text-ink-100"
                          : "border-brand-500/10 bg-ink-950/35 text-ink-400 hover:border-brand-400/30 hover:text-ink-100",
                      ].join(" ")}
                      onClick={() => setCandidateFilter(filter)}
                    >
                      {t(`candidateFilter.${filter}`, { count: candidateCounts[filter] })}
                    </button>
                  ))}
                </div>
                {!visibleCandidateRows.length ? (
                  <Empty title={t("noCandidatesInFilter")} subtitle={t("noCandidatesInFilterSub")} />
                ) : null}
                {visibleCandidateRows.map((row: CandidateViewModel) => {
                  const {
                    candidate,
                    previewType,
                    previewStatus,
                    evidenceRefs,
                    gateBlockers,
                    canPromote,
                  } = row;
                  return (
                    <div
                      key={candidate.id}
                      data-testid="asset-candidate-card"
                      className="rounded-lg border border-brand-500/10 bg-ink-950/35 p-3 text-sm"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <Pill tone={candidate.kind === "gene" ? "brand" : "ok"}>{candidate.kind}</Pill>
                            <span className="break-all font-mono text-ink-100">{candidate.id}</span>
                            {previewType ? <Pill tone="neutral">{previewType}</Pill> : null}
                            {previewStatus ? <Pill tone={toneForStatus(previewStatus)}>{previewStatus}</Pill> : null}
                          </div>
                          <div className="mt-2 text-ink-300">{candidate.summary}</div>
                        </div>
                        <Pill tone={canPromote ? "ok" : "danger"}>
                          {canPromote ? t("safe") : t("blockedStatus")}
                        </Pill>
                      </div>
                      <TokenList values={uniqueStrings([...candidate.blocked_reasons, ...gateBlockers])} tone="danger" empty="" />
                      {evidenceRefs.length ? (
                        <div className="mt-2">
                          <TokenList values={evidenceRefs.slice(0, 6)} empty="" onSelect={onEvidenceRef} />
                        </div>
                      ) : null}
                      <div className="mt-3 flex flex-wrap justify-end gap-2">
                        <button className="btn btn-ghost" type="button" onClick={() => setSelectedCandidateId(candidate.id)}>
                          <SearchIcon size={14} />
                          {t("inspect")}
                        </button>
                        <button className="btn btn-ghost" disabled={busy === candidate.id} onClick={() => void onReject(candidate.id)}>
                          <XIcon size={14} />
                          {t("reject")}
                        </button>
                        <button
                          className="btn btn-primary"
                          disabled={busy === candidate.id || !canPromote}
                          onClick={() => void onPromote(candidate.id)}
                        >
                          <CheckIcon size={14} />
                          {t("promote")}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </div>
      </div>
      <CandidateDetailDrawer
        row={selectedCandidateRow}
        busy={busy}
        onClose={() => setSelectedCandidateId(null)}
        onEvidenceRef={onEvidenceRef}
        onPromote={onPromote}
        onReject={onReject}
      />
    </>
  );
}

function CandidateDetailDrawer({
  row,
  busy,
  onClose,
  onEvidenceRef,
  onPromote,
  onReject,
}: {
  row: CandidateViewModel | null;
  busy: string;
  onClose: () => void;
  onEvidenceRef: (ref: string) => void;
  onPromote: (id: string) => Promise<void>;
  onReject: (id: string) => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  if (!row) return null;
  const {
    candidate,
    previewType,
    previewStatus,
    selectedByOptimizer,
    evidenceRefs,
    gates,
    gateChecks,
    gateBlockers,
    gateWarnings,
    canPromote,
  } = row;
  const payload = objectRecord(candidate.payload);
  const metadata = objectRecord(payload.metadata);
  const outcomeScore = finiteNumber(payload.outcome_score);
  const blockers = uniqueStrings([...candidate.blocked_reasons, ...gateBlockers]);
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-ink-950/60 backdrop-blur-sm" data-testid="candidate-detail-drawer">
      <button
        type="button"
        className="absolute inset-0 cursor-default"
        aria-label={t("closeCandidateDetails")}
        onClick={onClose}
      />
      <aside className="relative z-10 flex h-full w-full max-w-2xl flex-col border-l border-brand-500/15 bg-ink-950 shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-brand-500/10 px-5 py-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Pill tone={candidate.kind === "gene" ? "brand" : "ok"}>{candidate.kind}</Pill>
              <Pill tone={canPromote ? "ok" : "danger"}>{canPromote ? t("safe") : t("blockedStatus")}</Pill>
              {previewType ? <Pill tone="neutral">{previewType}</Pill> : null}
              {previewStatus ? <Pill tone={toneForStatus(previewStatus)}>{previewStatus}</Pill> : null}
              {selectedByOptimizer ? <Pill tone="brand">{t("optimizerSelected")}</Pill> : null}
              {gates.review_only_until_promoted ? <Pill tone="neutral">{t("candidateReviewOnly")}</Pill> : null}
              {gates.selector_eligible === false ? <Pill tone="neutral">{t("candidateNotSelectorEligible")}</Pill> : null}
              {row.negative ? <Pill tone="danger">{t("candidateNegative")}</Pill> : null}
              {row.positive ? <Pill tone="ok">{t("candidatePositive")}</Pill> : null}
            </div>
            <h3 className="mt-2 break-all text-lg font-semibold text-ink-50">{candidate.id}</h3>
            <p className="mt-1 text-sm text-ink-400">{candidate.summary}</p>
          </div>
          <button
            type="button"
            data-testid="candidate-detail-close"
            className="btn btn-ghost px-2"
            aria-label={t("closeCandidateDetails")}
            onClick={onClose}
          >
            <XIcon size={16} />
          </button>
        </div>
        <div className="embedded-scroll min-h-0 flex-1 space-y-4 px-5 py-4 text-sm">
          <section className="grid gap-2 sm:grid-cols-2">
            <DetailMini label={t("candidateState")} value={String(candidate.state || t("unknown"))} />
            <DetailMini label={t("strategy")} value={String(candidate.strategy_id || t("missing"))} />
            <DetailMini label={t("candidateOutcomeScore")} value={outcomeScore === null ? t("missing") : formatWeight(outcomeScore)} />
            <DetailMini label={t("candidateSourceEvent")} value={String(candidate.source_event_id || t("missing"))} />
          </section>

          <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
            <div className="mb-2 text-[11px] font-medium text-ink-500">{t("candidateProvenance")}</div>
            <div className="grid gap-2 sm:grid-cols-2">
              {["origin", "optimizer_run_id", "optimizer_candidate_id", "preview_type", "preview_status"].map((key) => (
                <DetailMini key={key} label={key} value={String(metadata[key] || t("missing"))} />
              ))}
            </div>
          </section>

          <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <div className="text-[11px] font-medium text-ink-500">{t("candidatePromotionGates")}</div>
              <Pill tone={canPromote ? "ok" : "danger"}>{canPromote ? t("safe") : t("blockedStatus")}</Pill>
            </div>
            {blockers.length ? (
              <div className="mb-2">
                <TokenList values={blockers} tone="danger" empty="" />
              </div>
            ) : null}
            {gateWarnings.length ? (
              <div className="mb-2">
                <TokenList values={gateWarnings} empty="" />
              </div>
            ) : null}
            {gateChecks.length ? (
              <div className="space-y-2">
                {gateChecks.map((check: Record<string, unknown>, index: number) => {
                  const id = String(check.id || index);
                  const status = String(check.status || "unknown");
                  return (
                    <div key={`${id}:${index}`} className="rounded-md border border-brand-500/10 bg-ink-900/45 p-2">
                      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                        <Pill tone={toneForStatus(status)}>{status}</Pill>
                        <span className="break-all font-mono text-[11px] text-ink-500">{id}</span>
                      </div>
                      <div className="mt-1 break-words text-[12px] text-ink-300">{String(check.summary || id)}</div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="text-[12px] text-ink-500">{t("noCandidateGateChecks")}</div>
            )}
          </section>

          <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <div className="text-[11px] font-medium text-ink-500">{t("candidateEvidence")}</div>
              <Pill tone={evidenceRefs.length ? "brand" : "neutral"}>{evidenceRefs.length}</Pill>
            </div>
            <TokenList values={evidenceRefs} empty={t("noEvidence")} onSelect={onEvidenceRef} />
          </section>

          <DebugDetails
            title={t("candidateDebugPayload")}
            description={t("candidateDebugPayloadDesc")}
            testId="candidate-debug-payload"
          >
            <Json value={{ payload: candidate.payload, promotion_gates: candidate.promotion_gates }} />
          </DebugDetails>
        </div>
        <div className="flex flex-wrap justify-end gap-2 border-t border-brand-500/10 px-5 py-4">
          <button className="btn btn-ghost" disabled={busy === candidate.id} onClick={() => void onReject(candidate.id)}>
            <XIcon size={14} />
            {t("reject")}
          </button>
          <button
            className="btn btn-primary"
            disabled={busy === candidate.id || !canPromote}
            onClick={() => void onPromote(candidate.id)}
          >
            <CheckIcon size={14} />
            {t("promote")}
          </button>
        </div>
      </aside>
    </div>
  );
}

function OptimizerFeedbackSummaryPanel({
  feedback,
  onEvidenceRef,
}: {
  feedback?: EvolutionOptimizerFeedbackSummary | null;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const runCount = finiteNumber(feedback?.run_count);
  const sampleCount = finiteNumber(feedback?.sample_count);
  const positiveSamples = finiteNumber(feedback?.positive_samples);
  const negativeSamples = finiteNumber(feedback?.negative_samples);
  const neutralSamples = finiteNumber(feedback?.neutral_samples);
  const positive = Array.isArray(feedback?.top_positive_features) ? feedback.top_positive_features : [];
  const negative = Array.isArray(feedback?.top_negative_features) ? feedback.top_negative_features : [];
  const examples = Array.isArray(feedback?.recent_examples) ? feedback.recent_examples.slice(-3).reverse() : [];
  const decisions = feedback?.candidate_decisions;
  const recentDecisions = Array.isArray(decisions?.recent) ? decisions.recent.slice(0, 4) : [];
  const decisionEvidenceRefs = stringList(decisions?.evidence_refs);
  const evidenceRefs = stringList(feedback?.evidence_refs);
  const hasDetails = Boolean(
    feedback?.calibration
      || positive.length
      || negative.length
      || examples.length
      || recentDecisions.length
      || evidenceRefs.length
      || decisionEvidenceRefs.length,
  );
  if (!feedback || (!runCount && !sampleCount && !positive.length && !negative.length && !examples.length && !recentDecisions.length)) {
    return null;
  }
  return (
    <div data-testid="optimizer-feedback-summary">
      <Card title={t("optimizerFeedbackSummary")} description={t("optimizerFeedbackSummaryDesc")}>
        <div className="space-y-3 text-sm">
          <div className="flex flex-wrap gap-1.5">
            {runCount !== null ? <Pill tone="neutral">{t("optimizerFeedbackRuns", { count: runCount })}</Pill> : null}
            {sampleCount !== null ? <Pill tone="brand">{t("optimizerFeedbackSamples", { count: sampleCount })}</Pill> : null}
            {positiveSamples !== null ? <Pill tone="ok">{t("optimizerFeedbackPositiveSamples", { count: positiveSamples })}</Pill> : null}
            {negativeSamples !== null ? <Pill tone="danger">{t("optimizerFeedbackNegativeSamples", { count: negativeSamples })}</Pill> : null}
            {neutralSamples !== null ? <Pill tone="neutral">{t("optimizerFeedbackNeutralSamples", { count: neutralSamples })}</Pill> : null}
          </div>

          {hasDetails ? (
            <details className="border-t border-brand-500/10 pt-3" data-testid="optimizer-feedback-details">
              <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 text-[12px] text-ink-300 marker:hidden">
                <span className="font-medium">{t("optimizerFeedbackDetails")}</span>
                <span className="text-ink-500">{t("optimizerFeedbackDetailsDesc")}</span>
              </summary>
              <div className="mt-3 space-y-3">
                <OptimizerCalibrationPanel calibration={feedback.calibration} />

                {positive.length || negative.length ? (
                  <div className="grid gap-2">
                    <OptimizerFeedbackFeatureList title={t("optimizerFeedbackPositive")} rows={positive.slice(0, 5)} tone="ok" />
                    <OptimizerFeedbackFeatureList title={t("optimizerFeedbackNegative")} rows={negative.slice(0, 5)} tone="danger" />
                  </div>
                ) : (
                  <div className="text-[12px] text-ink-500">{t("noOptimizerFeedbackFeatures")}</div>
                )}

                {examples.length ? (
                  <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
                    <div className="mb-1.5 text-[11px] font-medium text-ink-500">{t("optimizerFeedbackRecent")}</div>
                    <div className="space-y-1.5">
                      {examples.map((example, index) => (
                        <div key={`${example.proposal_id || example.run_id || index}:${index}`} className="min-w-0 text-[12px] text-ink-300">
                          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                            {example.state ? <Pill tone={toneForStatus(example.state)}>{example.state}</Pill> : null}
                            {example.selected_candidate_id ? (
                              <span className="break-all font-mono text-[11px] text-ink-200">{example.selected_candidate_id}</span>
                            ) : null}
                            {typeof example.feedback_sample_count === "number" ? (
                              <Pill tone="neutral">{t("optimizerFeatureSamples", { count: example.feedback_sample_count })}</Pill>
                            ) : null}
                          </div>
                          {example.proposal_id || example.run_id ? (
                            <div className="mt-1 min-w-0 break-all font-mono text-[11px] text-ink-500">
                              {[example.proposal_id, example.run_id].filter(Boolean).join(" / ")}
                            </div>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}

                <OptimizerCandidateDecisionList
                  summary={decisions}
                  rows={recentDecisions}
                  onEvidenceRef={onEvidenceRef}
                />

                {evidenceRefs.length || decisionEvidenceRefs.length ? (
                  <div>
                    <div className="mb-1.5 text-[11px] font-medium text-ink-500">{t("evidence")}</div>
                    <TokenList values={uniqueStrings([...evidenceRefs, ...decisionEvidenceRefs])} empty="" onSelect={onEvidenceRef} />
                  </div>
                ) : null}
              </div>
            </details>
          ) : null}
        </div>
      </Card>
    </div>
  );
}

function OptimizerCalibrationPanel({
  calibration,
}: {
  calibration?: EvolutionOptimizerFeedbackSummary["calibration"];
}) {
  const t = useTranslations("selfEvolution");
  if (!calibration || typeof calibration !== "object") return null;
  const status = String(calibration.status || "");
  const confidence = String(calibration.confidence || "");
  const sourceMix = objectRecord(calibration.source_mix);
  const polarityMix = objectRecord(calibration.polarity_mix);
  const concentration = objectRecord(calibration.feature_concentration);
  const warnings = stringList(calibration.warnings);
  const proposalRatio = finiteNumber(sourceMix.proposal_ratio);
  const decisionRatio = finiteNumber(sourceMix.candidate_decision_ratio);
  const positiveRatio = finiteNumber(polarityMix.positive_ratio);
  const negativeRatio = finiteNumber(polarityMix.negative_ratio);
  const topFeatureRatio = finiteNumber(concentration.top_feature_ratio);
  if (!status && !confidence && !warnings.length && proposalRatio === null && decisionRatio === null) {
    return null;
  }
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2" data-testid="optimizer-calibration">
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] font-medium text-ink-500">{t("optimizerCalibration")}</span>
        {status ? <Pill tone={toneForStatus(status)}>{status}</Pill> : null}
        {confidence ? <Pill tone={toneForOptimizerConfidence(confidence)}>{t("optimizerCalibrationConfidence", { value: confidence })}</Pill> : null}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {proposalRatio !== null ? (
          <Pill tone="neutral">{t("optimizerCalibrationProposalRatio", { value: formatRatioPercent(proposalRatio) })}</Pill>
        ) : null}
        {decisionRatio !== null ? (
          <Pill tone={decisionRatio >= 0.7 ? "warn" : "neutral"}>
            {t("optimizerCalibrationDecisionRatio", { value: formatRatioPercent(decisionRatio) })}
          </Pill>
        ) : null}
        {positiveRatio !== null ? (
          <Pill tone="ok">{t("optimizerCalibrationPositiveRatio", { value: formatRatioPercent(positiveRatio) })}</Pill>
        ) : null}
        {negativeRatio !== null ? (
          <Pill tone={negativeRatio >= 0.7 ? "danger" : "neutral"}>
            {t("optimizerCalibrationNegativeRatio", { value: formatRatioPercent(negativeRatio) })}
          </Pill>
        ) : null}
        {topFeatureRatio !== null ? (
          <Pill tone={topFeatureRatio >= 0.75 ? "warn" : "neutral"}>
            {t("optimizerCalibrationFeatureConcentration", { value: formatRatioPercent(topFeatureRatio) })}
          </Pill>
        ) : null}
      </div>
      {warnings.length ? (
        <div className="mt-2">
          <TokenList values={warnings.slice(0, 6)} empty="" tone="danger" />
        </div>
      ) : null}
    </div>
  );
}

function toneForOptimizerConfidence(value: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  if (value === "high") return "ok";
  if (value === "medium") return "warn";
  if (value === "low") return "danger";
  return "neutral";
}

function formatRatioPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function OptimizerCandidateDecisionList({
  summary,
  rows,
  onEvidenceRef,
}: {
  summary?: EvolutionOptimizerFeedbackSummary["candidate_decisions"];
  rows: EvolutionOptimizerCandidateDecision[];
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const total = finiteNumber(summary?.total);
  const promoted = finiteNumber(summary?.promoted);
  const rejected = finiteNumber(summary?.rejected);
  if (!rows.length && !total) return null;
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2" data-testid="optimizer-candidate-decisions">
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] font-medium text-ink-500">{t("optimizerCandidateDecisions")}</span>
        {total !== null ? <Pill tone="brand">{t("optimizerCandidateDecisionTotal", { count: total })}</Pill> : null}
        {promoted !== null ? <Pill tone="ok">{t("optimizerCandidateDecisionPromoted", { count: promoted })}</Pill> : null}
        {rejected !== null ? <Pill tone="danger">{t("optimizerCandidateDecisionRejected", { count: rejected })}</Pill> : null}
      </div>
      {rows.length ? (
        <div className="space-y-2">
          {rows.map((row, index) => {
            const state = String(row.state || row.decision || "unknown");
            const refs = stringList(row.evidence_refs);
            return (
              <div key={`${row.candidate_id || index}:${index}`} className="rounded border border-brand-500/10 bg-ink-900/45 p-2">
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <Pill tone={toneForStatus(state)}>{state}</Pill>
                  {row.preview_status ? <Pill tone={toneForStatus(String(row.preview_status))}>{String(row.preview_status)}</Pill> : null}
                  {row.preview_type ? <Pill tone="neutral">{String(row.preview_type)}</Pill> : null}
                  {row.selected_by_optimizer ? <Pill tone="brand">{t("optimizerSelected")}</Pill> : null}
                  <span className="break-all font-mono text-[11px] text-ink-200">
                    {row.optimizer_candidate_id || row.candidate_id || t("unknown")}
                  </span>
                </div>
                {row.summary ? <div className="mt-1 text-[12px] text-ink-400">{row.summary}</div> : null}
                {row.promoted_ref || row.rejected_reason ? (
                  <div className="mt-1 break-all font-mono text-[11px] text-ink-500">
                    {row.promoted_ref ? `${t("promotedRef")}: ${row.promoted_ref}` : `${t("rejectedReason")}: ${row.rejected_reason}`}
                  </div>
                ) : null}
                {refs.length ? (
                  <div className="mt-1.5">
                    <TokenList values={refs.slice(0, 4)} empty="" onSelect={onEvidenceRef} />
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function CandidateBacktestPreviewGroup({
  preview,
  onEvidenceRef,
}: {
  preview: EvolutionCandidateBacktestPreview;
  onEvidenceRef?: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const status = String(preview.status || "unknown");
  const delta = finiteNumber(preview.score_delta);
  const blockers = stringList(preview.blocked_reasons);
  const refs = stringList(preview.evidence_refs);
  const result = preview.backtest_result ?? {};
  const metrics: Array<{ key: string; value: unknown }> = [
    { key: "verdict", value: result.verdict },
    { key: "return", value: result.total_return_pct },
    { key: "drawdown", value: result.max_drawdown_pct },
    { key: "sharpe", value: result.sharpe_ratio },
    { key: "trades", value: result.total_trades },
  ].filter(({ value }) => value !== undefined && value !== null && String(value) !== "");
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] font-medium text-ink-500">{t("optimizerBacktestPreview")}</span>
        <Pill tone={toneForStatus(status)}>{status}</Pill>
        {delta !== null && delta !== 0 ? (
          <Pill tone={delta > 0 ? "ok" : "danger"}>
            {t("optimizerPreviewDelta", { delta: formatCompactValue(delta) })}
          </Pill>
        ) : null}
        {preview.allow_mock === false ? <Pill tone="neutral">{t("realDataOnly")}</Pill> : null}
      </div>
      {preview.reason ? <div className="text-[12px] text-ink-500">{preview.reason}</div> : null}
      {metrics.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {metrics.map(({ key, value }) => (
            <Pill key={key} tone={key === "verdict" ? toneForStatus(String(value)) : "neutral"}>
              {key} {formatCompactValue(value)}
            </Pill>
          ))}
        </div>
      ) : null}
      <CandidateBacktestBaselineComparison
        comparison={preview.baseline_comparison}
        onEvidenceRef={onEvidenceRef}
      />
      {blockers.length ? (
        <div className="mt-2">
          <OptimizerTokenGroup label={t("blockers")} tokens={blockers} danger />
        </div>
      ) : null}
      {refs.length ? (
        <div className="mt-2">
          <TokenList values={refs} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </div>
  );
}

function CandidateBacktestBaselineComparison({
  comparison,
  onEvidenceRef,
}: {
  comparison?: EvolutionCandidateBacktestPreview["baseline_comparison"];
  onEvidenceRef?: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  if (!comparison || typeof comparison !== "object") return null;
  const status = String(comparison.status || "");
  const direction = String(comparison.overall_direction || "");
  const delta = finiteNumber(comparison.score_delta);
  const deltas = Array.isArray(comparison.metrics_delta)
    ? comparison.metrics_delta.slice(0, 5)
    : [];
  const refs = stringList(comparison.evidence_refs);
  if (!status && !direction && delta === null && !deltas.length && !refs.length) return null;
  return (
    <div className="mt-2 rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] font-medium text-ink-500">{t("optimizerBacktestBaseline")}</span>
        {status ? <Pill tone={toneForStatus(status)}>{status}</Pill> : null}
        {direction ? <Pill tone={toneForStatus(direction)}>{direction}</Pill> : null}
        {delta !== null && delta !== 0 ? (
          <Pill tone={delta > 0 ? "ok" : "danger"}>
            {t("optimizerPreviewDelta", { delta: formatCompactValue(delta) })}
          </Pill>
        ) : null}
      </div>
      {comparison.summary ? <div className="text-[12px] text-ink-500">{String(comparison.summary)}</div> : null}
      {deltas.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {deltas.map((row, index) => {
            const key = String(row.key || index);
            const metricDelta = finiteNumber(row.delta);
            return (
              <Pill key={`${key}:${index}`} tone={toneForStatus(String(row.direction || ""))}>
                {key} {metricDelta !== null ? formatCompactValue(metricDelta) : String(row.direction || "")}
              </Pill>
            );
          })}
        </div>
      ) : null}
      {refs.length ? (
        <div className="mt-2">
          <TokenList values={refs.slice(0, 3)} empty="" onSelect={onEvidenceRef} />
        </div>
      ) : null}
    </div>
  );
}

function OptimizerFeedbackFeatureList({
  title,
  rows,
  tone,
}: {
  title: string;
  rows: EvolutionOptimizerFeedbackFeature[];
  tone: "ok" | "danger";
}) {
  const t = useTranslations("selfEvolution");
  if (!rows.length) return null;
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 p-2">
      <div className="mb-1.5 text-[11px] font-medium text-ink-500">{title}</div>
      <div className="space-y-1.5">
        {rows.map((row, index) => {
          const feature = String(row.feature || t("unknown"));
          const net = finiteNumber(row.net);
          const samples = finiteNumber(row.samples);
          return (
            <div key={`${feature}:${index}`} className="flex min-w-0 flex-wrap items-center gap-1.5">
              <Pill tone={tone}>{net !== null ? formatCompactValue(net) : t("unknown")}</Pill>
              <span className="min-w-0 break-all font-mono text-[11px] text-ink-300">{feature}</span>
              {samples !== null ? <Pill tone="neutral">{t("optimizerFeatureSamples", { count: samples })}</Pill> : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ProposalsPanel({
  proposals,
  busy,
  onValidate,
  onApprove,
  onReject,
  onApply,
  onEvidenceRef,
}: {
  proposals: Array<Record<string, unknown>>;
  busy: string;
  onValidate: (id: string, dryRun?: boolean) => Promise<void>;
  onApprove: (id: string) => Promise<void>;
  onReject: (id: string) => Promise<void>;
  onApply: (id: string) => Promise<void>;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const [detail, setDetail] = useState<EvolutionProposalDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailBusy, setDetailBusy] = useState("");
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    if (!detail) return;
    if (!proposals.some((proposal) => String(proposal.id || "") === detail.id)) {
      setDetail(null);
      setDetailOpen(false);
      setDetailError(null);
    }
  }, [detail, proposals]);

  async function inspectProposal(id: string, clearDetail = true) {
    setDetailOpen(true);
    if (clearDetail) setDetail(null);
    setDetailBusy(id);
    setDetailError(null);
    try {
      const out = await clientApi.proposalDetail(id);
      if (out.error) throw new Error(String(out.error));
      setDetail(out);
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : String(e));
    } finally {
      setDetailBusy("");
    }
  }

  async function validateAndRefresh(id: string, dryRun: boolean) {
    await onValidate(id, dryRun);
    if (detail?.id === id) {
      await inspectProposal(id, false);
    }
  }

  async function approveAndRefresh(id: string) {
    await onApprove(id);
    if (detail?.id === id) {
      await inspectProposal(id, false);
    }
  }

  async function rejectAndRefresh(id: string) {
    await onReject(id);
    if (detail?.id === id) {
      await inspectProposal(id);
    }
  }

  async function applyAndRefresh(id: string) {
    await onApply(id);
    if (detail?.id === id) {
      await inspectProposal(id, false);
    }
  }

  if (!proposals.length && busy === "load") {
    return (
      <Card title={t("openProposalsTitle", { count: 0 })} description={t("openProposalsDesc")}>
        <div className="rounded-lg border border-brand-500/10 bg-ink-950/30 px-4 py-8 text-center text-sm text-ink-400">
          {t("loading")}
        </div>
      </Card>
    );
  }
  if (!proposals.length) {
    return (
      <Card title={t("openProposalsEmpty")}>
        <Empty title={t("openProposalsEmpty")} subtitle={t("openProposalsEmptySub")} />
      </Card>
    );
  }
  return (
    <div className="space-y-4">
      <Card title={t("openProposalsTitle", { count: proposals.length })} description={t("openProposalsDesc")}>
        <div className="embedded-list-scroll-lg divide-y divide-brand-500/10">
          {proposals.map((proposal) => {
            const id = String(proposal.id || "");
            const state = String(proposal.state || "");
            const evidence = Array.isArray(proposal.evidence_refs) ? proposal.evidence_refs.map(String) : [];
            const validation = String(proposal.validation_plan_id || t("missing"));
            const target = String(proposal.target || t("workspaceProposal"));
            return (
              <div key={id} className="py-3 text-sm transition hover:bg-white/[0.02]" data-testid="proposal-row">
                <div className="grid gap-3 px-1 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <Pill tone={toneForStatus(state)}>
                        {state || t("unknown")}
                      </Pill>
                      <span className="break-all font-mono text-[12px] text-ink-100">{id}</span>
                      <span className="text-[11px] text-ink-500">{String(proposal.kind || "")}</span>
                    </div>
                    <button
                      type="button"
                      className="mt-1 block w-full min-w-0 text-left"
                      disabled={detailBusy === id}
                      onClick={() => void inspectProposal(id)}
                    >
                      <span className="block break-words text-ink-200 hover:text-brand-100">
                        {String(proposal.summary || id)}
                      </span>
                    </button>
                    <div className="mt-2 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-ink-500">
                      <span className="min-w-0 max-w-full truncate">{t("validation")}: {validation}</span>
                      <span className="min-w-0 max-w-full truncate">{t("target")}: {target}</span>
                      {evidence.length ? (
                        <span>{t("evidenceCount", { count: evidence.length })}</span>
                      ) : null}
                    </div>
                    {evidence.length ? (
                      <div className="mt-2 max-w-full overflow-hidden">
                        <TokenList values={evidence.slice(0, 3)} empty="" onSelect={onEvidenceRef} />
                      </div>
                    ) : null}
                  </div>
                  <div className="flex justify-start sm:justify-end">
                    <button
                      className="btn btn-ghost px-3 py-1.5 text-[12px]"
                      disabled={detailBusy === id}
                      onClick={() => void inspectProposal(id)}
                    >
                      <DiffIcon size={14} />
                      {detailBusy === id ? t("loading") : t("inspect")}
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </Card>
      {detailOpen ? (
        <ProposalDetailDrawer
          detail={detail}
          loading={Boolean(detailBusy)}
          error={detailError}
          onClose={() => setDetailOpen(false)}
          onEvidenceRef={onEvidenceRef}
          busy={busy === detail?.id}
          onValidate={detail ? (dryRun) => validateAndRefresh(detail.id, dryRun) : undefined}
          onApprove={detail ? () => approveAndRefresh(detail.id) : undefined}
          onReject={detail ? () => rejectAndRefresh(detail.id) : undefined}
          onApply={detail ? () => applyAndRefresh(detail.id) : undefined}
        />
      ) : null}
    </div>
  );
}

function ProposalDetailDrawer({
  detail,
  loading,
  error,
  onClose,
  onEvidenceRef,
  busy,
  onValidate,
  onApprove,
  onReject,
  onApply,
}: {
  detail: EvolutionProposalDetail | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onEvidenceRef: (ref: string) => void;
  busy?: boolean;
  onValidate?: (dryRun: boolean) => Promise<void>;
  onApprove?: () => Promise<void>;
  onReject?: () => Promise<void>;
  onApply?: () => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-ink-950/60 backdrop-blur-sm" data-testid="proposal-detail-drawer">
      <button
        type="button"
        className="absolute inset-0 cursor-default"
        aria-label={t("closeProposalDetail")}
        onClick={onClose}
      />
      <aside className="relative z-10 flex h-full w-full max-w-3xl flex-col border-l border-brand-500/15 bg-ink-950 shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-brand-500/10 px-5 py-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold text-ink-50">{t("proposalDetail")}</h3>
              {detail?.state ? <Pill tone={toneForStatus(detail.state)}>{detail.state}</Pill> : null}
            </div>
            {detail?.summary ? <div className="mt-1 break-words text-[12px] text-ink-400">{detail.summary}</div> : null}
          </div>
          <button
            type="button"
            className="btn btn-ghost px-2"
            aria-label={t("closeProposalDetail")}
            onClick={onClose}
          >
            <XIcon size={16} />
          </button>
        </div>
        <div className="embedded-scroll min-h-0 flex-1 px-5 py-4">
          <ProposalDetailPanel
            detail={detail}
            loading={loading}
            error={error}
            onEvidenceRef={onEvidenceRef}
            busy={busy}
            onValidate={onValidate}
            onApprove={onApprove}
            onReject={onReject}
            onApply={onApply}
            framed={false}
          />
        </div>
      </aside>
    </div>
  );
}

function ProposalDetailPanel({
  detail,
  loading,
  error,
  onEvidenceRef,
  busy = false,
  onValidate,
  onApprove,
  onReject,
  onApply,
  framed = true,
}: {
  detail: EvolutionProposalDetail | null;
  loading: boolean;
  error: string | null;
  onEvidenceRef: (ref: string) => void;
  busy?: boolean;
  onValidate?: (dryRun: boolean) => Promise<void>;
  onApprove?: () => Promise<void>;
  onReject?: () => Promise<void>;
  onApply?: () => Promise<void>;
  framed?: boolean;
}) {
  const t = useTranslations("selfEvolution");
  if (error) {
    const content = (
      <ErrorBanner error={error} />
    );
    return framed ? (
      <Card title={t("proposalDetail")}>
        {content}
      </Card>
    ) : content;
  }
  if (loading && !detail) {
    const content = (
      <div className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-4 text-sm text-ink-300">
        {t("loading")}
      </div>
    );
    return framed ? (
      <Card title={t("proposalDetail")}>
        {content}
      </Card>
    ) : content;
  }
  if (!detail) {
    const content = <Empty title={t("noProposalSelected")} subtitle={t("selectProposal")} />;
    return framed ? (
      <Card title={t("proposalDetail")}>
        {content}
      </Card>
    ) : content;
  }
  const evidence = Array.isArray(detail.evidence_refs) ? detail.evidence_refs.map(String) : [];
  const changes = detail.file_changes ?? [];
  const state = String(detail.state || "");
  const canApprove = ["draft", "pending_review", "proposed"].includes(state);
  const canReject = ["draft", "pending_review", "proposed", "approved"].includes(state);
  const canApply = state === "approved" && Boolean(detail.action_gates?.can_apply);
  const content = (
    <div className="space-y-4 text-sm">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <Pill tone="warn">{detail.kind || t("unknown")}</Pill>
          <span className="font-mono text-[11px] text-ink-500">{detail.id}</span>
        </div>
        {detail.summary ? <p className="mt-2 text-ink-300">{detail.summary}</p> : null}
      </div>
      <AgentRunReplayPanel
        item={{
          id: detail.id,
          title: String(detail.kind || t("proposalDetail")),
          summary: String(detail.summary || ""),
          why: String(detail.summary || ""),
          proposal_id: detail.id,
          validation_plan_id: detail.validation_plan_id,
          strategy_id: String((detail.metadata || {}).strategy_id || ""),
        }}
        process={detail.process}
        onEvidenceRef={onEvidenceRef}
      />
      <RunResultSummary
        proposalId={detail.id}
        state={detail.state}
        target={String(detail.target || t("workspaceProposal"))}
        validationPlanId={detail.validation_plan_id}
        validationStatus={detail.action_gates?.validation?.status}
        blockedReasons={detail.action_gates?.blockers}
        evidenceRefs={evidence}
        fileChanges={changes}
        backtestComparison={detail.backtest_comparison}
        onEvidenceRef={onEvidenceRef}
      />
      {onValidate ? (
        <div className="flex flex-wrap justify-end gap-2">
          <button className="btn btn-ghost" disabled={busy} onClick={() => void onValidate(true)}>
            <ShieldCheckIcon size={14} />
            {t("checkPlan")}
          </button>
          <button className="btn btn-primary" disabled={busy} onClick={() => void onValidate(false)}>
            <ShieldCheckIcon size={14} />
            {busy ? t("validating") : t("runValidation")}
          </button>
          {canApprove && onApprove ? (
            <button className="btn btn-ghost" disabled={busy} onClick={() => void onApprove()}>
              <CheckIcon size={14} />
              {busy ? t("approving") : t("approveProposal")}
            </button>
          ) : null}
          {canReject && onReject ? (
            <button className="btn btn-ghost" disabled={busy} onClick={() => void onReject()}>
              <XIcon size={14} />
              {busy ? t("rejecting") : t("rejectProposal")}
            </button>
          ) : null}
          {state === "approved" && onApply ? (
            <button className="btn btn-primary" disabled={busy || !canApply} onClick={() => void onApply()}>
              <CheckIcon size={14} />
              {busy ? t("applying") : t("applyProposal")}
            </button>
          ) : null}
        </div>
      ) : null}
      <SupportingEvidenceStack
        lineageGraph={detail.lineage_graph}
        optimizerReport={detail.optimizer_report}
        actionGates={detail.action_gates}
        fitnessVector={detail.fitness_vector}
        whyReused={detail.why_reused}
        postApplyMonitor={detail.post_apply_monitor}
        onEvidenceRef={onEvidenceRef}
      />
      <ProposalReviewDetails
        changes={changes}
        backtestComparison={detail.backtest_comparison}
        rationaleMd={detail.rationale_md}
        onEvidenceRef={onEvidenceRef}
      />
    </div>
  );
  return framed ? (
    <Card
      title={t("proposalDetail")}
      actions={<Pill tone={toneForStatus(detail.state)}>{detail.state || t("unknown")}</Pill>}
    >
      {content}
    </Card>
  ) : content;
}

function ProposalReviewDetails({
  changes,
  backtestComparison,
  rationaleMd,
  onEvidenceRef,
}: {
  changes: EvolutionProposalFileChange[];
  backtestComparison?: EvolutionBacktestComparison | null;
  rationaleMd?: string | null;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const count = (backtestComparison ? 1 : 0) + (changes.length ? 1 : 0) + (rationaleMd ? 1 : 0);
  if (!count) return null;
  return (
    <details className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3" data-testid="proposal-review-details">
      <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 text-[12px] text-ink-300 marker:hidden">
        <span className="font-medium">{t("proposalReviewDetails")}</span>
        <span className="flex flex-wrap items-center gap-2">
          <span className="text-ink-500">{t("proposalReviewDetailsDesc")}</span>
          <Pill tone="neutral">{count}</Pill>
        </span>
      </summary>
      <div className="mt-3 space-y-3">
        {backtestComparison ? (
          <BacktestComparisonPanel
            comparison={backtestComparison}
            onEvidenceRef={onEvidenceRef}
          />
        ) : null}
        <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-[11px] font-medium text-ink-500">{t("changedFiles")}</div>
            <Pill tone={changes.length ? "ok" : "neutral"}>{changes.length}</Pill>
          </div>
          {changes.length ? (
            <div className="mt-3 space-y-3">
              {changes.map((change) => (
                <ProposalFileChangePreview key={change.path} change={change} />
              ))}
            </div>
          ) : (
            <div className="mt-3 text-sm text-ink-500">{t("noFileChanges")}</div>
          )}
        </section>
        {rationaleMd ? (
          <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
            <div className="text-[11px] font-medium text-ink-500">{t("rationale")}</div>
            <pre className="embedded-scroll mt-2 max-h-72 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
              {rationaleMd}
            </pre>
          </section>
        ) : null}
      </div>
    </details>
  );
}

function BacktestComparisonPanel({
  comparison,
  onEvidenceRef,
}: {
  comparison: EvolutionBacktestComparison;
  onEvidenceRef: (ref: string) => void;
}) {
  const t = useTranslations("selfEvolution");
  const deltas = comparison.metrics_delta ?? [];
  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] font-medium text-ink-500">{t("backtestComparison")}</div>
          {comparison.summary ? <div className="mt-1 text-[12px] text-ink-400">{comparison.summary}</div> : null}
        </div>
        <Pill tone={comparison.status === "complete" ? "ok" : "warn"}>{comparison.status}</Pill>
      </div>
      <div className="mt-3 grid gap-2 md:grid-cols-2">
        <BacktestRunMini label={t("beforeBacktest")} run={comparison.before ?? null} />
        <BacktestRunMini label={t("afterBacktest")} run={comparison.after ?? null} />
      </div>
      {deltas.length ? (
        <div className="mt-3 overflow-x-auto">
          <table className="min-w-full text-left text-[12px]">
            <thead className="text-ink-500">
              <tr>
                <th className="py-1 pr-3 font-medium">{t("metric")}</th>
                <th className="py-1 pr-3 font-medium">{t("before")}</th>
                <th className="py-1 pr-3 font-medium">{t("after")}</th>
                <th className="py-1 pr-3 font-medium">{t("delta")}</th>
              </tr>
            </thead>
            <tbody>
              {deltas.slice(0, 8).map((row) => (
                <tr key={row.key} className="border-t border-brand-500/10">
                  <td className="py-1.5 pr-3 font-mono text-ink-300">{row.key}</td>
                  <td className="py-1.5 pr-3 font-mono text-ink-300">{formatMetric(row.before)}</td>
                  <td className="py-1.5 pr-3 font-mono text-ink-300">{formatMetric(row.after)}</td>
                  <td className="py-1.5 pr-3">
                    <Pill tone={row.direction === "improved" ? "ok" : row.direction === "regressed" ? "danger" : "neutral"}>
                      {formatMetric(row.delta)}
                    </Pill>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="mt-3 text-sm text-ink-500">{t("noBacktestDelta")}</div>
      )}
      <div className="mt-3">
        <TokenList values={comparison.evidence_refs ?? []} empty="" onSelect={onEvidenceRef} />
      </div>
    </section>
  );
}

function BacktestRunMini({
  label,
  run,
}: {
  label: string;
  run: EvolutionBacktestComparison["before"] | null;
}) {
  const t = useTranslations("selfEvolution");
  const metrics = run?.metrics ?? {};
  return (
    <div className="rounded-md border border-brand-500/10 bg-ink-950/35 px-3 py-2">
      <div className="text-[11px] font-medium text-ink-500">{label}</div>
      {run ? (
        <>
          <div className="mt-1 font-mono text-[12px] text-ink-100">{run.backtest_id}</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {metrics.verdict ? <Pill tone={toneForStatus(String(metrics.verdict))}>{String(metrics.verdict)}</Pill> : null}
            {metrics.total_return_pct !== undefined ? <Pill tone="neutral">ret {String(metrics.total_return_pct)}%</Pill> : null}
            {metrics.max_drawdown_pct !== undefined ? <Pill tone="neutral">dd {String(metrics.max_drawdown_pct)}%</Pill> : null}
          </div>
        </>
      ) : (
        <div className="mt-1 text-[12px] text-ink-500">{t("missing")}</div>
      )}
    </div>
  );
}

function ProposalFileChangePreview({ change }: { change: EvolutionProposalFileChange }) {
  const t = useTranslations("selfEvolution");
  const diff = String(change.diff || "");
  return (
    <div className="rounded-md border border-brand-500/15 bg-ink-900/50 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={change.before_exists ? "warn" : "ok"}>
          {change.before_exists ? t("modifiedFile") : t("newFile")}
        </Pill>
        <span className="break-all font-mono text-[12px] text-ink-100">{change.path}</span>
        {change.before_truncated || change.after_truncated ? <Pill tone="warn">{t("truncated")}</Pill> : null}
      </div>
      {diff ? (
        <pre className="embedded-scroll mt-2 max-h-80 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
          {diff}
        </pre>
      ) : null}
      <details className="mt-2">
        <summary className="cursor-pointer text-[12px] text-ink-400">{t("beforeAfter")}</summary>
        <div className="mt-2 grid gap-2 lg:grid-cols-2">
          <div>
            <div className="mb-1 text-[11px] font-medium text-ink-500">{t("before")}</div>
            <pre className="embedded-scroll max-h-64 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
              {change.before_exists ? String(change.before || "") : t("beforeMissing")}
            </pre>
          </div>
          <div>
            <div className="mb-1 text-[11px] font-medium text-ink-500">{t("after")}</div>
            <pre className="embedded-scroll max-h-64 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
              {String(change.after || "")}
            </pre>
          </div>
        </div>
      </details>
    </div>
  );
}

function DetailMini({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-brand-500/10 bg-ink-950/30 px-3 py-2">
      <div className="text-[11px] text-ink-500 font-medium">{label}</div>
      <div className="mt-1 truncate font-mono text-[12px] text-ink-300">{value}</div>
    </div>
  );
}

function formatTime(value?: string | null) {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function shortId(value: string) {
  return value.length > 16 ? `${value.slice(0, 8)}...${value.slice(-4)}` : value;
}
