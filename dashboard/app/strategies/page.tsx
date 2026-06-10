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
import {
  confirm as confirmDialog,
  prompt as promptDialog,
} from "../../lib/dialogs";
import type { StrategyCard as StrategyScorecard } from "../../lib/api";
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
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
    setBusy("create");
    setError(null);
    setNotice(null);
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
      let notice = `${t("createdPrefix")} ${out.strategy_id}. ${t("createdSuffix")}`;
      if (out.warning && out.warning.code === "account_already_bound") {
        notice += ` · ${t("createdAccountSharedSuffix", {
          count: out.warning.strategies?.length ?? 0,
        })}`;
      }
      setNotice(notice);
      setDraft(EMPTY_DRAFT);
      setShowCreate(false);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function renameStrategy(strategy: StrategyRecord) {
    const nextTitle = await promptDialog({
      title: t("editName"),
      message: t("renamePrompt", { id: strategy.id }),
      defaultValue: strategy.title || strategy.id,
      placeholder: t("fieldTitle"),
      okLabel: tCommon("save"),
    });
    if (nextTitle === null) return;
    const trimmed = nextTitle.trim();
    if (!trimmed) {
      setError(t("nameRequired"));
      return;
    }
    if (trimmed === (strategy.title || strategy.id)) return;
    setBusy(`rename:${strategy.id}`);
    setError(null);
    setNotice(null);
    try {
      const res = await clientApi.strategyUpdate(strategy.id, {
        title: trimmed,
        reason: "dashboard_rename_strategy",
      });
      if (!res.ok) throw new Error("strategy_rename_failed");
      setNotice(t("nameUpdated", { id: strategy.id, title: trimmed }));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function deleteStrategy(strategy: StrategyRecord, force = false) {
    const ok = await confirmDialog({
      message: force
        ? t("forceDeleteConfirm", { id: strategy.id })
        : t("deleteConfirm", { id: strategy.id }),
      tone: "danger",
      okLabel: force ? t("forceDelete") : tCommon("delete"),
    });
    if (!ok) return;
    setBusy(`delete:${strategy.id}`);
    setError(null);
    setNotice(null);
    try {
      const res = await clientApi.strategyDelete({
        strategy_id: strategy.id,
        force,
      });
      if (!res.ok) {
        if (res.state && !force) {
          const closeFirst = res.state.open_positions > 0
            ? await confirmDialog({
                title: t("cannotDeleteTitle"),
                message: t("cannotDelete", {
                  positions: res.state.open_positions,
                  executors: res.state.active_executors,
                  orders: res.state.active_orders,
                }),
                okLabel: t("closePositions"),
                cancelLabel: t("pauseInstead"),
                tone: "warning",
              })
            : false;
          if (closeFirst) {
            await closeStrategyPositions(strategy);
          } else {
            await pauseStrategy(strategy);
          }
          return;
        }
        throw new Error(res.error || "strategy_delete_failed");
      }
      setNotice(t("deletedInfo", { id: strategy.id }));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function closeStrategyPositions(strategy: StrategyRecord) {
    setBusy(`close:${strategy.id}`);
    setError(null);
    setNotice(null);
    try {
      const preview = await clientApi.strategyClosePositions({
        strategy_id: strategy.id,
        dry_run: true,
      });
      if (!preview.ok) throw new Error(preview.error || "strategy_close_preview_failed");
      if (preview.count <= 0) {
        setNotice(t("noPositionsToClose", { id: strategy.id }));
        await load();
        return;
      }
      const ok = await confirmDialog({
        title: t("closePositionsTitle"),
        message: t("closePositionsConfirm", {
          id: strategy.id,
          count: preview.count,
          notional: preview.positions
            .reduce((sum, row) => sum + (Number(row.notional_usd) || 0), 0)
            .toFixed(2),
        }),
        okLabel: t("closePositions"),
        tone: "warning",
      });
      if (!ok) return;
      const res = await clientApi.strategyClosePositions({
        strategy_id: strategy.id,
        operator: "dashboard",
        reason: "strategy_delete_prepare",
      });
      if (!res.ok) throw new Error(res.error || "strategy_close_positions_failed");
      setNotice(t("closeSubmitted", {
        id: strategy.id,
        count: res.submitted?.length ?? res.count,
      }));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function pauseStrategy(strategy: StrategyRecord) {
    if (strategy.status === "paused") {
      setNotice(t("pausedInfo", { id: strategy.id }));
      return;
    }
    const ok = await confirmDialog({
      message: t("pauseConfirm", { id: strategy.id }),
      okLabel: t("pauseStrategy"),
      tone: "warning",
    });
    if (!ok) return;
    setBusy(`pause:${strategy.id}`);
    setError(null);
    setNotice(null);
    try {
      const res = await clientApi.strategySetStatus(strategy.id, "paused", "dashboard_pause");
      if (!res.ok) throw new Error("strategy_pause_failed");
      setNotice(t("pausedInfo", { id: strategy.id }));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
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

  return (
    <div>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <button
            onClick={() => void load()}
            disabled={loading}
            className="btn btn-ghost cursor-pointer text-xs"
          >
            {loading ? tCommon("refreshing") : tCommon("refresh")}
          </button>
        }
      />
      <SectionTabs section="strategy" />
      <PageBody>
        {error && <ErrorBanner error={error} />}
        {notice && (
          <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-sm text-emerald-200">
            {notice}
          </div>
        )}

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
            value={formatSignedUsd(totalPnlSum)}
            tone={totalPnlSum > 0 ? "ok" : totalPnlSum < 0 ? "danger" : "neutral"}
          />
        </div>

        <Advanced
          title={t("createAdvancedTitle")}
          description={t("createAdvancedHint")}
          open={showCreate}
          onToggle={(next) => setShowCreate(next)}
        >
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
            <Field label={t("fieldStrategyId")}>
              <input
                value={draft.strategy_id}
                onChange={(e) => setDraft({ ...draft, strategy_id: e.target.value })}
                className="input-dark font-mono"
                placeholder="eth_mean_reversion"
              />
            </Field>
            <Field label={t("fieldTitle")}>
              <input
                value={draft.title}
                onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                className="input-dark"
                placeholder="ETH mean reversion"
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
            <Field label={t("fieldWallet")}>
              <WalletSelect
                value={draft.wallet_id}
                bindings={walletBindings}
                discovery={discovery}
                onChange={(wallet_id) => setDraft({ ...draft, wallet_id })}
              />
            </Field>
            <Field label={t("fieldMarkets")}>
              <input
                value={draft.markets}
                onChange={(e) => setDraft({ ...draft, markets: e.target.value })}
                className="input-dark font-mono"
                placeholder="binance:BTCUSDT"
              />
            </Field>
            <Field label={t("fieldTriggerKinds")}>
              <input
                value={draft.trigger_kinds}
                onChange={(e) => setDraft({ ...draft, trigger_kinds: e.target.value })}
                className="input-dark font-mono"
              />
            </Field>
            <Field label={t("fieldSubagents")}>
              <input
                value={draft.subagents}
                onChange={(e) => setDraft({ ...draft, subagents: e.target.value })}
                className="input-dark font-mono"
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
          <div className="mt-3 flex justify-end">
            <button
              onClick={() => void createStrategy()}
              disabled={busy !== null || !draft.strategy_id || !draft.account_id || !draft.markets}
              className="btn btn-primary cursor-pointer"
            >
              {busy === "create" ? t("creating") : t("createStrategyBtn")}
            </button>
          </div>
        </Advanced>

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
                  onError={setError}
                  onNotice={setNotice}
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
                      label: `${k} (${v})`,
                    })),
                  ]}
                  size="sm"
                  ariaLabel={t("allStatuses")}
                />
              </div>
            </div>
          }
        >
          {filtered.length === 0 ? (
            <Empty
              title={strategies.length === 0 ? t("noStrategiesTitle") : t("noMatchTitle")}
              subtitle={
                strategies.length === 0
                  ? t("noStrategiesSubtitle")
                  : t("noMatchSubtitle")
              }
            />
          ) : (
            <div className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(320px,1fr))]">
              {filtered.map((strategy) => (
                <StrategyCard
                  key={strategy.id}
                  strategy={strategy}
                  scorecard={strategyScorecards[strategy.id]}
                  busy={busy}
                  onRename={renameStrategy}
                  onDelete={deleteStrategy}
                  onPause={pauseStrategy}
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
  onDelete: (strategy: StrategyRecord, force?: boolean) => Promise<void>;
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
  const renaming = busy === `rename:${strategy.id}`;
  const deleting = busy === `delete:${strategy.id}`;
  const pausing = busy === `pause:${strategy.id}`;
  const closing = busy === `close:${strategy.id}`;
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
      className="group relative flex cursor-pointer flex-col gap-3 rounded-xl border border-[color:var(--line)] bg-[color:var(--card)] p-4 transition-colors hover:border-[color:var(--line-hi)]"
      onClick={() => {
        void router.push(strategyHref);
      }}
    >
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-[15px] font-medium text-[color:var(--text-base)]">
            {strategy.title || t("untitledStrategy")}
          </div>
          <div className="mt-0.5 truncate font-mono text-[12px] text-[color:var(--text-muted)]">
            {strategy.id}
          </div>
        </div>
        <StatusDot tone={tone} label={strategy.status} />
      </div>
      <div className="relative flex items-baseline justify-between gap-3">
        <div className="text-[12px] text-[color:var(--text-muted)]">
          {strategy.mode || "–"} · {strategy.account_id || "–"}
        </div>
        <div className={`text-[18px] font-medium tabular-nums ${pnlClassName(totalPnl)}`}>
          {formatSignedUsd(totalPnl)}
        </div>
      </div>
      <div className="relative truncate font-mono text-[12px] text-[color:var(--text-muted)]">
        {markets}
      </div>
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
          {closing ? <span className="ml-1">{t("closingPositions")}</span> : deleting ? <span className="ml-1">{t("deleting")}</span> : null}
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
