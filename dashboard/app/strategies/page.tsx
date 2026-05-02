"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  clientApi,
  type AccountSummary,
  type DiscoverySnapshot,
  type EvolutionProposal,
  type StrategyRecord,
  type WalletBinding,
} from "../../lib/clientApi";
import {
  Card,
  Empty,
  ErrorBanner,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
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

export default function StrategiesPage() {
  const t = useTranslations("strategies");
  const tCommon = useTranslations("common");
  const [strategies, setStrategies] = useState<StrategyRecord[]>([]);
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
      // 04-29 §11 P9 — strategy create / rebind reads the
      // account roster from /accounts/list (the new control-plane
      // surface; quarantine/read_only/disabled are filtered out for
      // binding) and the configured wallet providers from
      // /wallet/configured. The legacy /discovery snapshot is kept
      // around as a fallback for very old workspaces.
      const [snap, res, accList, wallets, proposalRes] = await Promise.all([
        clientApi.discoverySnapshot().catch(() => null),
        clientApi.strategiesAll(true),
        clientApi.accountsList().catch(() => ({ accounts: [], ts: 0 })),
        clientApi.walletConfigured().catch(() => ({ bindings: [], count: 0 })),
        clientApi.proposalsList().catch(() => ({ proposals: [] })),
      ]);
      setDiscovery(snap);
      setStrategies(res.strategies ?? []);
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
      setNotice(`${t("createdPrefix")} ${out.strategy_id}. ${t("createdSuffix")}`);
      setDraft(EMPTY_DRAFT);
      setShowCreate(false);
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

  return (
    <div>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <>
            <button
              onClick={() => void load()}
              disabled={loading}
              className="btn btn-ghost cursor-pointer text-xs"
            >
              {loading ? tCommon("refreshing") : tCommon("refresh")}
            </button>
            <button
              onClick={() => setShowCreate((v) => !v)}
              className="btn btn-primary cursor-pointer"
            >
              {showCreate ? tCommon("cancel") : t("newStrategy")}
            </button>
          </>
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

        {showCreate && (
          <Card
            title={t("createStrategy")}
            description={t("createStrategyDesc")}
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
                <select
                  value={draft.driver}
                  onChange={(e) => setDraft({ ...draft, driver: e.target.value as DraftForm["driver"] })}
                  className="input-dark"
                >
                  <option value="prompt">prompt</option>
                  <option value="script">script</option>
                </select>
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
          </Card>
        )}

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
                className="input-dark text-xs w-56"
              />
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="input-dark text-xs"
              >
                <option value="all">{t("allStatuses")}</option>
                {Object.entries(counts).map(([k, v]) => (
                  <option key={k} value={k}>
                    {k} ({v})
                  </option>
                ))}
              </select>
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
            <div className="embedded-list-scroll-lg grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
              {filtered.map((strategy) => (
                <StrategyCard key={strategy.id} strategy={strategy} />
              ))}
            </div>
          )}
        </Card>
      </PageBody>
    </div>
  );
}

function StrategyCard({ strategy }: { strategy: StrategyRecord }) {
  const t = useTranslations("strategies");
  const tone =
    strategy.status === "live"
      ? "ok"
      : strategy.status === "paper" || strategy.status === "canary"
      ? "brand"
      : strategy.status === "archived" || strategy.status === "paused"
      ? "neutral"
      : "neutral";
  return (
    <Link
      href={`/strategies/${encodeURIComponent(strategy.id)}`}
      className="group block rounded-xl border border-white/8 bg-bg-card hover:bg-white/[0.04] hover:border-brand-500/30 transition-colors duration-200 p-4 cursor-pointer"
      data-strategy-id={strategy.id}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="font-mono text-sm text-ink-100 truncate">{strategy.id}</div>
          <div className="text-[12px] text-ink-400 mt-0.5 truncate">
            {strategy.title || t("untitledStrategy")}
          </div>
        </div>
        <span className="text-brand-300/60 group-hover:text-brand-200 transition-colors text-xs leading-none">
          →
        </span>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <Pill tone={tone}>{strategy.status}</Pill>
        <Pill tone="brand">{strategy.mode}</Pill>
        <Pill tone="neutral">{t("acct")}: {strategy.account_id || "—"}</Pill>
      </div>
      <div className="mt-2 text-[11px] text-ink-500 font-mono truncate">
        {(strategy.markets || []).join(", ") || t("noMarkets")} ·{" "}
        {(strategy.trigger_kinds || []).join(", ") || t("noTriggers")}
      </div>
    </Link>
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
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="input-dark font-mono"
      >
        {accounts.map(({ profile }) => {
          const disabled = profile.status !== "active";
          const label = `${profile.id} · ${profile.venue} · ${profile.mode}` +
            (disabled ? ` (${profile.status})` : "");
          return (
            <option
              key={profile.id}
              value={profile.id}
              disabled={disabled}
              title={disabled ? t("accountStatus", { status: profile.status }) : undefined}
            >
              {label}
            </option>
          );
        })}
      </select>
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
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="input-dark font-mono"
    >
      {discovery.accounts.map((account) => (
        <option key={account.id} value={account.id}>
          {account.id} · {account.venue || account.exchange} · {account.mode}
        </option>
      ))}
    </select>
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
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="input-dark"
    >
      <option value="">{t("globalWallet")}</option>
      {bindings.map((binding) => {
        const probe = readyMap.get(binding.wallet_id);
        return (
          <option key={binding.wallet_id} value={binding.wallet_id}>
            {binding.label || binding.wallet_id} · {binding.provider}
            {binding.source === "legacy" ? ` ${t("legacyTag")}` : ""}
            {probe && !probe.ready ? ` ${t("notReadyDotTag")}` : ""}
          </option>
        );
      })}
      {/* Legacy fallback for installs that don't expose /wallet/configured yet */}
      {bindings.length === 0
        ? (discovery?.wallets?.providers ?? []).map((wallet) => (
            <option key={wallet.id} value={wallet.id}>
              {wallet.label || wallet.id}
              {wallet.ready ? "" : ` ${t("notReadyTag")}`}
            </option>
          ))
        : null}
    </select>
  );
}
