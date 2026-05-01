"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  Card,
  Empty,
  ErrorBanner,
  Json,
  Kpi,
  PageBody,
  PageHeader,
  Pill,
} from "../../../components/Page";
import { SectionTabs } from "../../../components/SectionTabs";
import { clientApi } from "../../../lib/clientApi";
import type {
  AccountSummary,
  ControlPlaneOrder,
  ReconciliationReport,
} from "../../../lib/clientApi";
import { formatBalance } from "../../../lib/currentAccount";
import { formatTsShort } from "../../../lib/format";

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

function severityTone(
  severity?: string,
): "ok" | "warn" | "danger" | "neutral" | "brand" {
  switch (severity) {
    case "info":
      return "ok";
    case "warning":
      return "warn";
    case "action_required":
    case "trading_halted":
      return "danger";
    default:
      return "neutral";
  }
}

type AuthHeader = { key: string; value: string; kind: string };

type WalletPortfolioRow = {
  account_id: string;
  wallet_id: string;
  venue: string;
  mode: string;
  ts: number;
  source: string;
  health: string;
  nav_usd: number;
  free_by_asset: Record<string, number>;
  cash_by_asset: Record<string, number>;
  meta: Record<string, unknown>;
};

function fmtTs(ts: unknown): string {
  if (ts == null) return "—";
  if (typeof ts === "string") return formatTsShort(ts);
  const seconds = Number(ts);
  if (!Number.isFinite(seconds)) return String(ts);
  const ms = seconds > 1e12 ? seconds : seconds * 1000;
  return formatTsShort(new Date(ms).toISOString());
}

export default function AccountDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const accountId = decodeURIComponent(params.id);
  const [summary, setSummary] = useState<AccountSummary | null>(null);
  const [orders, setOrders] = useState<ControlPlaneOrder[]>([]);
  const [reports, setReports] = useState<ReconciliationReport[]>([]);
  const [headers, setHeaders] = useState<AuthHeader[]>([]);
  const [newHeaderKey, setNewHeaderKey] = useState("");
  const [newHeaderValue, setNewHeaderValue] = useState("");
  const [walletPortfolio, setWalletPortfolio] =
    useState<WalletPortfolioRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  async function loadHeaders() {
    try {
      const res = await clientApi.accountsHeadersList({
        account_id: accountId,
      });
      if (res.ok) setHeaders(res.headers || []);
    } catch (e) {
      // Non-fatal: account may not have headers configured.
      void e;
    }
  }

  async function loadWalletPortfolio() {
    try {
      const res = await clientApi.walletPortfolio({ account_id: accountId });
      if (res.ok && res.accounts && res.accounts.length > 0) {
        setWalletPortfolio(res.accounts[0]);
      } else {
        setWalletPortfolio(null);
      }
    } catch (e) {
      void e;
    }
  }

  async function patchHeaders(patch: Record<string, string | null>) {
    setBusy("headers");
    setError(null);
    try {
      const res = await clientApi.accountsHeadersPatch({
        account_id: accountId,
        headers: patch,
        operator: "dashboard",
      });
      if (!res.ok) {
        throw new Error(res.detail || res.error || "headers_patch_failed");
      }
      setHeaders(res.headers || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function addHeader() {
    const key = newHeaderKey.trim();
    const value = newHeaderValue.trim();
    if (!key || !value) {
      setError(
        "Header key and value are required. Use vault://<name> to reference a secret stored via /security/secrets/put.",
      );
      return;
    }
    await patchHeaders({ [key]: value });
    setNewHeaderKey("");
    setNewHeaderValue("");
  }

  async function removeHeader(key: string) {
    if (!confirm(`Remove header ${key}?`)) return;
    await patchHeaders({ [key]: null });
  }

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [acctRes, ordersRes, reportsRes] = await Promise.all([
        clientApi.accountsGet(accountId),
        clientApi.controlOrdersList({
          account_id: accountId,
          state: "recent",
          limit: 50,
        }),
        clientApi.controlReconciliationReports({
          account_id: accountId,
          limit: 12,
        }),
      ]);
      if (!acctRes.ok || !acctRes.account) {
        setError(acctRes.error || "account_not_found");
      } else {
        setSummary(acctRes.account);
      }
      setOrders(ordersRes.orders || []);
      setReports(reportsRes.reports || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    void loadHeaders();
    void loadWalletPortfolio();
    const t = setInterval(() => {
      void load();
      void loadWalletPortfolio();
    }, 30_000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId]);

  async function setStatus(
    next: "active" | "read_only" | "disabled" | "quarantined",
  ) {
    if (!summary) return;
    if (next !== "active") {
      const reason = window.prompt(
        `Set ${accountId} status to ${next}. Reason?`,
        "manual_operator",
      );
      if (reason == null) return;
      setBusy(`status:${next}`);
      try {
        const res = await clientApi.accountsQuarantine({
          account_id: accountId,
          status: next,
          reason,
          operator: "dashboard",
        });
        if (!res.ok) throw new Error(res.detail || res.error || "failed");
        if (res.account) setSummary(res.account);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
      return;
    }
    setBusy(`status:${next}`);
    try {
      const res = await clientApi.accountsQuarantine({
        account_id: accountId,
        status: next,
        operator: "dashboard",
      });
      if (!res.ok) throw new Error(res.detail || res.error || "failed");
      if (res.account) setSummary(res.account);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function runReconcile() {
    setBusy("reconcile");
    try {
      const res = await clientApi.controlReconciliationRun({
        account_id: accountId,
        operator: "dashboard",
      });
      if (res.report) setReports((prev) => [res.report, ...prev].slice(0, 12));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function resetPaper() {
    if (!summary) return;
    if (summary.profile.mode !== "paper") return;
    const currentBalance = Number(summary.profile.initial_balance_usd) || 0;
    const raw = window.prompt(
      `Reset paper sandbox for ${accountId}?\n\n` +
        "This wipes orders / fills / positions / reservations / snapshots / " +
        "executor runs for the account, and resets the virtual ledger. " +
        "Strategy bindings stay.\n\n" +
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
    setBusy("reset_paper");
    try {
      let res = await clientApi.accountsResetPaper({
        account_id: accountId,
        initial_balance_usd: initial,
        operator: "dashboard",
      });
      if (!res.ok && res.error === "account_busy" && res.state) {
        const proceed = window.confirm(
          `Account ${accountId} still has ${res.state.active_orders} order(s) / ` +
            `${res.state.open_positions} position(s) / ` +
            `${res.state.active_executors} executor(s).\n\n` +
            "Force reset anyway?",
        );
        if (!proceed) {
          setBusy(null);
          return;
        }
        res = await clientApi.accountsResetPaper({
          account_id: accountId,
          initial_balance_usd: initial,
          force: true,
          operator: "dashboard",
        });
      }
      if (!res.ok) throw new Error(res.detail || res.error || "reset_failed");
      if (res.account) setSummary(res.account);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function deleteAccount(force = false) {
    if (
      !confirm(
        force
          ? `Force-delete ${accountId}? Active state will be ignored.`
          : `Delete ${accountId}?`,
      )
    )
      return;
    setBusy("delete");
    try {
      const res = await clientApi.accountsDelete({
        account_id: accountId,
        force,
        operator: "dashboard",
      });
      if (!res.ok) {
        if (res.state) {
          alert(
            `Cannot delete: open_positions=${res.state.open_positions}, active_executors=${res.state.active_executors}, active_orders=${res.state.active_orders}. Use 'force' to override.`,
          );
        } else {
          throw new Error(res.detail || res.error || "delete_failed");
        }
      } else {
        window.location.href = "/portfolio";
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  if (loading && !summary) {
    return (
      <div>
        <PageHeader title={`Account · ${accountId}`} description="Loading…" />
        <SectionTabs section="trading" />
        <PageBody>
          <Empty label="Loading account…" />
        </PageBody>
      </div>
    );
  }

  const profile = summary?.profile;
  const snapshot = summary?.snapshot;

  return (
    <div>
      <PageHeader
        title={`Account · ${accountId}`}
        description={
          profile
            ? `${profile.mode.toUpperCase()} on ${profile.venue} (${profile.kind}). Wallet binding: ${profile.wallet_id || "—"}.`
            : "Account detail"
        }
        actions={
          <div className="flex items-center gap-2">
            <Link href="/portfolio" className="btn-ghost text-xs">
              ← Portfolio
            </Link>
            <button
              onClick={runReconcile}
              disabled={busy === "reconcile"}
              className="btn-ghost text-xs"
            >
              {busy === "reconcile" ? "…" : "Reconcile"}
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
        {!profile ? (
          <Empty label="Account not found." />
        ) : (
          <>
            <div className="flex items-center gap-3 flex-wrap">
              <Pill tone={modePill(profile.mode)}>{profile.mode}</Pill>
              <Pill tone={statusPill(profile.status)}>{profile.status}</Pill>
              {profile.live_trading_enabled ? (
                <Pill tone="warn">live trading enabled</Pill>
              ) : (
                <Pill tone="brand">live trading off</Pill>
              )}
              {profile.kind ? (
                <span className="text-xs text-ink-300 font-mono">
                  kind={profile.kind}
                </span>
              ) : null}
              <span className="ml-auto flex items-center gap-2">
                {profile.status === "active" ? (
                  <>
                    <button
                      onClick={() => setStatus("read_only")}
                      disabled={busy?.startsWith("status:")}
                      className="btn-ghost text-xs text-[#f5a524]"
                    >
                      Set read_only
                    </button>
                    <button
                      onClick={() => setStatus("quarantined")}
                      disabled={busy?.startsWith("status:")}
                      className="btn-ghost text-xs text-[#ef4560]"
                    >
                      Quarantine
                    </button>
                    <button
                      onClick={() => setStatus("disabled")}
                      disabled={busy?.startsWith("status:")}
                      className="btn-ghost text-xs"
                    >
                      Disable
                    </button>
                  </>
                ) : (
                  <button
                    onClick={() => setStatus("active")}
                    disabled={busy?.startsWith("status:")}
                    className="btn-ghost text-xs text-accent-300"
                  >
                    Re-activate
                  </button>
                )}
                {profile.mode === "paper" ? (
                  <button
                    onClick={() => void resetPaper()}
                    disabled={busy === "reset_paper"}
                    className="btn-ghost text-xs text-brand-200"
                    title="Wipe paper trading state and reset balance"
                  >
                    {busy === "reset_paper" ? "Resetting…" : "Reset paper"}
                  </button>
                ) : null}
                <button
                  onClick={() => deleteAccount(false)}
                  disabled={busy === "delete"}
                  className="btn-ghost text-xs text-[#ef4560]"
                >
                  Delete
                </button>
              </span>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Kpi
                label={`Total (${profile.base_currency || "USDT"})`}
                value={money(snapshot?.total_usd, profile.base_currency)}
                tone="brand"
              />
              <Kpi
                label="Free"
                value={money(
                  snapshot?.free_usd ?? snapshot?.available_usd,
                  profile.base_currency,
                )}
              />
              <Kpi
                label="Reserved"
                value={money(summary?.reserved_usd, profile.base_currency)}
                tone={(summary?.reserved_usd ?? 0) > 0 ? "warn" : "neutral"}
              />
              <Kpi
                label="Snapshot"
                value={snapshot?.health || "—"}
                tone={
                  snapshot?.health === "ok"
                    ? "ok"
                    : snapshot?.health
                      ? "warn"
                      : "neutral"
                }
                delta={snapshot ? fmtTs(snapshot.ts) : "no snapshot"}
              />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <Card
                title="Permissions & limits"
                description="What this account is allowed to do, and the per-account guard rails."
              >
                <div className="grid grid-cols-2 gap-3 text-xs">
                  <div>
                    <div className="text-ink-500 mb-1">Permissions</div>
                    <ul className="embedded-list-scroll-sm space-y-1">
                      {Object.entries(profile.permissions).map(([k, v]) => (
                        <li
                          key={k}
                          className="flex items-center justify-between gap-2 font-mono"
                        >
                          <span className="text-ink-300">{k}</span>
                          <Pill
                            tone={
                              k === "withdraw"
                                ? v
                                  ? "danger"
                                  : "ok"
                                : v
                                  ? "ok"
                                  : "neutral"
                            }
                          >
                            {v ? "yes" : "no"}
                          </Pill>
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <div className="text-ink-500 mb-1">Limits</div>
                    <ul className="embedded-list-scroll-sm space-y-1 font-mono">
                      {Object.entries(profile.limits || {}).map(([k, v]) => (
                        <li
                          key={k}
                          className="flex items-center justify-between gap-2"
                        >
                          <span className="text-ink-300">{k}</span>
                          <span className="text-ink-100">
                            {Number(v).toLocaleString(undefined, {
                              maximumFractionDigits: 4,
                            })}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              </Card>

              <Card
                title="Credentials"
                description="vault:// references only. Plaintext is rejected at upsert. Use Settings → Integrations to manage the secrets."
              >
                {Object.keys(profile.credentials).length === 0 ? (
                  <Empty label="No credentials bound (paper or shadow account)." />
                ) : (
                  <ul className="embedded-list-scroll-sm text-xs font-mono space-y-1">
                    {Object.entries(profile.credentials).map(([k, v]) => (
                      <li
                        key={k}
                        className="flex items-center justify-between gap-2"
                      >
                        <span className="text-ink-300">{k}</span>
                        <span className="text-brand-300">{v}</span>
                      </li>
                    ))}
                  </ul>
                )}
                <div className="mt-3 text-xs text-ink-400">
                  Wallet binding:{" "}
                  <span className="font-mono text-ink-200">
                    {profile.wallet_id || "(none)"}
                  </span>
                </div>
              </Card>
            </div>

            <Card
              title="HTTP auth headers"
              description="Custom auth headers for data-source / REST connectors. Plaintext keys are refused — store the secret with /security/secrets/put first and reference it as vault://<name>."
            >
              {headers.length === 0 ? (
                <Empty label="No custom auth headers configured." />
              ) : (
                <ul className="embedded-list-scroll-sm text-xs font-mono space-y-1">
                  {headers.map((h) => (
                    <li
                      key={h.key}
                      className="flex items-center justify-between gap-2 border border-brand-500/10 rounded px-2 py-1"
                    >
                      <span className="text-ink-300">{h.key}</span>
                      <span className="text-brand-300">{h.value}</span>
                      <Pill
                        tone={h.kind === "vault_ref" ? "ok" : "warn"}
                      >
                        {h.kind}
                      </Pill>
                      <button
                        onClick={() => void removeHeader(h.key)}
                        disabled={busy === "headers"}
                        className="btn-ghost text-[11px] text-[#ef4560]"
                      >
                        Remove
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-3 flex flex-col gap-2 md:flex-row md:items-center">
                <input
                  className="input text-xs font-mono md:w-48"
                  placeholder="X-API-Key"
                  value={newHeaderKey}
                  onChange={(e) => setNewHeaderKey(e.target.value)}
                  disabled={busy === "headers"}
                />
                <input
                  className="input text-xs font-mono flex-1"
                  placeholder="vault://my_token  or  Bearer vault://my_token"
                  value={newHeaderValue}
                  onChange={(e) => setNewHeaderValue(e.target.value)}
                  disabled={busy === "headers"}
                />
                <button
                  onClick={() => void addHeader()}
                  disabled={busy === "headers"}
                  className="btn-ghost text-xs text-brand-200"
                >
                  {busy === "headers" ? "…" : "Add / update"}
                </button>
              </div>
            </Card>

            {profile.wallet_id ? (
              <Card
                title="Wallet portfolio"
                description="Live balances pulled from the bound on-chain wallet provider. Configure addresses under provider_config.balances on the account row."
              >
                {walletPortfolio == null ? (
                  <Empty label="No wallet snapshot yet — install the wallet provider, log in, and configure provider_config.balances." />
                ) : (
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 flex-wrap text-xs">
                      <Pill
                        tone={
                          walletPortfolio.health === "ok"
                            ? "ok"
                            : walletPortfolio.health
                              ? "warn"
                              : "neutral"
                        }
                      >
                        {walletPortfolio.health}
                      </Pill>
                      <span className="font-mono text-ink-300">
                        wallet={walletPortfolio.wallet_id}
                      </span>
                      <span className="font-mono text-ink-400">
                        source={walletPortfolio.source}
                      </span>
                      <span className="ml-auto font-mono text-ink-500">
                        {fmtTs(walletPortfolio.ts)}
                      </span>
                    </div>
                    <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                      <Kpi
                        label="NAV (stablecoin)"
                        value={money(
                          walletPortfolio.nav_usd,
                          profile.base_currency,
                        )}
                        tone="brand"
                      />
                      <Kpi
                        label="Distinct assets"
                        value={String(
                          Object.keys(walletPortfolio.free_by_asset || {})
                            .length,
                        )}
                      />
                      <Kpi
                        label="Mode"
                        value={walletPortfolio.mode}
                      />
                    </div>
                    {Object.keys(walletPortfolio.free_by_asset || {}).length >
                    0 ? (
                      <ul className="embedded-list-scroll-sm text-xs font-mono space-y-1">
                        {Object.entries(walletPortfolio.free_by_asset).map(
                          ([asset, amount]) => (
                            <li
                              key={asset}
                              className="flex items-center justify-between gap-2 border border-brand-500/10 rounded px-2 py-1"
                            >
                              <span className="text-ink-300">{asset}</span>
                              <span className="text-ink-100">
                                {Number(amount).toLocaleString(undefined, {
                                  maximumFractionDigits: 8,
                                })}
                              </span>
                            </li>
                          ),
                        )}
                      </ul>
                    ) : null}
                    {Object.keys(walletPortfolio.meta || {}).length > 0 ? (
                      <details className="text-xs">
                        <summary className="cursor-pointer text-ink-400">
                          meta
                        </summary>
                        <Json value={walletPortfolio.meta} />
                      </details>
                    ) : null}
                  </div>
                )}
              </Card>
            ) : null}

            <Card
              title={`Open positions (${summary?.open_position_count ?? 0})`}
            >
              {(summary?.open_positions || []).length === 0 ? (
                <Empty label="No open positions." />
              ) : (
                <Json value={summary?.open_positions} />
              )}
            </Card>

            <Card
              title={`Active protections (${summary?.protection_count ?? 0})`}
            >
              {(summary?.protections || []).length === 0 ? (
                <Empty label="No active protection rules." />
              ) : (
                <Json value={summary?.protections} />
              )}
            </Card>

            <Card
              title={`Active executors (${summary?.active_executors.length ?? 0})`}
            >
              {(summary?.active_executors || []).length === 0 ? (
                <Empty label="No active executors." />
              ) : (
                <ul className="embedded-list-scroll-sm space-y-1">
                  {summary!.active_executors.map((exec) => (
                    <li
                      key={exec.executor_id}
                      className="flex items-center gap-2 text-xs font-mono border border-brand-500/10 rounded px-2 py-1"
                    >
                      <Pill tone="brand">{exec.kind}</Pill>
                      <span>{exec.state}</span>
                      <span className="text-ink-500">{exec.market}</span>
                      <span className="text-ink-400">{exec.strategy_id}</span>
                      <span className="ml-auto text-ink-500">
                        {fmtTs(exec.created_at)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card
              title={`Recent orders (${orders.length})`}
              description="Latest order tracker rows for this account."
            >
              {orders.length === 0 ? (
                <Empty label="No recent orders." />
              ) : (
                <div className="embedded-table-scroll">
                  <table className="table w-full">
                    <thead>
                      <tr className="text-[11px] text-ink-400">
                        <th>State</th>
                        <th>Market</th>
                        <th>Side</th>
                        <th>Size</th>
                        <th>Filled</th>
                        <th>Avg</th>
                        <th>Strategy</th>
                        <th>Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      {orders.map((o) => (
                        <tr key={o.order_id} className="text-xs">
                          <td>{o.state}</td>
                          <td className="font-mono">{o.market}</td>
                          <td>{o.side}</td>
                          <td>{o.size_base}</td>
                          <td>{o.filled_size}</td>
                          <td>{o.avg_price ?? "—"}</td>
                          <td className="font-mono text-ink-400">
                            {o.strategy_id || "—"}
                          </td>
                          <td className="text-ink-400 font-mono">
                            {fmtTs(o.created_at)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            <Card
              title={`Reconciliation reports (${reports.length})`}
              description="Severity-tagged drift reports for this account."
            >
              {reports.length === 0 ? (
                <Empty label="No reports." />
              ) : (
                <div className="embedded-list-scroll-sm space-y-1.5">
                  {reports.map((r) => (
                    <div
                      key={r.report_id}
                      className="flex items-center gap-2 text-xs border border-brand-500/10 rounded px-2 py-1"
                    >
                      <Pill tone={severityTone(r.severity)}>{r.severity}</Pill>
                      <span className="font-mono text-ink-300">{r.scope}</span>
                      <span className="text-ink-400">
                        {Number(r.summary?.issue_count ?? 0)} issue(s)
                      </span>
                      <span className="ml-auto text-ink-500 font-mono">
                        {fmtTs(r.ts)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            <Card
              title="Raw profile"
              description="Full account row as stored in accounts/accounts.yml."
            >
              <Json value={profile} />
            </Card>
          </>
        )}
      </PageBody>
    </div>
  );
}
