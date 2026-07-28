"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Advanced, Card, Pill } from "../Page";
import { Select } from "../Select";
import {
  clientApi,
  type AccountSummary,
  type WalletBinding,
} from "../../lib/clientApi";
import { confirm as confirmDialog } from "../../lib/dialogs";

interface Props {
  strategyId: string;
  currentAccountId: string | null;
  currentWalletId: string | null;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
  onRefresh: () => Promise<void> | void;
}

/**
 * Strategy bind card.
 *
 * Pulls the live account roster from /accounts/list and the wallet
 * bindings from /wallet/configured so the operator can re-bind a
 * strategy without leaving the strategy workspace. Quarantined /
 * read_only / disabled accounts are still surfaced (so the operator
 * sees the full picture), but binding to anything other than
 * ``active`` is intentionally disabled — the risk gate would refuse
 * the resulting orders anyway.
 *
 * The select is always rendered against the *latest* control-plane
 * data so a freshly-added account or wallet shows up here as soon as
 * /accounts/upsert returns; no page reload required.
 */
export function StrategyBindCard({
  strategyId,
  currentAccountId,
  currentWalletId,
  onError,
  onNotice,
  onRefresh,
}: Props) {
  const t = useTranslations("strategyBind");
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [walletBindings, setWalletBindings] = useState<WalletBinding[]>([]);
  const [accountId, setAccountId] = useState<string>(currentAccountId ?? "");
  const [walletId, setWalletId] = useState<string>(currentWalletId ?? "");
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setAccountId(currentAccountId ?? "");
  }, [currentAccountId]);
  useEffect(() => {
    setWalletId(currentWalletId ?? "");
  }, [currentWalletId]);

  async function load() {
    setLoading(true);
    try {
      const [accList, wallets] = await Promise.all([
        clientApi.accountsList().catch(() => ({ accounts: [], ts: 0 })),
        clientApi.walletConfigured().catch(() => ({ bindings: [], count: 0 })),
      ]);
      setAccounts(accList.accounts ?? []);
      setWalletBindings(wallets.bindings ?? []);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const selectedAccount = accounts.find((a) => a.profile.id === accountId);
  const accountDirty = accountId && accountId !== (currentAccountId ?? "");
  const walletDirty = (walletId || "") !== (currentWalletId || "");
  const accountBlocked =
    selectedAccount && selectedAccount.profile.status !== "active";

  async function applyAccount() {
    if (!accountDirty) return;
    // Pre-flight soft warning: if the target
    // account is already bound to a non-archived strategy, surface
    // a confirm dialog *before* we hit /strategy/bind_account so the
    // operator can either back out or be reminded that
    // PnL / capital reservations / snapshots are shared once two
    // strategies share an account. The dialog explicitly recommends
    // setting up an exchange sub-account, which is what the backend
    // warning text says too — keeping the two in sync.
    if (selectedAccount) {
      const shared = (selectedAccount.bound_strategies || []).filter(
        (entry) => entry.strategy_id && entry.strategy_id !== strategyId,
      );
      if (shared.length > 0) {
        const names = shared
          .slice(0, 5)
          .map((entry) => entry.strategy_id)
          .join(", ");
        const proceed = await confirmDialog({
          title: t("shareWarningTitle"),
          message: t("shareWarningMessage", {
            count: shared.length,
            accountId,
            strategies: names,
          }),
          okLabel: t("shareWarningContinue"),
          cancelLabel: t("shareWarningCancel"),
          tone: "warning",
        });
        if (!proceed) return;
      }
    }
    setBusy("account");
    onError(null);
    try {
      const res = await clientApi.strategyBindAccount(strategyId, accountId);
      if (!res.ok) throw new Error("strategy_bind_account_failed");
      if (res.warning && res.warning.code === "account_already_bound") {
        onNotice(
          t("boundAccountWithWarning", {
            strategyId,
            accountId,
            count: res.warning.strategies?.length ?? 0,
          }),
        );
      } else {
        onNotice(t("boundAccount", { strategyId, accountId }));
      }
      await onRefresh();
      void load();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function applyWallet() {
    if (!walletDirty) return;
    setBusy("wallet");
    onError(null);
    try {
      const res = await clientApi.strategyBindWallet(
        strategyId,
        walletId.trim() || null,
      );
      if (!res.ok) throw new Error("strategy_bind_wallet_failed");
      onNotice(
        walletId.trim()
          ? t("boundWallet", { strategyId, walletId: walletId.trim() })
          : t("clearedWallet", { strategyId }),
      );
      await onRefresh();
      void load();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const boundAccount = accounts.find(
    (a) => a.profile.id === (currentAccountId ?? ""),
  );

  return (
    // No "Refresh roster" button — the roster loads on mount and reloads
    // after every bind, so the manual refresh only duplicated that.
    <Card title={t("title")} description={t("description")}>
      {/* Read-only summary first: most visits just check the wiring. */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
        <div className="flex items-center gap-2">
          <span className="text-ink-400">{t("account")}</span>
          <span className="font-mono text-ink-200">{currentAccountId || "–"}</span>
          {boundAccount ? (
            <Pill
              tone={
                boundAccount.profile.status === "active"
                  ? "ok"
                  : boundAccount.profile.status === "read_only"
                  ? "warn"
                  : "danger"
              }
            >
              {boundAccount.profile.mode} · {boundAccount.profile.status}
            </Pill>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-ink-400">{t("wallet")}</span>
          <span className="font-mono text-ink-200">
            {currentWalletId || t("accountFallbackInline")}
          </span>
        </div>
      </div>

      <Advanced title={t("changeBindings")}>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
        <div>
          <label className="text-[11px] text-ink-400">{t("account")}</label>
          <div className="mt-1 flex gap-2">
            <div className="flex-1">
              <Select
                value={accountId}
                onChange={(value) => setAccountId(value)}
                disabled={accounts.length === 0}
                options={(() => {
                  const opts = [] as Array<{
                    value: string;
                    label: string;
                    disabled?: boolean;
                  }>;
                  if (accounts.length === 0) {
                    opts.push({ value: "", label: t("noAccounts") });
                  } else if (!accountId) {
                    opts.push({ value: "", label: t("pickAccount") });
                  }
                  for (const { profile } of accounts) {
                    const disabled = profile.status !== "active";
                    opts.push({
                      value: profile.id,
                      disabled,
                      label: `${profile.id} · ${profile.venue} · ${profile.mode}${
                        disabled ? ` (${profile.status})` : ""
                      }`,
                    });
                  }
                  return opts;
                })()}
                size="sm"
                ariaLabel={t("account")}
                className="font-mono"
              />
            </div>
            <button
              onClick={() => void applyAccount()}
              disabled={!accountDirty || Boolean(accountBlocked) || busy !== null}
              className="btn-primary text-xs px-3"
            >
              {busy === "account" ? t("binding") : t("bind")}
            </button>
          </div>
          <div className="mt-1.5 flex items-center gap-2 text-[11px] text-ink-400">
            <span>{t("current")}</span>
            <span className="font-mono text-ink-200">
              {currentAccountId || "–"}
            </span>
            {selectedAccount ? (
              <Pill
                tone={
                  selectedAccount.profile.status === "active"
                    ? "ok"
                    : selectedAccount.profile.status === "read_only"
                    ? "warn"
                    : "danger"
                }
              >
                {selectedAccount.profile.mode} · {selectedAccount.profile.status}
              </Pill>
            ) : null}
          </div>
          {accountBlocked ? (
            <div className="mt-1 text-[11px] text-danger">
              {t("cannotBindPrefix")}{" "}
              <span className="font-mono">
                {selectedAccount!.profile.status}
              </span>
              {t("cannotBindSuffix")}
            </div>
          ) : null}
        </div>
        <div>
          <label className="text-[11px] text-ink-400">{t("wallet")}</label>
          <div className="mt-1 flex gap-2">
            <div className="flex-1">
              <Select
                value={walletId}
                onChange={(value) => setWalletId(value)}
                options={[
                  { value: "", label: t("walletFallback") },
                  ...walletBindings.map((binding) => ({
                    value: binding.wallet_id,
                    label: `${binding.label || binding.wallet_id} · ${binding.provider}${
                      binding.source === "legacy" ? ` (${t("legacyTag")})` : ""
                    }`,
                  })),
                ]}
                size="sm"
                ariaLabel={t("wallet")}
              />
            </div>
            <button
              onClick={() => void applyWallet()}
              disabled={!walletDirty || busy !== null}
              className="btn-primary text-xs px-3"
            >
              {busy === "wallet" ? t("binding") : t("bind")}
            </button>
          </div>
          <div className="mt-1.5 text-[11px] text-ink-400">
            {t("current")}{" "}
            <span className="font-mono text-ink-200">
              {currentWalletId || t("accountFallbackInline")}
            </span>
          </div>
          {walletBindings.length === 0 ? (
            <div className="mt-1 text-[11px] text-ink-500">
              {t("noWalletsPrefix")}{" "}
              <span className="font-mono">wallet.providers</span>{" "}
              {t("noWalletsSuffix")}
            </div>
          ) : null}
        </div>
      </div>
      </Advanced>
    </Card>
  );
}
