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
import { useEffect, useMemo, useState } from "react";

import {
  Card,
  Empty,
  ErrorBanner,
  Kpi,
  PageBody,
  PageHeader,
  Pill,
} from "../../../components/Page";
import { SectionTabs } from "../../../components/SectionTabs";
import {
  clientApi,
  type StrategyDetail,
} from "../../../lib/clientApi";
import type {
  StrategyRunRecord,
  StrategyTuningStatusEnvelope,
  StrategyWorkspaceEnvelope,
} from "../../../lib/strategyTypes";
import { StrategyBindCard } from "../../../components/strategies/StrategyBindCard";
import { StrategyHistoryCard } from "../../../components/strategies/StrategyHistoryCard";
import { StrategyEvolutionCard } from "../../../components/strategies/StrategyEvolutionCard";
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

export default function StrategyDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const strategyId = decodeURIComponent(params.id);
  const [detail, setDetail] = useState<StrategyDetail | null>(null);
  const [workspace, setWorkspace] =
    useState<StrategyWorkspaceEnvelope | null>(null);
  const [tuning, setTuning] = useState<StrategyTuningStatusEnvelope | null>(
    null,
  );
  const [files, setFiles] = useState<FilesEnvelope | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
        eyebrow="Strategy workspace"
        description={
          detail?.strategy.path
            ? detail.strategy.path
            : "Loading strategy package…"
        }
        actions={
          <div className="flex items-center gap-2">
            <Link
              href="/strategies"
              className="btn-ghost text-xs"
            >
              ← All strategies
            </Link>
            <Link
              href={`/strategies/${encodeURIComponent(strategyId)}/backtests`}
              className="btn-ghost text-xs"
            >
              Backtests
            </Link>
            <button
              onClick={() => void refresh()}
              disabled={loading}
              className="btn-ghost text-xs"
            >
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        }
      />
      <SectionTabs section="strategy" />
      <PageBody>
        {error && <ErrorBanner error={error} />}
        {notice && (
          <div className="rounded-lg border border-accent-500/30 bg-accent-500/10 px-4 py-2 text-sm text-accent-300">
            {notice}
          </div>
        )}

        {!detail ? (
          <Card title={`Strategy ${strategyId}`}>
            <Empty
              label={
                loading
                  ? `Loading ${strategyId}…`
                  : "Strategy not found in this workspace."
              }
            />
          </Card>
        ) : (
          <>
            <KpiRow
              detail={detail}
              workspace={workspace}
              pnl={pnl}
              lastRun={lastRun}
            />

            {workspace?.ok && (
              <StrategyStatusBar
                envelope={workspace}
                busy={busy}
                disabled={loading}
                onSetBusy={setBusy}
                onRefresh={refresh}
                onError={setError}
                onNotice={setNotice}
              />
            )}

            <StrategyPromotionCard
              strategyId={strategyId}
              status={
                detail?.strategy?.status ??
                (workspace?.manifest as Record<string, unknown> | undefined)?.[
                  "status"
                ] as string | undefined
              }
              onError={setError}
              onNotice={setNotice}
              onRefresh={refresh}
            />

            <StrategyBindCard
              strategyId={strategyId}
              currentAccountId={detail?.strategy?.account_id ?? null}
              currentWalletId={detail?.strategy?.wallet_id ?? null}
              onError={setError}
              onNotice={setNotice}
              onRefresh={refresh}
            />

            <StrategyRiskDecisionsCard strategyId={strategyId} />

            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <StrategyScheduleCard
                strategyId={strategyId}
                status={workspace?.schedules ?? null}
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

            <SubagentPromptsCard
              strategyId={strategyId}
              listed={subagents}
              detail={detail}
              files={subagentFiles}
              onAfterSave={async () => {
                await refresh();
              }}
              onError={setError}
              onNotice={setNotice}
            />

            <StrategyFilesCard
              strategyId={strategyId}
              files={editableFiles}
              onAfterSave={async () => {
                await refresh();
              }}
              onError={setError}
              onNotice={setNotice}
            />

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

            <StrategyHistoryCard
              strategyId={strategyId}
              history={workspace?.history ?? null}
            />

            <ManifestEditorCard
              detail={detail}
              onAfterSave={async () => {
                await refresh();
              }}
              onError={setError}
              onNotice={setNotice}
            />
          </>
        )}
      </PageBody>
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
  const trading = workspace?.schedules?.trading;
  const tuning = workspace?.schedules?.tuning;
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <Kpi
        label="Status"
        value={detail.strategy.status}
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
            mode <span className="text-ink-200">{detail.strategy.mode}</span>
          </>
        }
      />
      <Kpi
        label="Realised PnL"
        value={
          pnl.realised_usd === 0
            ? "$0.00"
            : `${pnl.realised_usd >= 0 ? "+" : ""}$${pnl.realised_usd.toFixed(
                2,
              )}`
        }
        tone={
          pnl.realised_usd > 0
            ? "ok"
            : pnl.realised_usd < 0
              ? "danger"
              : "neutral"
        }
        delta={`${pnl.fills} fills · ${pnl.errors} err`}
      />
      <Kpi
        label="Trading cron"
        value={
          trading
            ? trading.cron
              ? trading.cron
              : trading.every_seconds
                ? `every ${trading.every_seconds}s`
                : "installed"
            : "not installed"
        }
        tone={trading?.enabled ? "ok" : "warn"}
        delta={trading?.target ?? "—"}
      />
      <Kpi
        label="Reflection cron"
        value={
          tuning
            ? tuning.cron
              ? tuning.cron
              : tuning.every_seconds
                ? `every ${tuning.every_seconds}s`
                : "installed"
            : "not installed"
        }
        tone={tuning?.enabled ? "ok" : "warn"}
        delta={
          lastRun
            ? `last run ${new Date(
                lastRun.finished_at || lastRun.started_at,
              ).toLocaleString()}`
            : "no runs yet"
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
}: {
  strategyId: string;
  listed: string[];
  detail: StrategyDetail;
  files: PackageFile[];
  onAfterSave: () => Promise<void>;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
}) {
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
    setEdits(initial);
  }, [initial]);

  const dirty = useMemo(() => {
    const keys = new Set([...Object.keys(initial), ...Object.keys(edits)]);
    return Array.from(keys).filter((k) => (initial[k] ?? "") !== (edits[k] ?? ""));
  }, [initial, edits]);

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
      onNotice(`Saved ${rel}`);
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
      title="Subagent & prompt library"
      description="Per-strategy `subagents/*.agent.md` and `prompts/*.md` bodies. Each save also snapshots strategy.yml for rollback."
    >
      {orderedKeys.length === 0 ? (
        <Empty label="No subagent or prompt files yet — list one in strategy.yml.subagents and save a body below." />
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
                    {isDirty ? <Pill tone="warn">unsaved</Pill> : null}
                  </div>
                  <button
                    onClick={() => void save(rel)}
                    disabled={busyKey !== null || !isDirty}
                    className="bg-brand-500/80 hover:bg-brand-500 disabled:opacity-40 text-white text-xs rounded px-3 py-1.5"
                  >
                    {busyKey === rel ? "Saving…" : "Save"}
                  </button>
                </div>
                <textarea
                  value={body}
                  onChange={(e) =>
                    setEdits((prev) => ({ ...prev, [rel]: e.target.value }))
                  }
                  className="input-dark font-mono w-full text-xs"
                  rows={Math.min(20, Math.max(8, body.split("\n").length + 2))}
                  placeholder={
                    rel.startsWith("subagents/")
                      ? "Describe the role, market, allowed actions, output schema."
                      : "Prompt body (markdown)."
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
}: {
  strategyId: string;
  files: PackageFile[];
  onAfterSave: () => Promise<void>;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
}) {
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
    const next: Record<string, string> = {};
    for (const f of filtered) {
      next[f.rel_path] = f.content ?? "";
    }
    setDrafts(next);
  }, [filtered]);

  const active = useMemo(
    () => filtered.find((f) => f.rel_path === activePath) ?? null,
    [filtered, activePath],
  );
  const draftBody = activePath ? (drafts[activePath] ?? "") : "";
  const original = active?.content ?? "";
  const dirty = activePath ? draftBody !== original : false;

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
      onNotice(`Saved ${activePath}`);
      await onAfterSave();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <Card
      title="Strategy package files"
      description="Editable: main.py, strategy.md, strategy.yml, tests/*, fixtures/*. Saves snapshot the package under versions/."
      actions={
        activePath ? (
          <button
            onClick={() => void save()}
            disabled={!dirty || busyKey !== null}
            className="bg-brand-500/80 hover:bg-brand-500 disabled:opacity-40 text-white text-xs rounded px-3 py-1.5"
          >
            {busyKey === activePath ? "Saving…" : "Save file"}
          </button>
        ) : null
      }
    >
      {filtered.length === 0 ? (
        <Empty label="No editable files yet — promote a strategy package to populate this surface." />
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
                      {f.kind} · {f.size} bytes
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="min-w-0">
            {!active ? (
              <Empty label="Pick a file to edit." />
            ) : active.error === "too_large" ? (
              <Empty
                label={`Too large to edit inline (${active.size} bytes). Edit on disk.`}
              />
            ) : active.error === "decode_failed" ? (
              <Empty label="Unreadable file (binary or non-UTF-8)." />
            ) : (
              <textarea
                value={draftBody}
                onChange={(e) =>
                  setDrafts((prev) => ({
                    ...prev,
                    [active.rel_path]: e.target.value,
                  }))
                }
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
  const [config, setConfig] = useState(JSON.stringify(detail.config ?? {}, null, 2));
  const [limits, setLimits] = useState(JSON.stringify(detail.limits ?? {}, null, 2));
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    setConfig(JSON.stringify(detail.config ?? {}, null, 2));
    setLimits(JSON.stringify(detail.limits ?? {}, null, 2));
  }, [detail.config, detail.limits]);

  function parseObj(label: string, text: string): Record<string, unknown> {
    try {
      const parsed = text.trim() ? JSON.parse(text) : {};
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error(`${label} must be a JSON object`);
      }
      return parsed as Record<string, unknown>;
    } catch (e) {
      throw new Error(
        `${label} JSON invalid: ${
          e instanceof Error ? e.message : String(e)
        }`,
      );
    }
  }

  async function save() {
    setBusy("save");
    onError(null);
    try {
      await clientApi.strategyUpdate(detail.strategy.id, {
        config: parseObj("config", config),
        limits: parseObj("limits", limits),
        reason: "dashboard_manifest_edit",
      });
      onNotice("config / limits updated.");
      await onAfterSave();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card
      title="Risk limits & config"
      description="limits.yml drives the trading kernel's per-strategy gates. config.yml is read by main.py through ctx.config."
      actions={
        <button
          onClick={() => void save()}
          disabled={busy !== null}
          className="bg-brand-500/80 hover:bg-brand-500 disabled:opacity-40 text-white text-xs rounded px-3 py-1.5"
        >
          {busy === "save" ? "Saving…" : "Save"}
        </button>
      }
    >
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-ink-500 mb-1">
            limits.yml
          </div>
          <textarea
            value={limits}
            onChange={(e) => setLimits(e.target.value)}
            className="input-dark font-mono w-full"
            rows={16}
            spellCheck={false}
          />
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-ink-500 mb-1">
            config.yml
          </div>
          <textarea
            value={config}
            onChange={(e) => setConfig(e.target.value)}
            className="input-dark font-mono w-full"
            rows={16}
            spellCheck={false}
          />
        </div>
      </div>
    </Card>
  );
}
