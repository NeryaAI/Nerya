"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, Pill } from "../Page";
import {
  clientApi,
  type AccountSummary,
  type WalletBinding,
} from "../../lib/clientApi";

interface Props {
  strategyId: string;
  currentAccountId: string | null;
  currentWalletId: string | null;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
  onRefresh: () => Promise<void> | void;
}

/**
 * Strategy bind card (Plan 2026-04-29 §11 P9).
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
    setBusy("account");
    onError(null);
    try {
      const res = await clientApi.strategyBindAccount(strategyId, accountId);
      if (!res.ok) throw new Error("strategy_bind_account_failed");
      onNotice(t("boundAccount", { strategyId, accountId }));
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

  return (
    <Card
      title={t("title")}
      description={t("description")}
      actions={
        <button
          onClick={() => void load()}
          disabled={loading}
          className="btn-ghost text-xs"
        >
          {loading ? "…" : t("refreshRoster")}
        </button>
      }
    >
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
        <div>
          <label className="text-[11px] text-ink-400">{t("account")}</label>
          <div className="mt-1 flex gap-2">
            <select
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className="input-dark font-mono flex-1"
              disabled={accounts.length === 0}
            >
              {accounts.length === 0 ? (
                <option value="">{t("noAccounts")}</option>
              ) : null}
              {!accountId && accounts.length > 0 ? (
                <option value="">{t("pickAccount")}</option>
              ) : null}
              {accounts.map(({ profile }) => {
                const disabled = profile.status !== "active";
                return (
                  <option
                    key={profile.id}
                    value={profile.id}
                    disabled={disabled}
                    title={
                      disabled ? t("accountIs", { status: profile.status }) : undefined
                    }
                  >
                    {profile.id} · {profile.venue} · {profile.mode}
                    {disabled ? ` (${profile.status})` : ""}
                  </option>
                );
              })}
            </select>
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
              {currentAccountId || "—"}
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
            <div className="mt-1 text-[11px] text-[#ef4560]">
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
            <select
              value={walletId}
              onChange={(e) => setWalletId(e.target.value)}
              className="input-dark flex-1"
            >
              <option value="">{t("walletFallback")}</option>
              {walletBindings.map((binding) => (
                <option key={binding.wallet_id} value={binding.wallet_id}>
                  {binding.label || binding.wallet_id} · {binding.provider}
                  {binding.source === "legacy" ? ` (${t("legacyTag")})` : ""}
                </option>
              ))}
            </select>
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
    </Card>
  );
}
