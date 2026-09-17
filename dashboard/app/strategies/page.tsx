"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  EditIcon,
  PauseIcon,
  TrashIcon,
} from "../../components/icons";
import {
  clientApi,
  type AccountSummary,
  type DiscoverySnapshot,
  type EvolutionProposal,
  type StrategyRecord,
  type WalletBinding,
} from "../../lib/clientApi";
import { confirm as confirmDialog, toast } from "../../lib/dialogs";
import {
  strategyStatusLabel,
  useStrategyLifecycle,
} from "../../lib/useStrategyLifecycle";
import type { StrategyCard as StrategyScorecard } from "../../lib/api";
import { ModePill } from "../../components/ModePill";
import {
  Advanced,
  Card,
  Empty,
  ErrorBanner,
  Kpi,
  PageBody,
  PageHeader,
  StatusDot,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import { StrategyWorkflowHub } from "../../components/workflows/StrategyWorkflowHub";
import { useWorkflowText } from "../../components/workflows/WorkflowCanvas";
import { StrategyCardSpark } from "../../components/strategies/StrategyCardSpark";
import { Select } from "../../components/Select";
import {
  StrategyProposalApprovalCard,
  isActiveStrategyProposal,
} from "../../components/strategies/StrategyProposalApprovalCard";

type DraftForm = {
  strategy_id: string;
  title: string;
  description: string;
  account_id: string;
  markets: string;
  trigger_kinds: string;
  subagents: string;
  driver: "prompt" | "script";
  status: "draft" | "paper" | "canary" | "live" | "paused" | "archived";
  wallet_id: string;
  main_prompt: string;
};

const EMPTY_DRAFT: DraftForm = {
  strategy_id: "",
  title: "",
  description: "",
  account_id: "",
  markets: "",
  trigger_kinds: "price.breakout",
  subagents: "market_analyst,risk_critic",
  driver: "prompt",
  status: "draft",
  wallet_id: "",
  main_prompt: "",
};

function parseList(value: string): string[] {
  return value.split(",").map((s) => s.trim()).filter(Boolean);
}

function pnlClassName(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value) || value === 0) {
    return "text-ink-400";
  }
  return value > 0 ? "text-accent-400" : "text-danger";
}

function formatSignedUsd(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value)) return "–";
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toLocaleString(undefined, {
    maximumFractionDigits: 2,
  })}`;
}

function finiteNumber(value: number | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export default function StrategiesPage() {
  const [legacy, setLegacy] = useState(false);
  const text = useWorkflowText();
  return legacy ? <><button className="btn btn-ghost mb-4" onClick={() => setLegacy(false)}>← {text("返回策略工作流", "Back to strategy workflows")}</button><LegacyStrategiesPage /></> : <StrategyWorkflowHub onLegacyView={() => setLegacy(true)} />;
}

function LegacyStrategiesPage() {
  const t = useTranslations("strategies");
  const tCommon = useTranslations("common");
  const [strategies, setStrategies] = useState<StrategyRecord[]>([]);
  const [strategyScorecards, setStrategyScorecards] = useState<
    Record<string, StrategyScorecard>
  >({});
  const [strategyProposals, setStrategyProposals] = useState<EvolutionProposal[]>([]);
  const [discovery, setDiscovery] = useState<DiscoverySnapshot | null>(null);
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [walletBindings, setWalletBindings] = useState<WalletBinding[]>([]);
  const [draft, setDraft] = useState<DraftForm>(EMPTY_DRAFT);
  const [showCreate, setShowCreate] = useState(false);
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [loading, setLoading] = useState(true);
  const [createBusy, setCreateBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Pause / delete / rename share the lifecycle hook with the detail
  // page; the local busy state only tracks the create form.
  const lifecycle = useStrategyLifecycle({ onRefresh: load });
  const busy = lifecycle.busy ?? (createBusy ? "create" : null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      // Strategy create / rebind reads the
      // account roster from /accounts/list (the new control-plane
      // surface; quarantine/read_only/disabled are filtered out for
      // binding) and the configured wallet providers from
      // /wallet/configured. The legacy /discovery snapshot is kept
      // around as a fallback for very old workspaces.
      const [snap, res, accList, wallets, proposalRes, scorecardRes] =
        await Promise.all([
          clientApi.discoverySnapshot().catch(() => null),
          clientApi.strategiesAll(true),
          clientApi.accountsList().catch(() => ({ accounts: [], ts: 0 })),
          clientApi.walletConfigured().catch(() => ({ bindings: [], count: 0 })),
          clientApi.proposalsList().catch(() => ({ proposals: [] })),
          clientApi.strategyList().catch(() => ({ strategies: [] })),
        ]);
      setDiscovery(snap);
      setStrategies(res.strategies ?? []);
      setStrategyScorecards(
        Object.fromEntries(
          (scorecardRes.strategies ?? []).map((item) => [item.id, item]),
        ),
      );
      setStrategyProposals((proposalRes.proposals ?? []).filter(isActiveStrategyProposal));
      setAccounts(accList.accounts ?? []);
      setWalletBindings(wallets.bindings ?? []);
      const firstBindable =
        accList.accounts?.find((a) => a.profile.status === "active")?.profile.id ??
        accList.accounts?.[0]?.profile.id ??
        snap?.accounts?.[0]?.id ??
        "";
      if (snap || accList.accounts.length > 0) {
        setDraft((prev) => ({
          ...prev,
          account_id: prev.account_id || firstBindable,
          markets: prev.markets || (snap?.markets?.[0] ?? ""),
        }));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function createStrategy() {
    // Surface the same soft warning we put on the
    // /strategy/bind_account flow. The backend will return ``warning``
    // post-hoc, but at the form level we already know which account
    // the operator picked, so we pre-flight against the cached
    // ``bound_strategies`` summary and let them back out before the
    // manifest gets written.
    const targetAccount = draft.account_id.trim();
    if (targetAccount) {
      const acct = accounts.find((a) => a.profile.id === targetAccount);
      const shared = (acct?.bound_strategies || []).filter(
        (entry) => entry.strategy_id && entry.strategy_id !== draft.strategy_id.trim().toLowerCase(),
      );
      if (shared.length > 0) {
        const names = shared.slice(0, 5).map((entry) => entry.strategy_id).join(", ");
        const proceed = await confirmDialog({
          title: t("shareWarningTitle"),
          message: t("shareWarningMessage", {
            count: shared.length,
            accountId: targetAccount,
            strategies: names,
          }),
          okLabel: t("shareWarningContinue"),
          cancelLabel: t("shareWarningCancel"),
          tone: "warning",
        });
        if (!proceed) return;
      }
    }
    setCreateBusy(true);
    setError(null);
    try {
      const out = await clientApi.strategyCreate({
        strategy_id: draft.strategy_id.trim().toLowerCase(),
        title: draft.title.trim() || draft.strategy_id.trim(),
        description: draft.description.trim(),
        account_id: draft.account_id.trim(),
        markets: parseList(draft.markets),
        trigger_kinds: parseList(draft.trigger_kinds),
        subagents: parseList(draft.subagents),
        driver: draft.driver,
        status: draft.status,
        wallet_id: draft.wallet_id || undefined,
        main_prompt: draft.main_prompt.trim() || undefined,
      });
      if (!out.ok) throw new Error(JSON.stringify(out));
      let message = `${t("createdPrefix")} ${out.strategy_id}. ${t("createdSuffix")}`;
      if (out.warning && out.warning.code === "account_already_bound") {
        message += ` · ${t("createdAccountSharedSuffix", {
          count: out.warning.strategies?.length ?? 0,
        })}`;
      }
      toast({ message, tone: "ok" });
      setDraft(EMPTY_DRAFT);
      setShowCreate(false);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreateBusy(false);
    }
  }

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return strategies.filter((s) => {
      if (statusFilter !== "all" && s.status !== statusFilter) return false;
      if (!q) return true;
      const haystack = [
        s.id,
        s.title || "",
        s.account_id || "",
        (s.markets || []).join(","),
        (s.trigger_kinds || []).join(","),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [strategies, filter, statusFilter]);

  const pendingStrategyProposals = useMemo(
    () => strategyProposals.filter(isActiveStrategyProposal),
    [strategyProposals],
  );

  const counts = useMemo(() => {
    const tally: Record<string, number> = {};
    for (const s of strategies) {
      tally[s.status] = (tally[s.status] || 0) + 1;
    }
    return tally;
  }, [strategies]);

  const totalPnlSum = useMemo(() => {
    let sum = 0;
    for (const s of strategies) {
      const card = strategyScorecards[s.id];
      const value =
        finiteNumber(card?.total_pnl_usd) ??
        ((finiteNumber(card?.realized_pnl_usd) ?? 0) +
          (finiteNumber(card?.unrealized_pnl_usd) ?? 0));
      if (Number.isFinite(value)) sum += value;
    }
    return sum;
  }, [strategies, strategyScorecards]);

  // Aggregate PnL provenance: if any scorecard trades live money the
  // sum must read as LIVE even when paper strategies are mixed in.
  const totalPnlMode = useMemo(() => {
    const modes = strategies
      .map((s) => pnlModeFromScorecard(strategyScorecards[s.id]))
      .filter(Boolean);
    if (modes.includes("live")) return "live" as const;
    if (modes.includes("paper")) return "paper" as const;
    return undefined;
  }, [strategies, strategyScorecards]);
  const totalPnlMixed = useMemo(() => {
    const modes = new Set(
      strategies
        .map((s) => pnlModeFromScorecard(strategyScorecards[s.id]))
        .filter(Boolean),
    );
    return modes.size > 1;
  }, [strategies, strategyScorecards]);

  return (
    <div>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowCreate((v) => !v)}
              className="btn btn-primary cursor-pointer text-xs"
            >
              {t("newStrategy")}
            </button>
            <button
              onClick={() => void load()}
              disabled={loading}
              className="btn btn-ghost cursor-pointer text-xs"
            >
              {loading ? tCommon("refreshing") : tCommon("refresh")}
            </button>
          </div>
        }
      />
      <SectionTabs section="strategy" />
      <PageBody>
        {error && <ErrorBanner error={error} />}

        <div className="flex flex-wrap items-end gap-x-8 gap-y-3 px-1">
          <Kpi inline label={t("kpiTotal")} value={String(strategies.length)} />
          <Kpi
            inline
            label={t("kpiLive")}
            value={String(counts["live"] || 0)}
            tone={(counts["live"] || 0) > 0 ? "ok" : "neutral"}
          />
          <Kpi
            inline
            label={t("kpiPending")}
            value={String(pendingStrategyProposals.length)}
            tone={pendingStrategyProposals.length > 0 ? "warn" : "neutral"}
          />
          <Kpi
            inline
            label={t("kpiTotalPnl")}
            value={
              <span className="inline-flex flex-wrap items-center gap-2">
                {formatSignedUsd(totalPnlSum)}
                {totalPnlMode ? <ModePill mode={totalPnlMode} /> : null}
              </span>
            }
            tone={totalPnlSum > 0 ? "ok" : totalPnlSum < 0 ? "danger" : "neutral"}
            delta={totalPnlMixed ? t("kpiTotalPnlMixed") : undefined}
          />
        </div>

        {showCreate ? (
          <Card title={t("createStrategy")} description={t("createStrategyDesc")}>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
              <Field label={t("fieldTitle")}>
                <input
                  value={draft.title}
                  onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                  className="input-dark"
                  placeholder="ETH mean reversion"
                />
              </Field>
              <Field label={t("fieldStrategyId")}>
                <input
                  value={draft.strategy_id}
                  onChange={(e) => setDraft({ ...draft, strategy_id: e.target.value })}
                  className="input-dark font-mono"
                  placeholder="eth_mean_reversion"
                />
              </Field>
              <Field label={t("fieldAccount")}>
                <AccountSelect
                  value={draft.account_id}
                  accounts={accounts}
                  discovery={discovery}
                  onChange={(account_id) => setDraft({ ...draft, account_id })}
                />
              </Field>
              <Field label={t("fieldMarkets")}>
                <input
                  value={draft.markets}
                  onChange={(e) => setDraft({ ...draft, markets: e.target.value })}
                  className="input-dark font-mono"
                  placeholder={t("fieldMarketsPlaceholder")}
                />
              </Field>
            </div>
            <Advanced
              title={t("createAdvancedGroupTitle")}
              description={t("createAdvancedHint")}
            >
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
                <Field label={t("fieldWallet")}>
                  <WalletSelect
                    value={draft.wallet_id}
                    bindings={walletBindings}
                    discovery={discovery}
                    onChange={(wallet_id) => setDraft({ ...draft, wallet_id })}
                  />
                </Field>
                <Field label={t("fieldTriggerKinds")}>
                  <input
                    value={draft.trigger_kinds}
                    onChange={(e) => setDraft({ ...draft, trigger_kinds: e.target.value })}
                    className="input-dark font-mono"
                    placeholder={t("fieldTriggerKindsPlaceholder")}
                  />
                </Field>
                <Field label={t("fieldSubagents")}>
                  <input
                    value={draft.subagents}
                    onChange={(e) => setDraft({ ...draft, subagents: e.target.value })}
                    className="input-dark font-mono"
                    placeholder={t("fieldSubagentsPlaceholder")}
                  />
                </Field>
                <Field label={t("fieldDriver")}>
                  <Select<DraftForm["driver"]>
                    value={draft.driver}
                    onChange={(value) => setDraft({ ...draft, driver: value })}
                    options={[
                      { value: "prompt", label: "prompt" },
                      { value: "script", label: "script" },
                    ]}
                    size="sm"
                    ariaLabel={t("fieldDriver")}
                  />
                </Field>
                <Field label={t("fieldDescription")} full>
                  <textarea
                    value={draft.description}
                    onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                    className="input-dark h-16"
                  />
                </Field>
                <Field label={t("fieldMainPrompt")} full>
                  <textarea
                    value={draft.main_prompt}
                    onChange={(e) => setDraft({ ...draft, main_prompt: e.target.value })}
                    className="input-dark font-mono h-28"
                  />
                </Field>
              </div>
            </Advanced>
            <div className="mt-3 flex justify-end">
              <button
                onClick={() => void createStrategy()}
                disabled={busy !== null || !draft.strategy_id || !draft.account_id || !draft.markets}
                className="btn btn-primary cursor-pointer"
              >
                {createBusy ? t("creating") : t("createStrategyBtn")}
              </button>
            </div>
          </Card>
        ) : null}

        {pendingStrategyProposals.length > 0 ? (
          <Card
            title={t("pendingProposalsTitle", { count: pendingStrategyProposals.length })}
            description={t("pendingProposalsDesc")}
          >
            <div className="embedded-list-scroll-lg grid gap-3">
              {pendingStrategyProposals.map((proposal) => (
                <StrategyProposalApprovalCard
                  key={proposal.id}
                  proposal={proposal}
                  approveNote="approved from strategies dashboard"
                  onApproved={async () => {
                    await load();
                  }}
                  onDeleted={async () => {
                    await load();
                  }}
                  onError={(msg) => {
                    if (msg) toast({ message: msg, tone: "error" });
                  }}
                  onNotice={(msg) => {
                    if (msg) toast({ message: msg, tone: "ok" });
                  }}
                />
              ))}
            </div>
          </Card>
        ) : null}

        <Card
          title={t("strategiesCount", { count: strategies.length })}
          description={t("strategiesDesc")}
          actions={
            <div className="flex items-center gap-2">
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder={t("filterPlaceholder")}
                className="input-dark text-[12px] w-56"
              />
              <div className="min-w-[180px]">
                <Select
                  value={statusFilter}
                  onChange={(value) => setStatusFilter(value)}
                  options={[
                    { value: "all", label: t("allStatuses") },
                    ...Object.entries(counts).map(([k, v]) => ({
                      value: k,
                      label: `${strategyStatusLabel(t, k)} (${v})`,
                    })),
                  ]}
                  size="sm"
                  ariaLabel={t("allStatuses")}
                />
              </div>
            </div>
          }
        >
          {loading && strategies.length === 0 ? (
            // First-paint skeleton — avoids flashing the "no strategies"
            // empty state while the six load calls are in flight.
            <div
              className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(360px,1fr))]"
              aria-hidden="true"
            >
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <div key={i} className="skeleton h-[230px] rounded-xl" />
              ))}
            </div>
          ) : filtered.length === 0 ? (
            <Empty
              title={strategies.length === 0 ? t("noStrategiesTitle") : t("noMatchTitle")}
              subtitle={
                strategies.length === 0
                  ? t("noStrategiesSubtitle")
                  : t("noMatchSubtitle")
              }
            />
          ) : (
            <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(360px,1fr))]">
              {filtered.map((strategy) => (
                <StrategyCard
                  key={strategy.id}
                  strategy={strategy}
                  scorecard={strategyScorecards[strategy.id]}
                  busy={busy}
                  onRename={lifecycle.rename}
                  onDelete={lifecycle.remove}
                  onPause={lifecycle.pause}
                />
              ))}
            </div>
          )}
        </Card>
      </PageBody>
    </div>
  );
}

function statusTone(status: string): "ok" | "warn" | "danger" | "brand" | "neutral" {
  if (status === "live") return "ok";
  if (status === "paper" || status === "canary") return "brand";
  if (status === "paused") return "warn";
  if (status === "archived") return "neutral";
  return "neutral";
}

/** Trading-mode for a PnL number, from the scorecard's own flags. */
function pnlModeFromScorecard(
  scorecard: StrategyScorecard | undefined,
): "paper" | "live" | undefined {
  if (!scorecard) return undefined;
  if (scorecard.paper_trading_enabled) return "paper";
  if (scorecard.live_trading_enabled) return "live";
  return undefined;
}

function StrategyCard({
  strategy,
  scorecard,
  busy,
  onRename,
  onDelete,
  onPause,
}: {
  strategy: StrategyRecord;
  scorecard?: StrategyScorecard;
  busy: string | null;
  onRename: (strategy: StrategyRecord) => Promise<void>;
  onDelete: (strategy: StrategyRecord) => Promise<void>;
  onPause: (strategy: StrategyRecord) => Promise<void>;
}) {
  const t = useTranslations("strategies");
  const tCommon = useTranslations("common");
  const totalPnl =
    finiteNumber(scorecard?.total_pnl_usd) ??
    (scorecard
      ? (finiteNumber(scorecard?.realized_pnl_usd) ?? 0) +
        (finiteNumber(scorecard?.unrealized_pnl_usd) ?? 0)
      : undefined);
  // PnL provenance (compliance): the paper/live pill travels with the
  // number so a big green figure can never be mistaken for real-money
  // profit without its source label.
  const pnlMode = pnlModeFromScorecard(scorecard);
  const renaming = busy === `rename:${strategy.id}`;
  const deleting = busy === `delete:${strategy.id}`;
  const pausing = busy === `pause:${strategy.id}`;
  const canPause = !["paused", "archived", "draft", "static_review", "backtested"].includes(
    strategy.status,
  );
  const markets = (strategy.markets || []).join(", ") || t("noMarkets");
  const tone = statusTone(strategy.status);
  const router = useRouter();
  const strategyHref = `/strategies/${encodeURIComponent(strategy.id)}`;

  return (
    <div
      data-strategy-id={strategy.id}
      className="group relative flex min-h-[230px] cursor-pointer flex-col gap-4 overflow-hidden rounded-xl border border-[color:var(--line)] bg-[linear-gradient(180deg,rgba(23,26,53,.78),rgba(10,11,26,.72))] p-5 shadow-[0_24px_70px_-52px_rgba(139,92,246,.9)] transition-colors hover:border-[color:var(--line-hi)]"
      onClick={() => {
        void router.push(strategyHref);
      }}
    >
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -right-16 -top-20 h-40 w-40 rounded-full bg-violet-500/10 blur-3xl transition-opacity group-hover:opacity-90"
      />
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-[16px] font-semibold text-[color:var(--text-base)]">
            {strategy.title || t("untitledStrategy")}
          </div>
          <div className="mt-0.5 truncate font-mono text-[12px] text-[color:var(--text-muted)]">
            {strategy.id}
          </div>
        </div>
        <StatusDot tone={tone} label={strategyStatusLabel(t, strategy.status)} />
      </div>
      <div className="relative flex items-baseline justify-between gap-3">
        <div className="rounded-full border border-[color:var(--line)] bg-white/[0.03] px-2.5 py-1 text-[11px] text-[color:var(--text-muted)]">
          {strategy.mode || "–"} · {strategy.account_id || "–"}
        </div>
        <div className="flex items-center gap-1.5">
          {pnlMode ? <ModePill mode={pnlMode} /> : null}
          <div className={`text-[22px] font-semibold tabular-nums ${pnlClassName(totalPnl)}`}>
            {formatSignedUsd(totalPnl)}
          </div>
        </div>
      </div>
      <div className="relative truncate font-mono text-[12px] text-[color:var(--text-muted)]">
        {markets}
      </div>
      <StrategyCardSpark strategyId={strategy.id} />
      <div className="relative flex items-center justify-end gap-1 pt-1">
        <button
          type="button"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            void onRename(strategy);
          }}
          disabled={busy !== null}
          title={t("editName")}
          aria-label={t("editName")}
          className="btn btn-ghost cursor-pointer text-[12px] py-0.5 px-1.5"
        >
          <EditIcon size={13} />
          {renaming ? <span className="ml-1">{t("renaming")}</span> : null}
        </button>
        {canPause ? (
          <button
            type="button"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              void onPause(strategy);
            }}
            disabled={busy !== null}
            title={t("pauseStrategy")}
            aria-label={t("pauseStrategy")}
            className="btn btn-ghost cursor-pointer text-amber-500 text-[12px] py-0.5 px-1.5"
          >
            <PauseIcon size={13} />
            {pausing ? <span className="ml-1">{t("pausing")}</span> : null}
          </button>
        ) : null}
        <button
          type="button"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            void onDelete(strategy);
          }}
          disabled={busy !== null}
          title={tCommon("delete")}
          aria-label={tCommon("delete")}
          className="btn btn-ghost cursor-pointer text-rose-500 text-[12px] py-0.5 px-1.5"
        >
          <TrashIcon size={13} />
          {deleting ? <span className="ml-1">{t("deleting")}</span> : null}
        </button>
      </div>
    </div>
  );
}

function Field({
  label,
  children,
  full = false,
}: {
  label: string;
  children: ReactNode;
  full?: boolean;
}) {
  return (
    <label className={`block ${full ? "md:col-span-2" : ""}`}>
      <span className="text-[11px] text-ink-400">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  );
}

function AccountSelect({
  value,
  accounts,
  discovery,
  onChange,
}: {
  value: string;
  accounts: AccountSummary[];
  discovery: DiscoverySnapshot | null;
  onChange: (value: string) => void;
}) {
  const t = useTranslations("strategies");
  // Prefer /accounts/list (control plane). Falls back to the legacy
  // discovery snapshot if the new endpoint hasn't returned anything
  // yet (e.g. on a brand-new workspace).
  if (accounts.length > 0) {
    return (
      <Select
        value={value}
        onChange={(next) => onChange(next)}
        options={accounts.map(({ profile }) => {
          const disabled = profile.status !== "active";
          return {
            value: profile.id,
            disabled,
            label:
              `${profile.id} · ${profile.venue} · ${profile.mode}` +
              (disabled ? ` (${profile.status})` : ""),
          };
        })}
        size="sm"
        ariaLabel="account"
        className="font-mono"
      />
    );
  }
  if (!discovery?.accounts?.length) {
    return (
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="input-dark font-mono"
        placeholder="paper_main"
      />
    );
  }
  return (
    <Select
      value={value}
      onChange={(next) => onChange(next)}
      options={discovery.accounts.map((account) => ({
        value: account.id,
        label: `${account.id} · ${account.venue || account.exchange} · ${account.mode}`,
      }))}
      size="sm"
      ariaLabel="account"
      className="font-mono"
    />
  );
}

function WalletSelect({
  value,
  bindings,
  discovery,
  onChange,
}: {
  value: string;
  bindings: WalletBinding[];
  discovery: DiscoverySnapshot | null;
  onChange: (value: string) => void;
}) {
  const t = useTranslations("strategies");
  // /wallet/configured exposes the multi-provider map (providers and
  // legacy bindings combined). The discovery snapshot still drives the
  // `ready` indicator until the control-plane wallet probe lands, so
  // we union the two if both are present.
  const readyMap = new Map(
    (discovery?.wallets?.providers ?? []).map((p) => [p.id, p]),
  );
  const options = [
    { value: "", label: t("globalWallet") } as {
      value: string;
      label: string;
    },
  ];
  for (const binding of bindings) {
    const probe = readyMap.get(binding.wallet_id);
    options.push({
      value: binding.wallet_id,
      label:
        `${binding.label || binding.wallet_id} · ${binding.provider}` +
        (binding.source === "legacy" ? ` ${t("legacyTag")}` : "") +
        (probe && !probe.ready ? ` ${t("notReadyDotTag")}` : ""),
    });
  }
  if (bindings.length === 0 && discovery?.wallets?.providers?.length) {
    for (const wallet of discovery.wallets.providers) {
      options.push({
        value: wallet.id,
        label: `${wallet.label || wallet.id}${wallet.ready ? "" : ` ${t("notReadyTag")}`}`,
      });
    }
  }
  return (
    <Select
      value={value}
      onChange={(next) => onChange(next)}
      options={options}
      size="sm"
      ariaLabel={t("globalWallet")}
    />
  );
}
