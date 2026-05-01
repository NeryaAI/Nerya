"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
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
import { clientApi } from "../../lib/clientApi";
import type { AccountSummary } from "../../lib/clientApi";
import { formatBalance } from "../../lib/currentAccount";

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
          ? window.prompt(`Reason for setting ${account_id} to ${next}?`, "manual")
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
    const raw = window.prompt(
      `Reset paper sandbox for ${aid}?\n\n` +
        "This wipes orders / fills / positions / reservations / snapshots / executor runs " +
        "for the account, and resets the virtual ledger. Strategy bindings stay.\n\n" +
        "Initial balance (USD) — leave blank to keep current:",
      String(currentBalance || ""),
    );
    if (raw == null) return;
    const trimmed = raw.trim();
    let initial: number | undefined;
    if (trimmed !== "") {
      const parsed = Number(trimmed);
      if (!Number.isFinite(parsed) || parsed < 0) {
        setError(`Invalid initial balance: ${trimmed}`);
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
        const proceed = window.confirm(
          `Account ${aid} still has ` +
            `${res.state.active_orders} order(s) / ` +
            `${res.state.open_positions} position(s) / ` +
            `${res.state.active_executors} executor(s).\n\n` +
            "Force reset anyway?",
        );
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
        title="Accounts"
        description="Configure trading accounts (CEX, DEX, perps, on-chain) with mode/permissions/limits/credentials. Multi-exchange + multi-paper + on-chain wallets all live here."
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowWizard((s) => !s)}
              className="btn-ghost text-xs"
            >
              {showWizard ? "Close wizard" : "+ Add exchange"}
            </button>
            <button
              onClick={() => setShowAdd((s) => !s)}
              className="btn-ghost text-xs"
            >
              {showAdd ? "Close form" : "+ Add account"}
            </button>
            <button
              onClick={load}
              disabled={loading}
              className="btn-ghost text-xs"
            >
              {loading ? "…" : "Refresh"}
            </button>
          </div>
        }
      />
      <SectionTabs section="trading" />
      <PageBody>
        {error && <ErrorBanner error={error} />}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Kpi label="Accounts" value={`${totals.total}`} tone="brand" />
          <Kpi
            label="Live"
            value={`${totals.live}`}
            tone={totals.live > 0 ? "warn" : "neutral"}
            delta={`${totals.quarantined} quarantined`}
          />
          <Kpi
            label="Open positions"
            value={`${totals.positions}`}
            delta={`${totals.protections} protection rule(s)`}
          />
          <Kpi
            label="Reserved (USD-eq)"
            value={money(totals.reserved, "USDT")}
            delta={`${totals.currencies.size} currency${
              totals.currencies.size === 1 ? "" : "s"
            }`}
          />
        </div>

        {showWizard ? (
          <ExchangeAuthorWizard
            onApproved={() => {
              setShowWizard(false);
            }}
          />
        ) : null}

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
          title="All accounts"
          description="One row per AccountProfile. Click the id to open the driver page."
        >
          {accounts.length === 0 ? (
            <Empty
              label={
                loading
                  ? "Loading…"
                  : "No accounts configured yet. Click + Add account."
              }
            />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>Id</th>
                    <th>Mode</th>
                    <th>Status</th>
                    <th>Venue</th>
                    <th>Wallet</th>
                    <th>Currency</th>
                    <th>Total</th>
                    <th>Reserved</th>
                    <th>Positions</th>
                    <th>Executors</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {accounts.map((acc) => {
                    const p = acc.profile;
                    return (
                      <tr key={p.id} className="text-xs">
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
                          {p.wallet_id || "—"}
                        </td>
                        <td className="font-mono text-ink-300">
                          {p.base_currency || "USDT"}
                        </td>
                        <td>{money(acc.snapshot?.total_usd, p.base_currency)}</td>
                        <td
                          className={
                            acc.reserved_usd > 0 ? "text-[#f5a524]" : ""
                          }
                        >
                          {money(acc.reserved_usd, p.base_currency)}
                        </td>
                        <td>{acc.open_position_count}</td>
                        <td>{acc.active_executors.length}</td>
                        <td className="flex flex-wrap gap-1.5">
                          {p.status === "active" ? (
                            <>
                              <button
                                onClick={() => quarantine(p.id, "quarantined")}
                                disabled={busy === `${p.id}:quarantined`}
                                className="btn-ghost text-[11px] py-0.5 text-[#ef4560]"
                              >
                                quarantine
                              </button>
                              <button
                                onClick={() => quarantine(p.id, "read_only")}
                                disabled={busy === `${p.id}:read_only`}
                                className="btn-ghost text-[11px] py-0.5 text-[#f5a524]"
                              >
                                read-only
                              </button>
                            </>
                          ) : (
                            <button
                              onClick={() => quarantine(p.id, "active")}
                              disabled={busy === `${p.id}:active`}
                              className="btn-ghost text-[11px] py-0.5 text-accent-300"
                            >
                              re-activate
                            </button>
                          )}
                          {p.mode === "paper" ? (
                            <button
                              onClick={() => void resetPaper(acc)}
                              disabled={busy === `${p.id}:reset`}
                              className="btn-ghost text-[11px] py-0.5 text-brand-200"
                              title="Wipe paper trading state and reset balance"
                            >
                              {busy === `${p.id}:reset` ? "…" : "reset paper"}
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
