"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
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
  const t = useTranslations("accountDetail");
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
      setError(t("headerRequired"));
      return;
    }
    await patchHeaders({ [key]: value });
    setNewHeaderKey("");
    setNewHeaderValue("");
  }

  async function removeHeader(key: string) {
    if (!confirm(t("removeHeaderConfirm", { key }))) return;
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
        t("reasonPrompt", { id: accountId, status: next }),
        t("manualOperator"),
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
      t("resetPaperPrompt", { id: accountId }),
      String(currentBalance || ""),
    );
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
    setBusy("reset_paper");
    try {
      let res = await clientApi.accountsResetPaper({
        account_id: accountId,
        initial_balance_usd: initial,
        operator: "dashboard",
      });
      if (!res.ok && res.error === "account_busy" && res.state) {
        const proceed = window.confirm(
          t("accountBusyConfirm", {
            id: accountId,
            orders: res.state.active_orders,
            positions: res.state.open_positions,
            executors: res.state.active_executors,
          }),
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
          ? t("forceDeleteConfirm", { id: accountId })
          : t("deleteConfirm", { id: accountId }),
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
            t("cannotDelete", {
              positions: res.state.open_positions,
              executors: res.state.active_executors,
              orders: res.state.active_orders,
            }),
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
        <PageHeader title={t("accountPrefix", { id: accountId })} description={t("loading")} />
        <SectionTabs section="trading" />
        <PageBody>
          <Empty label={t("loadingAccount")} />
        </PageBody>
      </div>
    );
  }

  const profile = summary?.profile;
  const snapshot = summary?.snapshot;

  return (
    <div>
      <PageHeader
        title={t("accountPrefix", { id: accountId })}
        description={
          profile
            ? t("profileOn", { mode: profile.mode.toUpperCase(), venue: profile.venue, kind: profile.kind, wallet: profile.wallet_id || "—" })
            : t("accountDetail")
        }
        actions={
          <div className="flex items-center gap-2">
            <Link href="/portfolio" className="btn-ghost text-xs">
              {t("portfolio")}
            </Link>
            <button
              onClick={runReconcile}
              disabled={busy === "reconcile"}
              className="btn-ghost text-xs"
            >
              {busy === "reconcile" ? "…" : t("reconcile")}
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
        {!profile ? (
          <Empty label={t("notFound")} />
        ) : (
          <>
            <div className="flex items-center gap-3 flex-wrap">
              <Pill tone={modePill(profile.mode)}>{profile.mode}</Pill>
              <Pill tone={statusPill(profile.status)}>{profile.status}</Pill>
              {profile.live_trading_enabled ? (
                <Pill tone="warn">{t("liveTradingEnabled")}</Pill>
              ) : (
                <Pill tone="brand">{t("liveTradingOff")}</Pill>
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
                      {t("setReadOnly")}
                    </button>
                    <button
                      onClick={() => setStatus("quarantined")}
                      disabled={busy?.startsWith("status:")}
                      className="btn-ghost text-xs text-[#ef4560]"
                    >
                      {t("quarantine")}
                    </button>
                    <button
                      onClick={() => setStatus("disabled")}
                      disabled={busy?.startsWith("status:")}
                      className="btn-ghost text-xs"
                    >
                      {t("disable")}
                    </button>
                  </>
                ) : (
                  <button
                    onClick={() => setStatus("active")}
                    disabled={busy?.startsWith("status:")}
                    className="btn-ghost text-xs text-accent-300"
                  >
                    {t("reactivate")}
                  </button>
                )}
                {profile.mode === "paper" ? (
                  <button
                    onClick={() => void resetPaper()}
                    disabled={busy === "reset_paper"}
                    className="btn-ghost text-xs text-brand-200"
                    title={t("resetPaperTitle")}
                  >
                    {busy === "reset_paper" ? t("resetting") : t("resetPaper")}
                  </button>
                ) : null}
                <button
                  onClick={() => deleteAccount(false)}
                  disabled={busy === "delete"}
                  className="btn-ghost text-xs text-[#ef4560]"
                >
                  {t("delete")}
                </button>
              </span>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Kpi
                label={t("totalLabel", { currency: profile.base_currency || "USDT" })}
                value={money(snapshot?.total_usd, profile.base_currency)}
                tone="brand"
              />
              <Kpi
                label={t("free")}
                value={money(
                  snapshot?.free_usd ?? snapshot?.available_usd,
                  profile.base_currency,
                )}
              />
              <Kpi
                label={t("reserved")}
                value={money(summary?.reserved_usd, profile.base_currency)}
                tone={(summary?.reserved_usd ?? 0) > 0 ? "warn" : "neutral"}
              />
              <Kpi
                label={t("snapshot")}
                value={snapshot?.health || "—"}
                tone={
                  snapshot?.health === "ok"
                    ? "ok"
                    : snapshot?.health
                      ? "warn"
                      : "neutral"
                }
                delta={snapshot ? fmtTs(snapshot.ts) : t("noSnapshot")}
              />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <Card
                title={t("permissionsLimits")}
                description={t("permissionsLimitsDesc")}
              >
                <div className="grid grid-cols-2 gap-3 text-xs">
                  <div>
                    <div className="text-ink-500 mb-1">{t("permissions")}</div>
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
                            {v ? t("yes") : t("no")}
                          </Pill>
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <div className="text-ink-500 mb-1">{t("limits")}</div>
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
                title={t("credentials")}
                description={t("credentialsDesc")}
              >
                {Object.keys(profile.credentials).length === 0 ? (
                  <Empty label={t("noCredentials")} />
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
                  {t("walletBinding")}{" "}
                  <span className="font-mono text-ink-200">
                    {profile.wallet_id || "(none)"}
                  </span>
                </div>
              </Card>
            </div>

            <Card
              title={t("httpAuthHeaders")}
              description={t("httpAuthHeadersDesc")}
            >
              {headers.length === 0 ? (
                <Empty label={t("noHeaders")} />
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
                        {t("remove")}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-3 flex flex-col gap-2 md:flex-row md:items-center">
                <input
                  className="input text-xs font-mono md:w-48"
                  placeholder={t("headerKeyPlaceholder")}
                  value={newHeaderKey}
                  onChange={(e) => setNewHeaderKey(e.target.value)}
                  disabled={busy === "headers"}
                />
                <input
                  className="input text-xs font-mono flex-1"
                  placeholder={t("headerValuePlaceholder")}
                  value={newHeaderValue}
                  onChange={(e) => setNewHeaderValue(e.target.value)}
                  disabled={busy === "headers"}
                />
                <button
                  onClick={() => void addHeader()}
                  disabled={busy === "headers"}
                  className="btn-ghost text-xs text-brand-200"
                >
                  {busy === "headers" ? "…" : t("addUpdate")}
                </button>
              </div>
            </Card>

            {profile.wallet_id ? (
              <Card
                title={t("walletPortfolio")}
                description={t("walletPortfolioDesc")}
              >
                {walletPortfolio == null ? (
                  <Empty label={t("noWalletSnapshot")} />
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
                        label={t("navStable")}
                        value={money(
                          walletPortfolio.nav_usd,
                          profile.base_currency,
                        )}
                        tone="brand"
                      />
                      <Kpi
                        label={t("distinctAssets")}
                        value={String(
                          Object.keys(walletPortfolio.free_by_asset || {})
                            .length,
                        )}
                      />
                      <Kpi
                        label={t("mode")}
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
              title={t("openPositionsTitle", { count: summary?.open_position_count ?? 0 })}
            >
              {(summary?.open_positions || []).length === 0 ? (
                <Empty label={t("noOpenPositions")} />
              ) : (
                <Json value={summary?.open_positions} />
              )}
            </Card>

            <Card
              title={t("activeProtections", { count: summary?.protection_count ?? 0 })}
            >
              {(summary?.protections || []).length === 0 ? (
                <Empty label={t("noProtections")} />
              ) : (
                <Json value={summary?.protections} />
              )}
            </Card>

            <Card
              title={t("activeExecutors", { count: summary?.active_executors.length ?? 0 })}
            >
              {(summary?.active_executors || []).length === 0 ? (
                <Empty label={t("noActiveExecutors")} />
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
              title={t("recentOrders", { count: orders.length })}
              description={t("recentOrdersDesc")}
            >
              {orders.length === 0 ? (
                <Empty label={t("noRecentOrders")} />
              ) : (
                <div className="embedded-table-scroll">
                  <table className="table w-full">
                    <thead>
                      <tr className="text-[11px] text-ink-400">
                        <th>{t("colState")}</th>
                        <th>{t("colMarket")}</th>
                        <th>{t("colSide")}</th>
                        <th>{t("colSize")}</th>
                        <th>{t("colFilled")}</th>
                        <th>{t("colAvg")}</th>
                        <th>{t("colStrategy")}</th>
                        <th>{t("colCreated")}</th>
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
              title={t("reconciliationReports", { count: reports.length })}
              description={t("reconciliationDesc")}
            >
              {reports.length === 0 ? (
                <Empty label={t("noReports")} />
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
                        {t("issues", { count: Number(r.summary?.issue_count ?? 0) })}
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
              title={t("rawProfile")}
              description={t("rawProfileDesc")}
            >
              <Json value={profile} />
            </Card>
          </>
        )}
      </PageBody>
    </div>
  );
}
