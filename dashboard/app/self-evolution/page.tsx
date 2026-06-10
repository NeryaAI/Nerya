"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Advanced, Card, Empty, ErrorBanner, Json, PageBody, PageHeader, Pill } from "../../components/Page";
import { SwitchIndicator } from "../../components/SwitchControl";
import {
  CheckIcon,
  EvolutionIcon,
  SearchIcon,
  ShieldCheckIcon,
  SparkIcon,
  WrenchIcon,
  XIcon,
} from "../../components/icons";
import { clientApi } from "../../lib/clientApi";
import type {
  EvolutionAsset,
  EvolutionAssetCandidate,
  EvolutionConfigSnapshot,
  EvolutionProcessArtifact,
  EvolutionProcessTrace,
  EvolutionTimelineEnvelope,
  EvolutionTimelineItem,
} from "../../lib/evolutionTypes";

type Tab = "timeline" | "assets" | "proposals";

const TAB_IDS: Tab[] = ["timeline", "assets", "proposals"];
const HISTORY_PAGE_SIZE = 10;

function toneForStatus(status?: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  const s = String(status || "").toLowerCase();
  if (["applied", "approved", "passed", "promoted", "safe", "ok", "candidate"].includes(s)) return "ok";
  if (["warn", "warning", "pending_review", "draft", "proposed", "not_run"].includes(s)) return "warn";
  if (["critical", "danger", "blocked", "failed", "rejected", "rolled_back"].includes(s)) return "danger";
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
      setEnvelope(out);
      setSelectedId((current) =>
        current && out.timeline.some((item) => item.id === current)
          ? current
          : out.timeline[0]?.id ?? null,
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
  const summary = envelope?.summary;
  const assets = envelope?.raw.assets ?? [];
  const candidates = envelope?.raw.candidates ?? [];
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

  async function validateProposal(proposalId: string) {
    setBusy(proposalId);
    setError(null);
    try {
      const out = await clientApi.evolutionValidationRun({
        proposal_id: proposalId,
        dry_run: true,
      });
      setNotice(t("validationDryRunResult", { id: proposalId, status: out.ok ? t("safeWord") : t("blockedWord") }));
      await load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
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

        <Filters
          strategy={strategy}
          query={query}
          busy={busy}
          onStrategy={setStrategy}
          onQuery={setQuery}
          onFilter={() => void load(false)}
          onTuning={runTuningDryRun}
        />

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

        <LearningChain timeline={timeline} summary={summary} />

        <div className="overflow-x-auto border-b border-brand-500/10">
          {TAB_IDS.map((id) => {
            const labelKey = id === "timeline" ? "tabHistory" : id === "assets" ? "tabAssets" : "tabProposals";
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

        {tab === "timeline" ? (
          timeline.length ? (
            <TimelineConsole
              items={timeline}
              selected={selected}
              selectedId={selectedId}
              onSelect={setSelectedId}
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
            busy={busy}
            onPromote={promote}
            onReject={reject}
          />
        ) : null}

        {tab === "proposals" ? (
          <ProposalsPanel
            proposals={openProposals}
            busy={busy}
            onValidate={validateProposal}
          />
        ) : null}

        {envelope ? (
          <Advanced
            title={t("tabConfig")}
            storageKey="nerya.evolution.advanced.config"
          >
            <ConfigPanel config={envelope.config} />
          </Advanced>
        ) : null}
        {envelope ? (
          <Advanced
            title={t("tabDebug")}
            storageKey="nerya.evolution.advanced.debug"
          >
            <DebugPanel envelope={envelope} />
          </Advanced>
        ) : null}
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

function Stat({
  label,
  value,
  tone = "brand",
  compact = false,
}: {
  label: string;
  value: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "danger" | "brand";
  compact?: boolean;
}) {
  const color = {
    neutral: "text-white",
    ok: "text-accent-300",
    warn: "text-warn",
    danger: "text-danger",
    brand: "text-brand-200",
  }[tone];
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-950/35 p-3">
      <div className="text-[11px] text-ink-500 font-medium">{label}</div>
      <div className={`mt-1 font-mono ${compact ? "text-sm" : "text-xl"} ${color}`}>{value}</div>
    </div>
  );
}

function LearningChain({
  timeline,
  summary,
}: {
  timeline: EvolutionTimelineItem[];
  summary?: EvolutionTimelineEnvelope["summary"];
}) {
  const t = useTranslations("selfEvolution");
  const stages = [
    { id: "signal", label: t("stageSignals"), hint: t("stageSignalsHint") },
    { id: "reflection", label: t("stageReflection"), hint: t("stageReflectionHint") },
    { id: "proposal", label: t("stageProposal"), hint: t("stageProposalHint") },
    { id: "validation", label: t("stageValidation"), hint: t("stageValidationHint") },
    { id: "asset", label: t("stageAsset"), hint: t("stageAssetHint") },
  ];
  const blocked = (summary?.blocked_candidates ?? 0) + (summary?.blocked_validation_plans ?? 0);
  const lastActivity = summary?.last_activity_ts ? formatTime(summary.last_activity_ts) : null;
  return (
    <div>
      <div className="grid gap-2 md:grid-cols-5">
        {stages.map((stage, idx) => {
          const count = timeline.filter((item) => item.stage === stage.id).length;
          return (
            <div key={stage.id} className="relative rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
              {idx < stages.length - 1 ? (
                <div className="pointer-events-none absolute right-[-10px] top-1/2 hidden h-px w-5 bg-brand-500/25 md:block" />
              ) : null}
              <div className="flex items-center justify-between gap-2">
                <Pill tone={count ? stageTone(stage.id) : "neutral"}>{count}</Pill>
                <span className="text-[11px] text-ink-500 font-medium">{stage.hint}</span>
              </div>
              <div className="mt-2 text-sm font-medium text-ink-100">{stage.label}</div>
            </div>
          );
        })}
      </div>
      {(blocked || lastActivity) ? (
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-500">
          {blocked ? (
            <span>
              {t("blocked")}: <span className="font-mono text-danger">{blocked}</span>
            </span>
          ) : null}
          {lastActivity ? (
            <span>
              {t("lastActivity")}: <span className="font-mono text-ink-300">{lastActivity}</span>
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
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

function TimelineConsole({
  items,
  selected,
  selectedId,
  onSelect,
}: {
  items: EvolutionTimelineItem[];
  selected: EvolutionTimelineItem | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(460px,560px)] xl:items-stretch">
      <TimelineHistoryPanel
        items={items}
        selectedId={selectedId}
        onSelect={onSelect}
      />
      <div className="min-w-0" data-testid="timeline-detail-panel">
        <TimelineDetail item={selected} />
      </div>
    </div>
  );
}

function TimelineHistoryPanel({
  items,
  selectedId,
  onSelect,
}: {
  items: EvolutionTimelineItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
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
      className="card card-hover min-w-0 overflow-hidden xl:flex xl:h-full xl:min-h-0 xl:flex-col xl:self-stretch"
      data-testid="timeline-history-panel"
    >
      <div className="card-head">
        <div className="min-w-0">
          <h3 className="card-title break-words">{t("historyTitle", { count: items.length })}</h3>
          <p className="card-subtle mt-1 break-words">{t("historyDesc")}</p>
        </div>
      </div>
      <div className="px-5 py-4 xl:flex xl:min-h-0 xl:flex-1 xl:flex-col">
        <div className="embedded-list-scroll-lg divide-y divide-brand-500/10 xl:min-h-0 xl:flex-1 xl:max-h-none">
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
      <div className="mt-2 flex flex-wrap gap-2 font-mono text-[11px] text-ink-500">
        {item.proposal_id ? <span>proposal:{shortId(item.proposal_id)}</span> : null}
        {item.validation_plan_id ? <span>validation:{shortId(item.validation_plan_id)}</span> : null}
        {item.evidence_refs?.slice(0, 2).map((ref) => <span key={ref}>{ref}</span>)}
      </div>
    </button>
  );
}

function TimelineDetail({ item }: { item: EvolutionTimelineItem | null }) {
  const t = useTranslations("selfEvolution");
  if (!item) {
    return (
      <Card title={t("timelineDetail")}>
        <Empty title={t("noItemSelected")} subtitle={t("selectRow")} />
      </Card>
    );
  }
  const process = item.process;
  return (
    <Card
      title={t("timelineDetail")}
      actions={<Pill tone={toneForStatus(item.status)}>{item.status || t("unknown")}</Pill>}
    >
      <div className="space-y-4 text-sm">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={stageTone(item.stage)}>{item.stage}</Pill>
            <span className="font-mono text-[11px] text-ink-500">{item.record_id}</span>
          </div>
          <h3 className="mt-3 text-lg font-semibold text-ink-50">{item.title}</h3>
          <p className="mt-2 text-ink-300">{item.summary || t("noSummary")}</p>
        </div>

        <ProcessStatus process={process} />
        <AgentLearningFlow item={item} process={process} />
        <DetailRow label={t("whyTriggered")}>{item.why || t("noTriggerExplanation")}</DetailRow>
        <DetailRow label={t("evidence")}>
          <TokenList values={item.evidence_refs ?? []} empty={t("noEvidence")} />
        </DetailRow>
        <DetailRow label={t("generatedProposal")}>
          {item.proposal_id ? (
            <span className="font-mono text-ink-100">{item.proposal_id}</span>
          ) : (
            <span className="text-ink-500">{t("noLinkedProposal")}</span>
          )}
        </DetailRow>
        <DetailRow label={t("validationResult")}>
          {item.validation_plan_id || item.validation_status ? (
            <div className="space-y-1">
              {item.validation_plan_id ? <div className="font-mono text-ink-100">{item.validation_plan_id}</div> : null}
              {item.validation_status ? <Pill tone={toneForStatus(item.validation_status)}>{item.validation_status}</Pill> : null}
              <TokenList values={item.blocked_reasons ?? []} tone="danger" empty="" />
            </div>
          ) : (
            <span className="text-ink-500">{t("noValidationPlan")}</span>
          )}
        </DetailRow>
        <DetailRow label={t("assetOrOutcome")}>
          <div className="space-y-2">
            {item.outcome ? <Pill tone={toneForStatus(item.outcome)}>{item.outcome}</Pill> : null}
            <TokenList values={item.asset_ids ?? []} empty={item.outcome ? "" : t("noAssetLinked")} />
            {typeof item.outcome_score === "number" ? (
              <div className="font-mono text-[12px] text-ink-400">{t("score", { score: item.outcome_score })}</div>
            ) : null}
          </div>
        </DetailRow>
        <DetailRow label={t("nextStep")}>{item.next_step || t("reviewEvidence")}</DetailRow>

        <details className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
          <summary className="cursor-pointer text-[12px] text-ink-300">{t("rawRecord")}</summary>
          <div className="mt-3">
            <Json value={item.raw ?? item} />
          </div>
        </details>
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

type AgentFlowLabels = {
  trigger: string;
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
  proposedWrite: (path: string) => string;
};

function AgentLearningFlow({
  item,
  process,
}: {
  item: EvolutionTimelineItem;
  process?: EvolutionProcessTrace;
}) {
  const t = useTranslations("selfEvolution");
  const labels: AgentFlowLabels = {
    trigger: t("flowTrigger"),
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
    proposedWrite: (path: string) => t("flowProposedWrite", { path }),
  };
  const sectionArtifacts = (process?.sections ?? []).flatMap((section) =>
    section.artifacts.map((artifact, index) => ({ sectionId: section.id, sectionTitle: section.title, artifact, index })),
  );
  const fallbackArtifacts = sectionArtifacts.length
    ? []
    : (process?.artifacts ?? []).map((artifact, index) => ({ sectionId: "artifacts", sectionTitle: labels.evidence, artifact, index }));
  const artifactRefs = [...sectionArtifacts, ...fallbackArtifacts].slice(0, 32);
  const entries: AgentFlowEntry[] = [
    {
      id: `${item.id}:trigger`,
      role: "trigger",
      label: labels.trigger,
      title: item.title || t("flowRecord"),
      detail: item.why || item.summary || t("noTriggerExplanation"),
      badges: [
        item.strategy_id ? `strategy:${item.strategy_id}` : "",
        item.proposal_id ? `proposal:${shortId(item.proposal_id)}` : "",
        item.validation_plan_id ? `validation:${shortId(item.validation_plan_id)}` : "",
      ].filter(Boolean),
    },
  ];

  for (const { sectionId, sectionTitle, artifact, index } of artifactRefs) {
    const role = flowRoleForArtifact(artifact, sectionId);
    const path = displayPathForArtifact(artifact);
    entries.push({
      id: `${item.id}:${sectionId}:${artifact.id}:${index}`,
      role,
      label: flowLabel(role, labels),
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

  return (
    <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3" data-testid="agent-learning-flow">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-[11px] text-ink-500 font-medium">{t("agentFlow")}</div>
          <div className="mt-1 text-[12px] text-ink-400">{t("agentFlowDesc")}</div>
        </div>
        <Pill tone={artifactRefs.length ? "brand" : "neutral"}>{entries.length}</Pill>
      </div>
      <ol className="mt-4 space-y-3">
        {entries.map((entry) => (
          <AgentFlowBubble key={entry.id} entry={entry} />
        ))}
      </ol>
      {!artifactRefs.length ? (
        <div className="mt-3 text-sm text-ink-500">{t("flowNoArtifacts")}</div>
      ) : null}
    </section>
  );
}

function AgentFlowBubble({ entry }: { entry: AgentFlowEntry }) {
  const isAgentOutput = entry.role === "agent_output" || entry.role === "file_change";
  return (
    <li className={["flex", isAgentOutput ? "justify-end" : "justify-start"].join(" ")}>
      <div
        className={[
          "min-w-0 max-w-full rounded-lg border px-3 py-2.5 text-sm",
          isAgentOutput ? "w-[92%] border-accent-500/20 bg-accent-500/[0.06]" : "w-[92%] border-brand-500/15 bg-ink-900/45",
        ].join(" ")}
      >
        <div className="flex flex-wrap items-center gap-2">
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
        {entry.detail ? (
          <pre className="embedded-scroll mt-2 max-h-64 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
            {entry.detail}
          </pre>
        ) : null}
      </div>
    </li>
  );
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

function flowTitle(
  role: AgentFlowRole,
  artifact: EvolutionProcessArtifact,
  sectionTitle: string,
  path: string | undefined,
  labels: AgentFlowLabels,
) {
  if (role === "agent_input") {
    return artifact.kind === "input" ? labels.inputTitle : labels.promptTitle;
  }
  if (role === "agent_output") return labels.outputTitle;
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

function ProcessStatus({ process }: { process?: EvolutionProcessTrace }) {
  const t = useTranslations("selfEvolution");
  const hasChanges = Boolean(
    process?.has_file_changes || (process?.artifacts ?? []).some((artifact) => isProposedChangeArtifact(artifact)),
  );
  const flags = [
    { label: t("processPrompt"), active: Boolean(process?.has_prompt), tone: "brand" as const },
    { label: t("processInputs"), active: Boolean(process?.has_inputs), tone: "brand" as const },
    { label: t("processChanges"), active: hasChanges, tone: "ok" as const },
    { label: t("processDocs"), active: Boolean(process?.has_generated_docs), tone: "ok" as const },
    { label: t("processValidation"), active: Boolean(process?.has_validation), tone: "warn" as const },
    { label: t("processOutput"), active: Boolean(process?.has_outputs), tone: "ok" as const },
  ];
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
      {flags.map((flag) => (
        <div key={flag.label} className="rounded-lg border border-brand-500/10 bg-ink-950/25 px-2 py-2">
          <div className="text-[11px] text-ink-500 font-medium">{flag.label}</div>
          <div className="mt-1">
            <Pill tone={flag.active ? flag.tone : "neutral"}>{flag.active ? t("linked") : t("none")}</Pill>
          </div>
        </div>
      ))}
    </div>
  );
}

function ProcessTrace({ sections }: { sections: EvolutionProcessTrace["sections"] }) {
  return (
    <div className="space-y-3">
      {sections.map((section) => (
        <section key={section.id} className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-[11px] text-ink-500 font-medium">{section.title}</div>
              {section.summary ? <div className="mt-1 text-[12px] text-ink-400">{section.summary}</div> : null}
            </div>
            <Pill tone="neutral">{section.artifacts.length}</Pill>
          </div>
          <div className="mt-3 space-y-3">
            {section.artifacts.map((artifact) => (
              <ArtifactPreview key={`${section.id}:${artifact.id}:${artifact.path ?? ""}`} artifact={artifact} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function ArtifactPreview({ artifact }: { artifact: EvolutionProcessArtifact }) {
  const preview = String(artifact.preview || "");
  return (
    <div className="rounded-md border border-brand-500/15 bg-ink-900/50 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={artifactTone(artifact.kind)}>{artifact.kind || "artifact"}</Pill>
        <span className="font-medium text-ink-100">{artifact.title}</span>
        {artifact.language ? <span className="font-mono text-[11px] text-ink-500">{artifact.language}</span> : null}
        {typeof artifact.size === "number" ? <span className="font-mono text-[11px] text-ink-500">{formatBytes(artifact.size)}</span> : null}
        {artifact.redacted ? <Pill tone="neutral">redacted</Pill> : null}
        {artifact.truncated ? <Pill tone="warn">truncated</Pill> : null}
      </div>
      {artifact.path ? (
        <div className="mt-1 truncate font-mono text-[11px] text-ink-500">{artifact.path}</div>
      ) : null}
      <pre className="embedded-scroll mt-2 max-h-72 whitespace-pre-wrap break-words rounded-md border border-ink-700/70 bg-ink-950/70 p-2 text-[11px] leading-relaxed text-ink-200">
        {preview || "No preview available."}
      </pre>
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

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="text-[11px] text-ink-500 font-medium">{label}</div>
      <div className="mt-2 text-ink-300">{children}</div>
    </div>
  );
}

function TokenList({
  values,
  empty,
  tone = "neutral",
}: {
  values: string[];
  empty: string;
  tone?: "neutral" | "danger";
}) {
  if (!values.length) {
    return empty ? <span className="text-ink-500">{empty}</span> : null;
  }
  const color = tone === "danger" ? "border-danger/25 text-danger" : "border-brand-500/10 text-ink-300";
  return (
    <div className="flex flex-wrap gap-1.5">
      {values.map((value) => (
        <span key={value} className={`rounded border bg-ink-950/50 px-2 py-1 font-mono text-[11px] ${color}`}>
          {value}
        </span>
      ))}
    </div>
  );
}

function AssetsPanel({
  assets,
  candidates,
  busy,
  onPromote,
  onReject,
}: {
  assets: EvolutionAsset[];
  candidates: EvolutionAssetCandidate[];
  busy: string;
  onPromote: (id: string) => Promise<void>;
  onReject: (id: string) => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1fr_420px]">
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
                <TokenList values={(asset.evidence_refs ?? []) as string[]} empty="" />
              </div>
            ))}
          </div>
        )}
      </Card>
      <Card title={t("candidates", { count: candidates.length })} description={t("candidatesDesc")}>
        {!candidates.length ? (
          <Empty title={t("noCandidates")} subtitle={t("noCandidatesSub")} />
        ) : (
          <div className="space-y-3">
            {candidates.map((candidate) => (
              <div key={candidate.id} className="rounded-lg border border-brand-500/10 bg-ink-950/35 p-3 text-sm">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <Pill tone={candidate.kind === "gene" ? "brand" : "ok"}>{candidate.kind}</Pill>
                      <span className="font-mono text-ink-100">{candidate.id}</span>
                    </div>
                    <div className="mt-2 text-ink-300">{candidate.summary}</div>
                  </div>
                  <Pill tone={candidate.safe_to_promote ? "ok" : "danger"}>
                    {candidate.safe_to_promote ? t("safe") : t("blockedStatus")}
                  </Pill>
                </div>
                <TokenList values={candidate.blocked_reasons} tone="danger" empty="" />
                <div className="mt-3 flex justify-end gap-2">
                  <button className="btn btn-ghost" disabled={busy === candidate.id} onClick={() => void onReject(candidate.id)}>
                    <XIcon size={14} />
                    {t("reject")}
                  </button>
                  <button
                    className="btn btn-primary"
                    disabled={busy === candidate.id || !candidate.safe_to_promote}
                    onClick={() => void onPromote(candidate.id)}
                  >
                    <CheckIcon size={14} />
                    {t("promote")}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

function ProposalsPanel({
  proposals,
  busy,
  onValidate,
}: {
  proposals: Array<Record<string, unknown>>;
  busy: string;
  onValidate: (id: string) => Promise<void>;
}) {
  const t = useTranslations("selfEvolution");
  if (!proposals.length) {
    return (
      <Card title={t("openProposalsEmpty")}>
        <Empty title={t("openProposalsEmpty")} subtitle={t("openProposalsEmptySub")} />
      </Card>
    );
  }
  return (
    <Card title={t("openProposalsTitle", { count: proposals.length })} description={t("openProposalsDesc")}>
      <div className="embedded-list-scroll-lg divide-y divide-brand-500/10">
        {proposals.map((proposal) => {
          const id = String(proposal.id || "");
          const evidence = Array.isArray(proposal.evidence_refs) ? proposal.evidence_refs.map(String) : [];
          return (
            <div key={id} className="py-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Pill tone={toneForStatus(String(proposal.state || ""))}>
                  {String(proposal.state || t("unknown"))}
                </Pill>
                <span className="font-mono text-ink-100">{id}</span>
                <span className="text-[11px] text-ink-500">{String(proposal.kind || "")}</span>
              </div>
              <div className="mt-1 text-ink-300">{String(proposal.summary || "")}</div>
              <div className="mt-2 grid gap-2 md:grid-cols-2">
                <DetailMini label={t("validation")} value={String(proposal.validation_plan_id || t("missing"))} />
                <DetailMini label={t("target")} value={String(proposal.target || t("workspaceProposal"))} />
              </div>
              <div className="mt-2">
                <TokenList values={evidence} empty="" />
              </div>
              <div className="mt-3 flex justify-end">
                <button className="btn btn-ghost" disabled={busy === id} onClick={() => void onValidate(id)}>
                  <ShieldCheckIcon size={14} />
                  {t("validate")}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </Card>
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

function ConfigPanel({ config }: { config: EvolutionConfigSnapshot }) {
  const t = useTranslations("selfEvolution");
  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1fr_420px]">
      <Card title={t("evolutionGates")} description={t("evolutionGatesDesc")}>
        <div className="grid gap-3 md:grid-cols-2">
          <ConfigSwitch
            label={t("lifecycleHooks")}
            value={config.hooks.enabled}
            detail={t("sources", { list: config.hooks.sources.join(", ") })}
          />
          <ConfigSwitch
            label={t("memoryQualityGate")}
            value={config.memory_quality_gate.enabled}
            detail={t("minimumScore", { score: config.memory_quality_gate.minimum_score })}
          />
          <ConfigSwitch
            label={t("evidenceRequired")}
            value={config.memory_quality_gate.requires_evidence_refs}
            detail={t("evidenceRequiredDesc")}
          />
          <ConfigSwitch
            label={t("secretGuard")}
            value={config.memory_quality_gate.blocks_possible_secrets}
            detail={t("secretGuardDesc")}
          />
          <ConfigSwitch
            label={t("validationDryRunOnly")}
            value={config.validation.dry_run_only}
            detail={t("validationDryRunDesc")}
          />
          <ConfigSwitch
            label={t("shellExecution")}
            value={config.validation.execution_enabled}
            invert
            detail={t("shellExecutionDesc")}
          />
        </div>
        <div className="mt-4 rounded-lg border border-brand-500/10 bg-ink-950/30 p-3 text-sm">
          <div className="text-[11px] text-ink-500 font-medium">{t("allowedSteps")}</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {config.validation.allowed_step_types.map((step) => (
              <Pill key={step} tone="neutral">{step}</Pill>
            ))}
          </div>
        </div>
      </Card>
      <Card title={t("perStrategyTuning")} description={t("perStrategyDesc")}>
        <div className="mb-3 grid grid-cols-2 gap-3">
          <Stat label={t("strategies")} value={config.strategy_tuning.total_strategies} />
          <Stat label={t("enabled")} value={config.strategy_tuning.enabled_strategies} tone="ok" />
        </div>
        {!config.strategy_tuning.strategies.length ? (
          <Empty title={t("noTuningConfig")} />
        ) : (
          <div className="embedded-list-scroll divide-y divide-brand-500/10">
            {config.strategy_tuning.strategies.map((row) => (
              <div key={row.strategy_id} className="py-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone={row.enabled ? "ok" : "neutral"}>{row.enabled ? t("enabledStatus") : t("disabledStatus")}</Pill>
                  <span className="font-mono text-ink-100">{row.strategy_id}</span>
                </div>
                <div className="mt-1 text-[12px] text-ink-500">
                  {listConfigText(row.objectives) || t("objectivesNotConfigured")}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

function ConfigSwitch({
  label,
  value,
  detail,
  invert = false,
}: {
  label: string;
  value: boolean;
  detail: string;
  invert?: boolean;
}) {
  const ok = invert ? !value : value;
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-950/35 p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="font-medium text-ink-100">{label}</div>
        <SwitchIndicator
          checked={value}
          label={label}
          tone={ok ? "accent" : "danger"}
          size="sm"
          toneWhenOff
        />
      </div>
      <div className="mt-2 text-[12px] text-ink-500">{detail}</div>
    </div>
  );
}

function DebugPanel({ envelope }: { envelope: EvolutionTimelineEnvelope }) {
  const t = useTranslations("selfEvolution");
  return (
    <Card title={t("rawEnvelope")} description={t("rawEnvelopeDesc")}>
      <Json value={envelope} />
    </Card>
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

function listConfigText(value: unknown) {
  if (Array.isArray(value)) return value.map(String).join(", ");
  if (value && typeof value === "object") return Object.values(value as Record<string, unknown>).flat().map(String).join(", ");
  return "";
}
