"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
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
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import { AccountProposalsCard } from "../../components/accounts/AccountProposalsCard";
import { AddAccountForm } from "../../components/accounts/AddAccountForm";
import { ExchangeAuthorWizard } from "../../components/accounts/ExchangeAuthorWizard";
import { WalletProviderPanel } from "../../components/accounts/WalletProviderPanel";
import { clientApi } from "../../lib/clientApi";
import type { AccountSummary } from "../../lib/clientApi";
import { formatBalance } from "../../lib/currentAccount";
import { confirm as confirmDialog, prompt as promptDialog } from "../../lib/dialogs";

function money(value: unknown, currency: string = "USDT"): string {
  return formatBalance(value, currency);
}

function modePill(
  mode: string,
): "ok" | "warn" | "danger" | "brand" | "neutral" {
  switch (mode) {
    case "live":
      return "danger";
    case "canary":
      return "warn";
    case "shadow":
      return "brand";
    case "paper":
      return "ok";
    default:
      return "neutral";
  }
}

function statusPill(
  status: string,
): "ok" | "warn" | "danger" | "neutral" {
  switch (status) {
    case "active":
      return "ok";
    case "read_only":
      return "warn";
    case "quarantined":
    case "disabled":
      return "danger";
    default:
      return "neutral";
  }
}

export default function AccountsPage() {
  const t = useTranslations("accounts");
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [showWizard, setShowWizard] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await clientApi.accountsList();
      setAccounts(res.accounts || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
  }, []);

  async function quarantine(
    account_id: string,
    next: "active" | "quarantined" | "disabled" | "read_only",
  ) {
    setBusy(`${account_id}:${next}`);
    try {
      const reason =
        next !== "active"
          ? await promptDialog({
              message: t("reasonPrompt", { id: account_id, status: next }),
              defaultValue: t("manual"),
            })
          : undefined;
      if (next !== "active" && reason == null) {
        setBusy(null);
        return;
      }
      const res = await clientApi.accountsQuarantine({
        account_id,
        status: next,
        reason: reason || undefined,
        operator: "dashboard",
      });
      if (!res.ok) throw new Error(res.detail || res.error || "failed");
      if (res.account) {
        setAccounts((prev) =>
          prev.map((a) =>
            a.profile.id === account_id ? res.account! : a,
          ),
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function resetPaper(acc: AccountSummary) {
    const aid = acc.profile.id;
    if (acc.profile.mode !== "paper") return;
    const currentBalance = Number(acc.profile.initial_balance_usd) || 0;
    const raw = await promptDialog({
      message: t("resetPrompt", { id: aid }),
      defaultValue: String(currentBalance || ""),
    });
    if (raw == null) return;
    const trimmed = raw.trim();
    let initial: number | undefined;
    if (trimmed !== "") {
      const parsed = Number(trimmed);
      if (!Number.isFinite(parsed) || parsed < 0) {
        setError(t("invalidBalance", { value: trimmed }));
        return;
      }
      initial = parsed;
    }
    setBusy(`${aid}:reset`);
    try {
      let res = await clientApi.accountsResetPaper({
        account_id: aid,
        initial_balance_usd: initial,
        operator: "dashboard",
      });
      if (!res.ok && res.error === "account_busy" && res.state) {
        const proceed = await confirmDialog({
          message: t("accountBusy", {
            id: aid,
            orders: res.state.active_orders,
            positions: res.state.open_positions,
            executors: res.state.active_executors,
          }),
          tone: "danger",
        });
        if (!proceed) {
          setBusy(null);
          return;
        }
        res = await clientApi.accountsResetPaper({
          account_id: aid,
          initial_balance_usd: initial,
          force: true,
          operator: "dashboard",
        });
      }
      if (!res.ok) throw new Error(res.detail || res.error || "failed");
      if (res.account) {
        setAccounts((prev) =>
          prev.map((a) => (a.profile.id === aid ? res.account! : a)),
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const totals = accounts.reduce(
    (acc, a) => {
      acc.total += 1;
      if (a.profile.live_trading_enabled) acc.live += 1;
      if (a.profile.status === "quarantined") acc.quarantined += 1;
      acc.executors += a.active_executors.length;
      acc.positions += a.open_position_count;
      acc.protections += a.protection_count;
      acc.reserved += Number(a.reserved_usd || 0);
      acc.currencies.add(a.profile.base_currency || "USDT");
      return acc;
    },
    {
      total: 0,
      live: 0,
      quarantined: 0,
      executors: 0,
      positions: 0,
      protections: 0,
      reserved: 0,
      currencies: new Set<string>(),
    },
  );

  return (
    <div>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowWizard((s) => !s)}
              className="btn-ghost text-xs"
            >
              {showWizard ? t("closeWizard") : t("addExchange")}
            </button>
            <button
              onClick={() => setShowAdd((s) => !s)}
              className="btn-ghost text-xs"
            >
              {showAdd ? t("closeForm") : t("addAccount")}
            </button>
            <button
              onClick={load}
              disabled={loading}
              className="btn-ghost text-xs"
            >
              {loading ? "…" : t("refresh")}
            </button>
          </div>
        }
      />
      <SectionTabs section="trading" />
      <PageBody>
        {error && <ErrorBanner error={error} />}

        <div className="flex flex-wrap items-end gap-x-8 gap-y-3 px-1">
          <Kpi inline label={t("accountsKpi")} value={`${totals.total}`} tone="brand" />
          <Kpi
            inline
            label={t("live")}
            value={`${totals.live}`}
            tone={totals.live > 0 ? "warn" : "neutral"}
            delta={t("quarantined", { count: totals.quarantined })}
          />
          <Kpi
            inline
            label={t("openPositions")}
            value={`${totals.positions}`}
            delta={t("protectionRules", { count: totals.protections })}
          />
          <Kpi
            inline
            label={t("reservedUsd")}
            value={money(totals.reserved, "USDT")}
            delta={
              totals.currencies.size === 1
                ? t("currencyCount", { count: totals.currencies.size })
                : t("currencyCountPlural", { count: totals.currencies.size })
            }
          />
        </div>

        {showWizard ? (
          <ExchangeAuthorWizard
            onApproved={() => {
              setShowWizard(false);
            }}
          />
        ) : null}

        <Advanced
          title={t("walletPanelTitle")}
          description={t("walletPanelDesc")}
          storageKey="nerya.accounts.advanced.wallet"
        >
          <WalletProviderPanel bare onChanged={() => void load()} />
        </Advanced>

        {showAdd ? (
          <AddAccountForm
            onCancel={() => setShowAdd(false)}
            onSaved={(account) => {
              setShowAdd(false);
              setAccounts((prev) => {
                const idx = prev.findIndex(
                  (a) => a.profile.id === account.profile.id,
                );
                if (idx === -1) return [...prev, account];
                const copy = prev.slice();
                copy[idx] = account;
                return copy;
              });
            }}
            onProposed={() => {
              setShowAdd(false);
            }}
          />
        ) : null}

        <AccountProposalsCard onApplied={() => void load()} />

        <Card
          title={t("allAccounts")}
          description={t("allAccountsDesc")}
        >
          {accounts.length === 0 ? (
            <Empty
              label={
                loading
                  ? t("loading")
                  : t("noAccounts")
              }
            />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>{t("colId")}</th>
                    <th>{t("colMode")}</th>
                    <th>{t("colStatus")}</th>
                    <th>{t("colVenue")}</th>
                    <th>{t("colWallet")}</th>
                    <th>{t("colCurrency")}</th>
                    <th>{t("colTotal")}</th>
                    <th>{t("colReserved")}</th>
                    <th>{t("colPositions")}</th>
                    <th>{t("colExecutors")}</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {accounts.map((acc) => {
                    const p = acc.profile;
                    return (
                      <tr key={p.id} className="group text-xs">
                        <td>
                          <Link
                            href={`/accounts/${encodeURIComponent(p.id)}`}
                            className="font-mono text-brand-200 hover:text-brand-100"
                          >
                            {p.id}
                          </Link>
                        </td>
                        <td>
                          <Pill tone={modePill(p.mode)}>{p.mode}</Pill>
                        </td>
                        <td>
                          <Pill tone={statusPill(p.status)}>{p.status}</Pill>
                        </td>
                        <td className="font-mono">{p.venue}</td>
                        <td className="font-mono text-ink-400">
                          {p.wallet_id || "–"}
                        </td>
                        <td className="font-mono text-ink-300">
                          {p.base_currency || "USDT"}
                        </td>
                        <td>{money(acc.snapshot?.total_usd, p.base_currency)}</td>
                        <td
                          className={
                            acc.reserved_usd > 0 ? "text-warn" : ""
                          }
                        >
                          {money(acc.reserved_usd, p.base_currency)}
                        </td>
                        <td>{acc.open_position_count}</td>
                        <td>{acc.active_executors.length}</td>
                        {/* Row actions (incl. the destructive quarantine)
                            reveal on hover/focus so nine rows don't render
                            27 permanent buttons. */}
                        <td className="flex flex-wrap gap-1.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                          {p.status === "active" ? (
                            <>
                              <button
                                onClick={() => quarantine(p.id, "quarantined")}
                                disabled={busy === `${p.id}:quarantined`}
                                className="btn-ghost text-[11px] py-0.5 text-danger"
                              >
                                {t("quarantineBtn")}
                              </button>
                              <button
                                onClick={() => quarantine(p.id, "read_only")}
                                disabled={busy === `${p.id}:read_only`}
                                className="btn-ghost text-[11px] py-0.5 text-warn"
                              >
                                {t("readOnlyBtn")}
                              </button>
                            </>
                          ) : (
                            <button
                              onClick={() => quarantine(p.id, "active")}
                              disabled={busy === `${p.id}:active`}
                              className="btn-ghost text-[11px] py-0.5 text-accent-300"
                            >
                              {t("reactivate")}
                            </button>
                          )}
                          {p.mode === "paper" ? (
                            <button
                              onClick={() => void resetPaper(acc)}
                              disabled={busy === `${p.id}:reset`}
                              className="btn-ghost text-[11px] py-0.5 text-brand-200"
                              title={t("resetPaperTitle")}
                            >
                              {busy === `${p.id}:reset` ? "…" : t("resetPaper")}
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </PageBody>
    </div>
  );
}
