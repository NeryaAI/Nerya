"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Card, Empty, ErrorBanner, Json, PageBody, PageHeader, Pill } from "../../components/Page";
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

type Tab = "timeline" | "assets" | "proposals" | "config" | "debug";

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "timeline", label: "History" },
  { id: "assets", label: "Assets" },
  { id: "proposals", label: "Proposals" },
  { id: "config", label: "Config" },
  { id: "debug", label: "Debug" },
];

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
  const [tab, setTab] = useState<Tab>("timeline");
  const [envelope, setEnvelope] = useState<EvolutionTimelineEnvelope | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [strategy, setStrategy] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
        limit: 160,
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
      setNotice(`Reflection completed: ${String(out.count ?? out.proposals ?? "done")}.`);
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
      setError("Enter a strategy_id before running a tuning dry-run.");
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
      setNotice(`Tuning dry-run for ${sid}: ${out.ok ? "completed" : "blocked"}.`);
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
      setNotice(`Promoted candidate ${candidateId}.`);
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
      setNotice(`Rejected candidate ${candidateId}.`);
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
      setNotice(`Validation dry-run for ${proposalId}: ${out.ok ? "safe" : "blocked"}.`);
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
        eyebrow="Learning control plane"
        title="Self Evolution"
        description="Audit prompts, inputs, generated docs, proposals, validation gates, outcomes, and reusable learning assets."
        actions={
          <>
            <button className="btn btn-ghost" onClick={() => void load(false)} disabled={Boolean(busy)}>
              Refresh
            </button>
            <button className="btn btn-primary" onClick={() => void load(true)} disabled={Boolean(busy)}>
              <EvolutionIcon size={14} />
              {busy === "collect" ? "Collecting..." : "Collect signals"}
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
          onReflect={runReflection}
          onTuning={runTuningDryRun}
        />

        <SummaryStrip summary={summary} />
        <LearningChain timeline={timeline} />

        <div className="overflow-x-auto border-b border-white/5">
          {TABS.map((item) => (
            <button
              key={item.id}
              className={[
                "px-3 py-2 text-[12px]",
                tab === item.id ? "border-b border-brand-300 text-white" : "text-ink-400",
              ].join(" ")}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
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
              hasStrategy={Boolean(strategy.trim())}
              onCollect={() => void load(true)}
              onReflect={runReflection}
              onTuning={runTuningDryRun}
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

        {tab === "config" && envelope ? <ConfigPanel config={envelope.config} /> : null}
        {tab === "debug" && envelope ? <DebugPanel envelope={envelope} /> : null}
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
  onReflect,
  onTuning,
}: {
  strategy: string;
  query: string;
  busy: string;
  onStrategy: (value: string) => void;
  onQuery: (value: string) => void;
  onFilter: () => void;
  onReflect: () => Promise<void>;
  onTuning: () => Promise<void>;
}) {
  return (
    <Card>
      <div className="grid gap-3 lg:grid-cols-[minmax(180px,260px)_1fr_auto] lg:items-end">
        <label className="text-[12px] text-ink-300">
          Strategy
          <input
            value={strategy}
            onChange={(e) => onStrategy(e.target.value)}
            className="input-dark mt-1 font-mono"
            placeholder="optional strategy_id"
          />
        </label>
        <label className="text-[12px] text-ink-300">
          Search history, evidence, proposals, assets
          <input
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            className="input-dark mt-1"
            placeholder="slippage, noop, validation, proposal id"
          />
        </label>
        <div className="flex flex-wrap justify-start gap-2 lg:justify-end">
          <button className="btn btn-ghost" onClick={onFilter} disabled={Boolean(busy)}>
            <SearchIcon size={14} />
            Filter
          </button>
          <button className="btn btn-ghost" onClick={() => void onReflect()} disabled={Boolean(busy)}>
            <SparkIcon size={14} />
            {busy === "reflect" ? "Reflecting..." : "Run reflection"}
          </button>
          <button className="btn btn-ghost" onClick={() => void onTuning()} disabled={Boolean(busy)}>
            <WrenchIcon size={14} />
            {busy === "tuning" ? "Running..." : "Tuning dry-run"}
          </button>
        </div>
      </div>
    </Card>
  );
}

function SummaryStrip({ summary }: { summary?: EvolutionTimelineEnvelope["summary"] }) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <Stat label="Signals" value={summary?.signals ?? 0} />
      <Stat label="Open proposals" value={summary?.open_proposals ?? 0} tone={summary?.open_proposals ? "warn" : "neutral"} />
      <Stat label="Validation plans" value={summary?.validation_plans ?? 0} />
      <Stat label="Reusable assets" value={summary?.assets ?? 0} tone="ok" />
      <Stat label="Blocked" value={(summary?.blocked_candidates ?? 0) + (summary?.blocked_validation_plans ?? 0)} tone="danger" />
      <Stat label="Last activity" value={summary?.last_activity_ts ? formatTime(summary.last_activity_ts) : "none"} compact />
    </div>
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
    warn: "text-[#f5a524]",
    danger: "text-[#ef4560]",
    brand: "text-brand-200",
  }[tone];
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-950/35 p-3">
      <div className="text-[10px] uppercase tracking-[0.16em] text-ink-500">{label}</div>
      <div className={`mt-1 font-mono ${compact ? "text-sm" : "text-xl"} ${color}`}>{value}</div>
    </div>
  );
}

function LearningChain({ timeline }: { timeline: EvolutionTimelineItem[] }) {
  const stages = [
    { id: "signal", label: "Signals", hint: "why it triggered" },
    { id: "reflection", label: "Reflection", hint: "pattern detected" },
    { id: "proposal", label: "Proposal", hint: "what may change" },
    { id: "validation", label: "Validation", hint: "gates and evidence" },
    { id: "asset", label: "Asset", hint: "reusable learning" },
  ];
  return (
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
              <span className="text-[10px] uppercase tracking-[0.14em] text-ink-500">{stage.hint}</span>
            </div>
            <div className="mt-2 text-sm font-medium text-ink-100">{stage.label}</div>
          </div>
        );
      })}
    </div>
  );
}

function EmptyHistory({
  busy,
  hasStrategy,
  onCollect,
  onReflect,
  onTuning,
  onOpenProposals,
}: {
  busy: string;
  hasStrategy: boolean;
  onCollect: () => void;
  onReflect: () => Promise<void>;
  onTuning: () => Promise<void>;
  onOpenProposals: () => void;
}) {
  return (
    <Card title="No evolution history yet">
      <div className="grid gap-4 lg:grid-cols-[1fr_360px] lg:items-center">
        <Empty
          title="No signal, proposal, or asset matched the current filter."
          subtitle="Start by collecting signals, then run a reflection pass to convert repeated patterns into proposal-first changes."
        />
        <div className="flex flex-wrap justify-center gap-2 lg:justify-end">
          <button className="btn btn-primary" onClick={onCollect} disabled={Boolean(busy)}>
            <EvolutionIcon size={14} />
            Collect signals
          </button>
          <button className="btn btn-ghost" onClick={() => void onReflect()} disabled={Boolean(busy)}>
            Run reflection
          </button>
          <button className="btn btn-ghost" onClick={() => void onTuning()} disabled={Boolean(busy) || !hasStrategy}>
            Tuning dry-run
          </button>
          <button className="btn btn-ghost" onClick={onOpenProposals}>
            Open proposals
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
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(460px,560px)]">
      <Card
        title={`Evolution history (${items.length})`}
        description="Newest first. Select a row to inspect why it happened, what evidence exists, and what the next governed step is."
      >
        <div className="embedded-list-scroll-lg divide-y divide-brand-500/10">
          {items.map((item) => (
            <TimelineRow
              key={item.id}
              item={item}
              selected={selectedId === item.id}
              onSelect={() => onSelect(item.id)}
            />
          ))}
        </div>
      </Card>
      <TimelineDetail item={selected} />
    </div>
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
        <Pill tone={toneForStatus(item.status)}>{item.status || "unknown"}</Pill>
        {item.strategy_id ? <Pill tone="neutral">{item.strategy_id}</Pill> : null}
        <span className="ml-auto font-mono text-[11px] text-ink-500">{formatTime(item.ts)}</span>
      </div>
      <div className="mt-2 font-medium text-ink-100">{item.title}</div>
      <div className="mt-1 line-clamp-2 text-ink-400">{item.summary || item.why || "No summary."}</div>
      <div className="mt-2 flex flex-wrap gap-2 font-mono text-[11px] text-ink-500">
        {item.proposal_id ? <span>proposal:{shortId(item.proposal_id)}</span> : null}
        {item.validation_plan_id ? <span>validation:{shortId(item.validation_plan_id)}</span> : null}
        {item.evidence_refs?.slice(0, 2).map((ref) => <span key={ref}>{ref}</span>)}
      </div>
    </button>
  );
}

function TimelineDetail({ item }: { item: EvolutionTimelineItem | null }) {
  if (!item) {
    return (
      <Card title="Timeline detail">
        <Empty title="No item selected" subtitle="Select a history row to inspect its evidence chain." />
      </Card>
    );
  }
  const process = item.process;
  const sections = process?.sections ?? [];
  return (
    <Card
      title="Timeline detail"
      actions={<Pill tone={toneForStatus(item.status)}>{item.status || "unknown"}</Pill>}
    >
      <div className="space-y-4 text-sm">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={stageTone(item.stage)}>{item.stage}</Pill>
            <span className="font-mono text-[11px] text-ink-500">{item.record_id}</span>
          </div>
          <h3 className="mt-3 text-lg font-semibold text-ink-50">{item.title}</h3>
          <p className="mt-2 text-ink-300">{item.summary || "No summary."}</p>
        </div>

        <ProcessStatus process={process} />
        <DetailRow label="Why triggered">{item.why || "No trigger explanation recorded."}</DetailRow>
        <DetailRow label="Evidence">
          <TokenList values={item.evidence_refs ?? []} empty="No evidence refs recorded." />
        </DetailRow>
        <DetailRow label="Generated proposal">
          {item.proposal_id ? (
            <span className="font-mono text-ink-100">{item.proposal_id}</span>
          ) : (
            <span className="text-ink-500">No linked proposal.</span>
          )}
        </DetailRow>
        <DetailRow label="Validation result">
          {item.validation_plan_id || item.validation_status ? (
            <div className="space-y-1">
              {item.validation_plan_id ? <div className="font-mono text-ink-100">{item.validation_plan_id}</div> : null}
              {item.validation_status ? <Pill tone={toneForStatus(item.validation_status)}>{item.validation_status}</Pill> : null}
              <TokenList values={item.blocked_reasons ?? []} tone="danger" empty="" />
            </div>
          ) : (
            <span className="text-ink-500">No validation plan linked.</span>
          )}
        </DetailRow>
        <DetailRow label="Asset or outcome">
          <div className="space-y-2">
            {item.outcome ? <Pill tone={toneForStatus(item.outcome)}>{item.outcome}</Pill> : null}
            <TokenList values={item.asset_ids ?? []} empty={item.outcome ? "" : "No asset linked."} />
            {typeof item.outcome_score === "number" ? (
              <div className="font-mono text-[12px] text-ink-400">score {item.outcome_score}</div>
            ) : null}
          </div>
        </DetailRow>
        <DetailRow label="Next step">{item.next_step || "Review linked evidence."}</DetailRow>

        {sections.length ? (
          <ProcessTrace sections={sections} />
        ) : (
          <DetailRow label="Process artifacts">
            <span className="text-ink-500">No prompt, input, generated document, or output artifact is linked to this record yet.</span>
          </DetailRow>
        )}

        <details className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
          <summary className="cursor-pointer text-[12px] text-ink-300">Raw record</summary>
          <div className="mt-3">
            <Json value={item.raw ?? item} />
          </div>
        </details>
      </div>
    </Card>
  );
}

function ProcessStatus({ process }: { process?: EvolutionProcessTrace }) {
  const flags = [
    { label: "Prompt", active: Boolean(process?.has_prompt), tone: "brand" as const },
    { label: "Inputs", active: Boolean(process?.has_inputs), tone: "brand" as const },
    { label: "Docs", active: Boolean(process?.has_generated_docs), tone: "ok" as const },
    { label: "Validation", active: Boolean(process?.has_validation), tone: "warn" as const },
    { label: "Output", active: Boolean(process?.has_outputs), tone: "ok" as const },
  ];
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
      {flags.map((flag) => (
        <div key={flag.label} className="rounded-lg border border-brand-500/10 bg-ink-950/25 px-2 py-2">
          <div className="text-[10px] uppercase tracking-[0.14em] text-ink-500">{flag.label}</div>
          <div className="mt-1">
            <Pill tone={flag.active ? flag.tone : "neutral"}>{flag.active ? "linked" : "none"}</Pill>
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
              <div className="text-[10px] uppercase tracking-[0.16em] text-ink-500">{section.title}</div>
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
    <div className="rounded-md border border-white/10 bg-ink-900/50 p-2.5">
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
  if (k === "output" || k === "document") return "ok";
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
      <div className="text-[10px] uppercase tracking-[0.16em] text-ink-500">{label}</div>
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
  const color = tone === "danger" ? "border-[#ef4560]/25 text-[#ef4560]" : "border-brand-500/10 text-ink-300";
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
  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1fr_420px]">
      <Card
        title={`Genes and capsules (${assets.length})`}
        description="Reusable learning units selected by future memory recall and self-evolution runs."
      >
        {!assets.length ? (
          <Empty title="No assets match" subtitle="Promoted capsules and custom genes will appear here." />
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
      <Card title={`Candidates (${candidates.length})`} description="Promotion records an event and never mutates protected runtime scopes.">
        {!candidates.length ? (
          <Empty title="No candidates pending" subtitle="Asset candidates will appear after reflection, proposal outcomes, or memory gates." />
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
                    {candidate.safe_to_promote ? "safe" : "blocked"}
                  </Pill>
                </div>
                <TokenList values={candidate.blocked_reasons} tone="danger" empty="" />
                <div className="mt-3 flex justify-end gap-2">
                  <button className="btn btn-ghost" disabled={busy === candidate.id} onClick={() => void onReject(candidate.id)}>
                    <XIcon size={14} />
                    Reject
                  </button>
                  <button
                    className="btn btn-primary"
                    disabled={busy === candidate.id || !candidate.safe_to_promote}
                    onClick={() => void onPromote(candidate.id)}
                  >
                    <CheckIcon size={14} />
                    Promote
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
  if (!proposals.length) {
    return (
      <Card title="Open proposals">
        <Empty title="No open proposals" subtitle="Reflection and tuning produce proposal-first changes here before any apply step." />
      </Card>
    );
  }
  return (
    <Card title={`Open proposals (${proposals.length})`} description="Review state, validation plan, evidence refs, and target before using the governed apply flow.">
      <div className="embedded-list-scroll-lg divide-y divide-brand-500/10">
        {proposals.map((proposal) => {
          const id = String(proposal.id || "");
          const evidence = Array.isArray(proposal.evidence_refs) ? proposal.evidence_refs.map(String) : [];
          return (
            <div key={id} className="py-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Pill tone={toneForStatus(String(proposal.state || ""))}>
                  {String(proposal.state || "unknown")}
                </Pill>
                <span className="font-mono text-ink-100">{id}</span>
                <span className="text-[11px] text-ink-500">{String(proposal.kind || "")}</span>
              </div>
              <div className="mt-1 text-ink-300">{String(proposal.summary || "")}</div>
              <div className="mt-2 grid gap-2 md:grid-cols-2">
                <DetailMini label="Validation" value={String(proposal.validation_plan_id || "missing")} />
                <DetailMini label="Target" value={String(proposal.target || "workspace proposal")} />
              </div>
              <div className="mt-2">
                <TokenList values={evidence} empty="" />
              </div>
              <div className="mt-3 flex justify-end">
                <button className="btn btn-ghost" disabled={busy === id} onClick={() => void onValidate(id)}>
                  <ShieldCheckIcon size={14} />
                  Validate
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
      <div className="text-[10px] uppercase tracking-[0.14em] text-ink-500">{label}</div>
      <div className="mt-1 truncate font-mono text-[12px] text-ink-300">{value}</div>
    </div>
  );
}

function ConfigPanel({ config }: { config: EvolutionConfigSnapshot }) {
  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1fr_420px]">
      <Card title="Evolution gates" description="Current runtime guardrails. The dashboard displays policy state; mutating runtime config still belongs in Settings or proposals.">
        <div className="grid gap-3 md:grid-cols-2">
          <ConfigSwitch
            label="Lifecycle hooks"
            value={config.hooks.enabled}
            detail={`Sources: ${config.hooks.sources.join(", ")}`}
          />
          <ConfigSwitch
            label="Memory quality gate"
            value={config.memory_quality_gate.enabled}
            detail={`Minimum score ${config.memory_quality_gate.minimum_score}`}
          />
          <ConfigSwitch
            label="Evidence required"
            value={config.memory_quality_gate.requires_evidence_refs}
            detail="Low-value memories stay as events, not persistent learnings."
          />
          <ConfigSwitch
            label="Secret guard"
            value={config.memory_quality_gate.blocks_possible_secrets}
            detail="Potential secrets are blocked from learning assets."
          />
          <ConfigSwitch
            label="Validation dry-run only"
            value={config.validation.dry_run_only}
            detail="Dashboard validation inspects plans and allowlists only."
          />
          <ConfigSwitch
            label="Shell execution"
            value={config.validation.execution_enabled}
            invert
            detail="Actual validation command execution remains disabled here."
          />
        </div>
        <div className="mt-4 rounded-lg border border-brand-500/10 bg-ink-950/30 p-3 text-sm">
          <div className="text-[10px] uppercase tracking-[0.16em] text-ink-500">Allowed validation step types</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {config.validation.allowed_step_types.map((step) => (
              <Pill key={step} tone="neutral">{step}</Pill>
            ))}
          </div>
        </div>
      </Card>
      <Card title="Per-strategy tuning" description="Self-evolution tuning is configured in each strategy package.">
        <div className="mb-3 grid grid-cols-2 gap-3">
          <Stat label="Strategies" value={config.strategy_tuning.total_strategies} />
          <Stat label="Enabled" value={config.strategy_tuning.enabled_strategies} tone="ok" />
        </div>
        {!config.strategy_tuning.strategies.length ? (
          <Empty title="No strategy tuning config found" />
        ) : (
          <div className="embedded-list-scroll divide-y divide-brand-500/10">
            {config.strategy_tuning.strategies.map((row) => (
              <div key={row.strategy_id} className="py-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone={row.enabled ? "ok" : "neutral"}>{row.enabled ? "enabled" : "disabled"}</Pill>
                  <span className="font-mono text-ink-100">{row.strategy_id}</span>
                </div>
                <div className="mt-1 text-[12px] text-ink-500">
                  {listConfigText(row.objectives) || "Objectives not configured"}
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
  return (
    <Card title="Raw evolution envelope" description="Debug-only payload behind the operator view.">
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
