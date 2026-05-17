"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  Advanced,
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
import { AccountEquityCurveCard } from "../../../components/accounts/AccountEquityCurveCard";
import { clientApi } from "../../../lib/clientApi";
import { alert as alertDialog, confirm as confirmDialog, prompt as promptDialog } from "../../../lib/dialogs";
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

function asStr(v: unknown): string {
  if (v == null) return "";
  return String(v);
}

function asNum(v: unknown): number | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function fmtNum(v: unknown, digits = 6): string {
  const n = asNum(v);
  if (n === null) return "—";
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function positionSideTone(side: unknown): "ok" | "danger" | "neutral" {
  const s = asStr(side).toLowerCase();
  if (s === "long" || s === "buy") return "ok";
  if (s === "short" || s === "sell") return "danger";
  return "neutral";
}

function pnlTone(value: number | null): string {
  if (value === null) return "text-ink-500";
  if (value > 0) return "text-emerald-300";
  if (value < 0) return "text-rose-300";
  return "text-ink-400";
}

function protectionStatusTone(status: unknown): "ok" | "warn" | "danger" | "neutral" {
  const s = asStr(status).toLowerCase();
  if (s === "armed" || s === "active") return "ok";
  if (s === "triggered" || s === "executing") return "warn";
  if (s === "disarmed" || s === "failed" || s === "canceled") return "danger";
  return "neutral";
}

type PositionShareRow = {
  strategy_id?: string;
  size_base?: number;
  avg_entry_price?: number;
  realized_pnl_usd?: number;
  fees_usd?: number;
  funding_usd?: number;
  unrealized_pnl_usd?: number;
  notional_usd?: number;
};

function PositionRow({ pos }: { pos: Record<string, unknown> }) {
  const t = useTranslations("accountDetail.positions");
  const [expanded, setExpanded] = useState(false);
  const market = asStr(pos.market) || "—";
  const side = asStr(pos.side) || "—";
  const size = asNum(pos.size_base);
  const avg = asNum(pos.avg_entry_price);
  const mark = asNum(pos.mark_price);
  const unreal = asNum(pos.unrealized_pnl_usd);
  const strategy = asStr(pos.strategy_id);
  const rawShares = Array.isArray(pos.shares)
    ? (pos.shares as PositionShareRow[])
    : [];
  // The merged sentinel ``__merged__`` is an internal value; if we see
  // it we hide the strategy footer line and rely on the shares list
  // for per-strategy attribution. Same with positions that have
  // multiple shares — even if the merged row labels itself with one
  // strategy, the breakdown is the source of truth.
  const isMerged = strategy === "__merged__" || rawShares.length > 1;
  const hasShares = rawShares.length > 0;
  return (
    <li className="border border-brand-500/10 rounded px-2 py-1.5 text-xs">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {hasShares ? (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-ink-400 hover:text-ink-100 transition-colors"
            aria-label={expanded ? t("collapse") : t("expand")}
            aria-expanded={expanded}
          >
            <span className="font-mono text-[10px]">
              {expanded ? "▾" : "▸"}
            </span>
          </button>
        ) : (
          <span className="w-3" />
        )}
        <span className="font-mono text-ink-100 min-w-[110px]">{market}</span>
        <Pill tone={positionSideTone(side)}>{side}</Pill>
        <span className="font-mono text-ink-300">
          {size !== null ? fmtNum(Math.abs(size)) : "—"}
          {avg !== null ? (
            <span className="text-ink-500"> @ {fmtNum(avg, 4)}</span>
          ) : null}
        </span>
        {mark !== null ? (
          <span className="font-mono text-ink-500 text-[11px]">
            mark {fmtNum(mark, 4)}
          </span>
        ) : null}
        {isMerged ? (
          <Pill tone="brand">
            {t("mergedBadge", { count: rawShares.length })}
          </Pill>
        ) : null}
        <span className={`font-mono ${pnlTone(unreal)} ml-auto`}>
          {unreal !== null
            ? (unreal >= 0 ? "+" : "") + fmtNum(unreal, 2)
            : "—"}
        </span>
        {strategy && !isMerged ? (
          <span className="font-mono text-ink-500 text-[11px] w-full truncate">
            {strategy}
          </span>
        ) : null}
      </div>
      {expanded && hasShares ? (
        <ul className="mt-1.5 ml-4 space-y-0.5 border-l border-brand-500/15 pl-2">
          {rawShares.map((share, idx) => {
            const shareSize = asNum(share.size_base);
            const shareAvg = asNum(share.avg_entry_price);
            const shareUnreal = asNum(share.unrealized_pnl_usd);
            const shareRealized = asNum(share.realized_pnl_usd);
            const shareFees = asNum(share.fees_usd);
            return (
              <li
                key={share.strategy_id || idx}
                className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px]"
              >
                <span className="font-mono text-ink-200 min-w-[110px] truncate">
                  {share.strategy_id || "—"}
                </span>
                <span className="font-mono text-ink-300">
                  {shareSize !== null ? fmtNum(Math.abs(shareSize)) : "—"}
                  {shareAvg !== null ? (
                    <span className="text-ink-500">
                      {" "}
                      @ {fmtNum(shareAvg, 4)}
                    </span>
                  ) : null}
                </span>
                {shareRealized !== null ? (
                  <span className={`font-mono text-[10px] ${pnlTone(shareRealized)}`}>
                    {t("realized")}: {shareRealized >= 0 ? "+" : ""}
                    {fmtNum(shareRealized, 2)}
                  </span>
                ) : null}
                {shareFees !== null && shareFees > 0 ? (
                  <span className="font-mono text-[10px] text-ink-500">
                    {t("fees")}: {fmtNum(shareFees, 2)}
                  </span>
                ) : null}
                <span className={`font-mono ml-auto ${pnlTone(shareUnreal)}`}>
                  {shareUnreal !== null
                    ? (shareUnreal >= 0 ? "+" : "") + fmtNum(shareUnreal, 2)
                    : "—"}
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}
    </li>
  );
}

function ProtectionRow({ rule }: { rule: Record<string, unknown> }) {
  const market = asStr(rule.market) || "—";
  const side = asStr(rule.side) || "—";
  const status = asStr(rule.status) || "—";
  const mode = asStr(rule.mode);
  const sl = rule.stop_loss as Record<string, unknown> | undefined;
  const tp = rule.take_profit as Record<string, unknown> | undefined;
  const slPrice = sl ? asNum(sl.trigger_price) ?? asNum(sl.price) : null;
  const tpPrice = tp ? asNum(tp.trigger_price) ?? asNum(tp.price) : null;
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs border border-brand-500/10 rounded px-2 py-1.5">
      <span className="font-mono text-ink-100 min-w-[110px]">{market}</span>
      <Pill tone={positionSideTone(side)}>{side}</Pill>
      <Pill tone={protectionStatusTone(status)}>{status}</Pill>
      {mode ? <span className="text-[11px] text-ink-500 font-mono">{mode}</span> : null}
      {slPrice !== null ? (
        <span className="font-mono text-rose-300 text-[11px]">SL {fmtNum(slPrice, 4)}</span>
      ) : null}
      {tpPrice !== null ? (
        <span className="font-mono text-emerald-300 text-[11px]">TP {fmtNum(tpPrice, 4)}</span>
      ) : null}
    </li>
  );
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
  const [vaultedToast, setVaultedToast] = useState<string | null>(null);
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

  async function patchHeaders(
    patch: Record<string, string | null>,
    opts?: { autoVault?: boolean },
  ) {
    setBusy("headers");
    setError(null);
    setVaultedToast(null);
    try {
      const res = await clientApi.accountsHeadersPatch({
        account_id: accountId,
        headers: patch,
        operator: "dashboard",
        auto_vault: opts?.autoVault ?? false,
      });
      if (!res.ok) {
        throw new Error(res.detail || res.error || "headers_patch_failed");
      }
      setHeaders(res.headers || []);
      const vaulted = res.vaulted_refs || {};
      const entries = Object.entries(vaulted);
      if (entries.length > 0) {
        setVaultedToast(entries.map(([k, ref]) => `${k} → ${ref}`).join(", "));
      }
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
    await patchHeaders({ [key]: value }, { autoVault: true });
    setNewHeaderKey("");
    setNewHeaderValue("");
  }

  async function removeHeader(key: string) {
    const ok = await confirmDialog({
      message: t("removeHeaderConfirm", { key }),
      tone: "danger",
    });
    if (!ok) return;
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
      const reason = await promptDialog({
        message: t("reasonPrompt", { id: accountId, status: next }),
        defaultValue: t("manualOperator"),
      });
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
    const raw = await promptDialog({
      message: t("resetPaperPrompt", { id: accountId }),
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
    setBusy("reset_paper");
    try {
      let res = await clientApi.accountsResetPaper({
        account_id: accountId,
        initial_balance_usd: initial,
        operator: "dashboard",
      });
      if (!res.ok && res.error === "account_busy" && res.state) {
        const proceed = await confirmDialog({
          message: t("accountBusyConfirm", {
            id: accountId,
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
    const ok = await confirmDialog({
      message: force
        ? t("forceDeleteConfirm", { id: accountId })
        : t("deleteConfirm", { id: accountId }),
      tone: "danger",
    });
    if (!ok) return;
    setBusy("delete");
    try {
      const res = await clientApi.accountsDelete({
        account_id: accountId,
        force,
        operator: "dashboard",
      });
      if (!res.ok) {
        if (res.state) {
          await alertDialog({
            message: t("cannotDelete", {
              positions: res.state.open_positions,
              executors: res.state.active_executors,
              orders: res.state.active_orders,
            }),
          });
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

            <AccountEquityCurveCard
              accountId={accountId}
              currency={profile.base_currency || "USDT"}
            />

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
              <p className="mt-2 text-[11px] text-ink-500">
                {t("headerAutoVaultHint")}
              </p>
              {vaultedToast ? (
                <div className="mt-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-200 font-mono">
                  {t("headerVaultedToast")} {vaultedToast}
                </div>
              ) : null}
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

            <Card title={t("liveStateTitle")} description={t("liveStateDesc")}>
              <div className="space-y-4">
                <section>
                  <div className="mb-2 flex items-center gap-2 text-[12px] text-ink-400">
                    <span className="font-medium text-ink-200">{t("openPositionsTitle", { count: summary?.open_position_count ?? 0 })}</span>
                  </div>
                  {(summary?.open_positions || []).length === 0 ? (
                    <Empty label={t("noOpenPositions")} />
                  ) : (
                    <ul className="embedded-list-scroll-sm space-y-1">
                      {(summary?.open_positions || []).map((pos, idx) => (
                        <PositionRow key={asStr(pos.position_id) || idx} pos={pos} />
                      ))}
                    </ul>
                  )}
                </section>

                <section>
                  <div className="mb-2 flex items-center gap-2 text-[12px] text-ink-400">
                    <span className="font-medium text-ink-200">{t("activeProtections", { count: summary?.protection_count ?? 0 })}</span>
                  </div>
                  {(summary?.protections || []).length === 0 ? (
                    <Empty label={t("noProtections")} />
                  ) : (
                    <ul className="embedded-list-scroll-sm space-y-1">
                      {(summary?.protections || []).map((p, idx) => (
                        <ProtectionRow key={asStr(p.protection_id) || idx} rule={p} />
                      ))}
                    </ul>
                  )}
                </section>

                <section>
                  <div className="mb-2 flex items-center gap-2 text-[12px] text-ink-400">
                    <span className="font-medium text-ink-200">{t("activeExecutors", { count: summary?.active_executors.length ?? 0 })}</span>
                  </div>
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
                </section>
              </div>
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

            <Advanced
              title={t("reconciliationReports", { count: reports.length })}
              description={t("reconciliationDesc")}
              count={reports.length || undefined}
              storageKey={`nerya.account.advanced.recon.${profile?.id ?? "unknown"}`}
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
            </Advanced>

            <Advanced
              title={t("rawProfile")}
              storageKey={`nerya.account.advanced.rawProfile.${profile?.id ?? "unknown"}`}
            >
              <Json value={profile} />
            </Advanced>
          </>
        )}
      </PageBody>
    </div>
  );
}
