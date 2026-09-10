"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { clientApi } from "../../lib/clientApi";
import type {
  EquityPoint,
  PortfolioPnl,
  PortfolioPosition,
  PortfolioSummary,
} from "../../lib/api";
import type {
  ControlPlaneAccountHealth,
  ControlPlanePortfolioHealth,
  KillSwitchView,
  ReconciliationReport,
} from "../../lib/clientApi";
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
  StatusDot,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import { Sparkline } from "../../components/Sparkline";
import { ModePill } from "../../components/ModePill";
import { formatTsShort } from "../../lib/format";

/** Equity-curve time ranges → API `limit` (number of points). */
type CurveRange = "24H" | "7D" | "30D";
const CURVE_RANGE_LIMIT: Record<CurveRange, number> = {
  "24H": 24,
  "7D": 168,
  "30D": 720,
};
const CURVE_RANGE_KEY: Record<CurveRange, string> = {
  "24H": "range24H",
  "7D": "range7D",
  "30D": "range30D",
};

// Raw backend enums → i18n keys (fall back to the raw value when unknown).
const SEVERITY_LABEL_KEYS: Record<string, string> = {
  info: "sevInfo",
  warning: "sevWarning",
  action_required: "sevActionRequired",
  trading_halted: "sevTradingHalted",
};
const HEALTH_LABEL_KEYS: Record<string, string> = {
  ok: "healthOk",
  degraded: "healthDegraded",
  stale: "healthStale",
};
const MODE_LABEL_KEYS: Record<string, string> = {
  live: "live",
  paper: "paper",
  canary: "modeCanary",
  shadow: "modeShadow",
};

function money(value: unknown): string {
  let n = Number(value);
  if (!Number.isFinite(n)) return "-";
  // Avoid the confusing "$-0" rendering for tiny negative values.
  if (Math.abs(n) < 0.005) n = 0;
  const abs = Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
  return n < 0 ? `-$${abs}` : `$${abs}`;
}

/** Signed variant for PnL figures — positives carry an explicit "+", zeros
 * stay unsigned, matching the dashboard's fmtSigned semantics. */
function moneySigned(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return money(value);
  return `+${money(n)}`;
}

/** Colour class for PnL cells: green only for real gains, red for losses,
 * neutral for zero/missing — zero is not a profit. */
function pnlToneClass(value: unknown): string {
  const n = Number(value);
  if (value == null || !Number.isFinite(n) || n === 0) {
    return "text-[color:var(--text-muted)]";
  }
  return n > 0 ? "text-ok" : "text-danger";
}

/** Same semantics as {@link pnlToneClass} for the <Kpi> tone prop. */
function pnlToneKpi(value: unknown): "neutral" | "ok" | "danger" {
  const n = Number(value);
  if (value == null || !Number.isFinite(n) || n === 0) return "neutral";
  return n > 0 ? "ok" : "danger";
}

function numberish(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return value == null ? "-" : String(value);
  return n.toLocaleString(undefined, { maximumFractionDigits: 6 });
}

function flattenPositions(
  summary: PortfolioSummary | null,
  explicit: PortfolioPosition[],
): PortfolioPosition[] {
  if (explicit.length) return explicit;
  const out: PortfolioPosition[] = [];
  for (const account of summary?.accounts || []) {
    const positions = account.positions || {};
    for (const [market, position] of Object.entries(positions)) {
      out.push({
        ...position,
        account_id: position.account_id || account.id,
        market: position.market || market,
      });
    }
  }
  return out;
}

function snapshotHealthTone(
  health?: string,
): "ok" | "warn" | "danger" | "neutral" {
  if (!health) return "neutral";
  if (health === "ok") return "ok";
  if (health === "degraded") return "warn";
  return "danger";
}

function severityTone(
  severity?: string,
): "ok" | "warn" | "danger" | "neutral" | "brand" {
  switch (severity) {
    // Neutral, not green: green is reserved for "healthy / all-clear", and
    // an info-level report is not a positive signal.
    case "info":
      return "neutral";
    case "warning":
      return "warn";
    case "action_required":
      return "danger";
    case "trading_halted":
      return "danger";
    default:
      return "neutral";
  }
}

/**
 * Enum → localized label. Unknown values fall through to the raw string so a
 * new backend enum degrades visibly instead of rendering nothing.
 */
function enumLabel(
  keys: Record<string, string>,
  t: (k: string) => string,
  value?: string,
): string {
  if (!value) return "–";
  const key = keys[value];
  return key ? t(key) : value;
}

/** Shimmer placeholder for the first-load window. */
function Skel({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={`skeleton inline-block rounded-md ${className ?? "h-4 w-24 align-middle"}`}
    />
  );
}

export default function PortfolioPage() {
  const t = useTranslations("portfolio");
  const tCommon = useTranslations("common");
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [positions, setPositions] = useState<PortfolioPosition[]>([]);
  const [pnl, setPnl] = useState<PortfolioPnl | null>(null);
  const [curve, setCurve] = useState<EquityPoint[]>([]);
  const [health, setHealth] = useState<ControlPlanePortfolioHealth | null>(
    null,
  );
  const [reports, setReports] = useState<ReconciliationReport[]>([]);
  const [worstReport, setWorstReport] = useState<ReconciliationReport | null>(
    null,
  );
  const [killSwitch, setKillSwitch] = useState<KillSwitchView | null>(null);
  const [walletPortfolio, setWalletPortfolio] = useState<
    Array<{
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
    }>
  >([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // First-load gate: skeletons show only until the initial round lands, so
  // background 30s refreshes never flash placeholders over real values.
  const [loaded, setLoaded] = useState(false);
  const [reconcileBusy, setReconcileBusy] = useState<string | null>(null);
  // summary is the page's primary feed — its failure gets a visible banner
  // + retry instead of silently rendering $0.00.
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [curveRange, setCurveRange] = useState<CurveRange>("7D");
  const [curveLoading, setCurveLoading] = useState(true);
  const [curveError, setCurveError] = useState<string | null>(null);

  async function loadCurve(range: CurveRange) {
    setCurveLoading(true);
    setCurveError(null);
    try {
      const res = await clientApi.portfolioEquityCurve(CURVE_RANGE_LIMIT[range]);
      setCurve(res.points || []);
    } catch (e) {
      console.error("portfolio equity curve failed:", e);
      setCurveError(e instanceof Error ? e.message : String(e));
      setCurve([]);
    } finally {
      setCurveLoading(false);
    }
  }

  async function load() {
    setLoading(true);
    setError(null);
    const summaryP = clientApi
      .portfolioSummary()
      .then((res) => {
        setSummaryError(null);
        return res;
      })
      .catch((e: unknown) => {
        console.error("portfolio summary failed:", e);
        setSummaryError(e instanceof Error ? e.message : String(e));
        return null;
      });
    const [
      summaryRes,
      positionsRes,
      pnlRes,
      healthRes,
      reportsRes,
      killRes,
      walletPortfolioRes,
    ] = await Promise.all([
      summaryP,
      clientApi.portfolioPositions().catch((e: unknown) => {
        console.error("portfolio positions failed:", e);
        return { positions: [] };
      }),
      clientApi.portfolioPnl().catch((e: unknown) => {
        console.error("portfolio pnl failed:", e);
        return null;
      }),
      clientApi.portfolioHealth().catch((e: unknown) => {
        console.error("portfolio health failed:", e);
        return null;
      }),
      clientApi
        .controlReconciliationReports({ limit: 12 })
        .catch((e: unknown) => {
          console.error("reconciliation reports failed:", e);
          return { reports: [], worst_recent: null, filter: {} };
        }),
      clientApi.controlKillSwitchGet().catch((e: unknown) => {
        console.error("kill switch state failed:", e);
        return null;
      }),
      clientApi.walletPortfolio({}).catch((e: unknown) => {
        console.error("wallet portfolio failed:", e);
        return { ok: false, accounts: [] };
      }),
    ]);
    if (summaryRes) setSummary(summaryRes);
    setPositions(positionsRes.positions || []);
    setPnl(pnlRes);
    setHealth(healthRes);
    setReports(reportsRes.reports || []);
    setWorstReport(reportsRes.worst_recent ?? null);
    setKillSwitch(killRes);
    setWalletPortfolio(walletPortfolioRes.accounts || []);
    setLoaded(true);
    setLoading(false);
  }

  async function runReconcile(account_id?: string) {
    setReconcileBusy(account_id ?? "*");
    try {
      const res = await clientApi.controlReconciliationRun({
        account_id,
        operator: "dashboard",
      });
      if (res.report) {
        setReports((prev) => [res.report, ...prev].slice(0, 12));
        if (
          !worstReport ||
          severityRank(res.report.severity) > severityRank(worstReport.severity)
        ) {
          setWorstReport(res.report);
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setReconcileBusy(null);
    }
  }

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
  }, []);

  // Equity curve refetches on its own so switching the time range doesn't
  // re-run the whole 7-endpoint poll.
  useEffect(() => {
    void loadCurve(curveRange);
  }, [curveRange]);

  const accounts = summary?.accounts || [];
  const allPositions = useMemo(
    () => flattenPositions(summary, positions),
    [summary, positions],
  );
  const equityValues = curve
    .map((p) => Number(p.equity_usd))
    .filter(Number.isFinite);
  const tradeCountByAccount = useMemo(() => {
    const map: Record<string, number> = {};
    for (const account of accounts) {
      map[account.id] = Number(account.trade_count || 0);
    }
    return map;
  }, [accounts]);
  const modeByAccount = useMemo(() => {
    const map: Record<string, string> = {};
    for (const account of accounts) {
      map[account.id] = account.mode;
    }
    return map;
  }, [accounts]);
  // Paper and live equity are never meaningful summed together (paper is
  // simulated capital) — the KPI carries a per-mode split under the total.
  const equityByMode = useMemo(() => {
    const out = { paper: 0, live: 0 };
    for (const account of accounts) {
      const eq = Number(account.equity_usd || 0);
      if (account.mode === "live") out.live += eq;
      else out.paper += eq;
    }
    return out;
  }, [accounts]);
  const curveUp =
    equityValues.length > 1 &&
    equityValues[equityValues.length - 1] >= equityValues[0];

  const totals = health?.totals;
  const hasHealth = !!health && health.accounts.length > 0;

  return (
    <div>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <div className="flex items-center gap-2">
            {killSwitch ? (
              // Unified trading-mode semantics: live = danger red,
              // paper = brand violet (same as <ModePill> everywhere else).
              <Pill
                tone={
                  killSwitch.kill_switch
                    ? "danger"
                    : killSwitch.live_trading_enabled
                      ? "danger"
                      : "brand"
                }
              >
                {killSwitch.kill_switch
                  ? t("killSwitchOn")
                  : killSwitch.live_trading_enabled
                    ? t("liveTrading")
                    : t("paperOnly")}
              </Pill>
            ) : null}
            <button
              onClick={() => runReconcile()}
              disabled={!!reconcileBusy}
              className="btn-ghost text-xs"
              title={t("reconcileAllTitle")}
            >
              {reconcileBusy === "*" ? t("reconciling") : t("reconcileAll")}
            </button>
            <button
              onClick={load}
              disabled={loading}
              className="btn-ghost text-xs"
            >
              {loading ? tCommon("refreshing") : tCommon("refresh")}
            </button>
          </div>
        }
      />
      <SectionTabs section="trading" />
      <PageBody>
        {error && <ErrorBanner error={error} />}

        {/* summary is the primary feed — surface its failure loudly with a
            retry instead of letting the page read as "no positions, no
            wallets, $0 equity". */}
        {summaryError ? (
          <div className="rounded-lg border border-danger/40 bg-danger/10 px-4 py-3 text-[13px] text-danger flex flex-wrap items-center gap-3">
            <span className="min-w-0 flex-1 truncate" title={summaryError}>
              {t("loadFailed")} · {summaryError}
            </span>
            <button
              onClick={load}
              disabled={loading}
              className="btn-ghost text-xs border border-danger/40"
            >
              {t("retry")}
            </button>
          </div>
        ) : null}
        <ErrorBanner error={summaryError} />

        {worstReport &&
        (worstReport.severity === "action_required" ||
          worstReport.severity === "trading_halted") ? (
          <div
            className={`rounded-lg border px-4 py-3 text-[13px] ${
              worstReport.severity === "trading_halted"
                ? "border-danger/40 bg-danger/10 text-danger"
                : "border-warn/40 bg-warn/10 text-warn"
            }`}
          >
            <div className="flex items-center gap-2">
              <Pill tone={severityTone(worstReport.severity)}>
                {enumLabel(SEVERITY_LABEL_KEYS, t, worstReport.severity)}
              </Pill>
              <span className="font-mono text-[12px]">
                {worstReport.scope}
                {worstReport.account_id ? `:${worstReport.account_id}` : ""}
              </span>
              <span className="ml-auto text-[12px] text-[color:var(--text-muted)]">
                {formatTsShort(worstReport.ts)}
              </span>
            </div>
            <div className="mt-1 text-[13px]">
              {t("driftIssues", { count: Number(worstReport.summary?.issue_count ?? 0) })}
            </div>
          </div>
        ) : null}

        <section className="grid grid-cols-2 md:grid-cols-4 gap-x-8 gap-y-4">
          <Kpi
            inline
            label={t("equity")}
            value={
              loaded ? (
                money(summary?.totals?.equity_usd ?? pnl?.equity_usd)
              ) : (
                <Skel className="h-6 w-28" />
              )
            }
            tone="brand"
            delta={
              accounts.length > 0 ? (
                // Never present paper + live as one headline number without
                // context — split the total by mode under it.
                <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <ModePill mode="paper" />
                  <span className="font-mono">{money(equityByMode.paper)}</span>
                  <span className="text-[color:var(--text-muted)]">·</span>
                  <ModePill mode="live" />
                  <span className="font-mono">{money(equityByMode.live)}</span>
                </span>
              ) : undefined
            }
          />
          <Kpi
            inline
            label={t("cash")}
            value={loaded ? money(summary?.totals?.cash_usd) : <Skel className="h-6 w-24" />}
          />
          <Kpi
            inline
            label={t("realizedPnl")}
            value={loaded ? moneySigned(pnl?.realized_usd) : <Skel className="h-6 w-24" />}
            tone={pnlToneKpi(pnl?.realized_usd)}
          />
          <Kpi
            inline
            label={t("unrealizedPnl")}
            value={loaded ? moneySigned(pnl?.unrealized_usd) : <Skel className="h-6 w-24" />}
            tone={pnlToneKpi(pnl?.unrealized_usd)}
          />
        </section>

        {hasHealth ? (
          <Card
            title={t("accountHealth")}
            description={t("accountHealthDesc", {
              live: totals?.live_accounts ?? 0,
              total: totals?.accounts ?? 0,
            })}
          >
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {(health?.accounts || []).map((entry) => (
                <AccountHealthCard
                  key={entry.account_id}
                  entry={entry}
                  tradeCount={tradeCountByAccount[entry.account_id]}
                  onReconcile={() => runReconcile(entry.account_id)}
                  busy={reconcileBusy === entry.account_id}
                />
              ))}
            </div>
          </Card>
        ) : accounts.length > 0 ? (
          <Card
            title={t("accounts")}
            description={t("accountsDesc", {
              count: accounts.reduce((sum, a) => sum + Number(a.trade_count || 0), 0),
            })}
          >
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              {accounts.map((account) => (
                <div
                  key={account.id}
                  className="rounded-lg border border-[color:var(--line)] p-4"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[13px] text-[color:var(--text-base)]">
                      {account.id}
                    </span>
                    <Pill tone={account.live_trading_enabled ? "danger" : "brand"}>
                      {account.live_trading_enabled ? t("live") : t("paper")}
                    </Pill>
                    <span className="ml-auto text-[12px] text-[color:var(--text-muted)]">
                      {enumLabel(MODE_LABEL_KEYS, t, account.mode)}
                    </span>
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-2 text-[12px]">
                    <div>
                      <div className="text-[color:var(--text-muted)]">{t("cashLower")}</div>
                      <div className="text-[color:var(--text-base)]">
                        {money(account.cash_usd)}
                      </div>
                    </div>
                    <div>
                      <div className="text-[color:var(--text-muted)]">{t("equityLower")}</div>
                      <div className="text-brand-300">
                        {money(account.equity_usd)}
                      </div>
                    </div>
                    <div>
                      <div className="text-[color:var(--text-muted)]">{t("positionsLower")}</div>
                      <div className="text-[color:var(--text-base)]">
                        {Object.keys(account.positions || {}).length}
                      </div>
                    </div>
                    <div>
                      <div className="text-[color:var(--text-muted)]">{t("tradesLower")}</div>
                      <div className="text-[color:var(--text-base)]">{account.trade_count}</div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </Card>
        ) : !loading && !error && !summaryError ? (
          <Card
            title={t("noAccountsTitle")}
            description={t("noAccountsDesc")}
          >
            <div className="text-sm text-ink-300">
              {t("noAccountsUse")}{" "}
              <Link
                className="text-brand-300 hover:text-brand-200"
                href="/settings"
              >
                {t("settingsLink")}
              </Link>{" "}
              {t("noAccountsHint")}
            </div>
          </Card>
        ) : null}

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          <Card
            title={t("equityCurve")}
            description={t("equityCurveDesc")}
            actions={
              <div className="flex gap-1">
                {(["24H", "7D", "30D"] as CurveRange[]).map((range) => (
                  <button
                    key={range}
                    onClick={() => setCurveRange(range)}
                    className={`px-2 py-0.5 text-[12px] rounded-md font-medium ${
                      curveRange === range
                        ? "bg-brand-500/15 text-brand-200 border border-brand-500/30"
                        : "text-ink-400 hover:text-ink-100"
                    }`}
                  >
                    {t(CURVE_RANGE_KEY[range])}
                  </button>
                ))}
              </div>
            }
          >
            {curveLoading && curve.length === 0 ? (
              <div className="skeleton h-[120px] w-full" aria-hidden />
            ) : curve.length === 0 ? (
              <Empty
                label={curveError ? `${t("loadFailed")} · ${curveError}` : t("noEquityPoints")}
              />
            ) : (
              /* Color follows the range direction: up = ok mint, down =
                 danger red — same semantics as the PnL KPIs above. */
              <div>
                <Sparkline
                  values={equityValues}
                  width={420}
                  height={120}
                  tone={curveUp ? "accent" : "danger"}
                  fill
                />
              </div>
            )}
          </Card>

          <Card
            title={t("reconciliation")}
            description={t("reconciliationDesc")}
          >
            {!loaded ? (
              <div className="space-y-2.5" aria-hidden>
                {[0, 1, 2].map((i) => (
                  <div key={i} className="skeleton h-9 w-full" />
                ))}
              </div>
            ) : reports.length === 0 ? (
              <Empty label={t("noReconciliation")} />
            ) : (
              <div className="embedded-list-scroll divide-y divide-[color:var(--line)]">
                {reports.slice(0, 8).map((report) => (
                  <div
                    key={report.report_id}
                    className="flex items-start justify-between gap-2 py-2"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Pill tone={severityTone(report.severity)}>
                          {enumLabel(SEVERITY_LABEL_KEYS, t, report.severity)}
                        </Pill>
                        <span className="font-mono text-[12px] text-[color:var(--text-muted)]">
                          {report.scope}
                          {report.account_id ? `:${report.account_id}` : ""}
                        </span>
                      </div>
                      <div className="text-[12px] text-[color:var(--text-muted)] mt-0.5">
                        {t("issueCount", { count: Number(report.summary?.issue_count ?? 0) })}
                      </div>
                    </div>
                    <span className="text-[12px] text-[color:var(--text-muted)] font-mono shrink-0">
                      {formatTsShort(report.ts)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>

        {walletPortfolio.length > 0 ? (
          <Card
            title={t("walletPortfolio", { count: walletPortfolio.length })}
            description={t("walletPortfolioDesc")}
            padded={false}
          >
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr>
                    <th>{t("colAccount")}</th>
                    <th>{t("colWallet")}</th>
                    <th>{t("colMode")}</th>
                    <th>{t("colHealth")}</th>
                    <th className="text-right">{t("colStableNav")}</th>                    <th>{t("colAssets")}</th>
                    <th>{t("colSnapshot")}</th>
                  </tr>
                </thead>
                <tbody>
                  {walletPortfolio.map((row, index) => {
                    // Backend snapshots carry epoch seconds
                    // (account_snapshots uses time.time()); pass the raw
                    // number through — formatTs's parseDate heuristic
                    // already handles seconds-vs-milliseconds, so the
                    // manual ×1000 here produced double conversion.
                    const ts = Number(row.ts);
                    const tsLabel = Number.isFinite(ts)
                      ? formatTsShort(ts)
                      : "–";
                    const assets = Object.entries(row.free_by_asset || {})
                      .sort(([, a], [, b]) => Number(b) - Number(a))
                      .slice(0, 3)
                      .map(([k, v]) => `${numberish(v)} ${k}`)
                      .join(" · ") || "–";
                    return (
                      // account_id alone is not unique — one account can
                      // expose several wallets.
                      <tr key={`${row.account_id}:${row.wallet_id}:${index}`}>
                        <td className="font-mono text-[12px]">
                          <Link
                            href={`/accounts/${encodeURIComponent(row.account_id)}`}
                            className="hover:text-brand-300"
                          >
                            {row.account_id}
                          </Link>
                        </td>
                        <td className="font-mono text-[12px] text-[color:var(--text-muted)]">
                          {row.wallet_id}
                        </td>
                        <td>
                          {row.mode === "live" || row.mode === "paper" ? (
                            <ModePill mode={row.mode} />
                          ) : (
                            <Pill
                              tone={row.mode === "canary" ? "warn" : "neutral"}
                            >
                              {enumLabel(MODE_LABEL_KEYS, t, row.mode)}
                            </Pill>
                          )}
                        </td>
                        <td>
                          <Pill tone={snapshotHealthTone(row.health)}>
                            {enumLabel(HEALTH_LABEL_KEYS, t, row.health)}
                          </Pill>
                        </td>
                        <td className="text-right font-mono tabular-nums text-[12px]">
                          {money(row.nav_usd)}
                        </td>
                        <td className="font-mono text-[12px] text-[color:var(--text-muted)]">
                          {assets}
                        </td>
                        <td className="text-[color:var(--text-muted)] font-mono text-[12px]">
                          {tsLabel}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        ) : null}

        <Card
          title={t("openPositions", { count: allPositions.length })}
          description={t("openPositionsDesc")}
          padded={false}
        >
          {!loaded ? (
            <div className="px-5 py-4 space-y-2.5" aria-hidden>
              {[0, 1, 2].map((i) => (
                <div key={i} className="skeleton h-5 w-full" />
              ))}
            </div>
          ) : allPositions.length === 0 ? (
            <div className="px-5 py-4">
              <Empty label={t("noOpenPositions")} />
            </div>
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr>
                    <th>{t("colAccount")}</th>
                    <th>{t("colMode")}</th>
                    <th>{t("colMarket")}</th>
                    <th>{t("colSide")}</th>
                    <th className="text-right">{t("colSize")}</th>
                    <th className="text-right">{t("colAvgEntry")}</th>
                    <th className="text-right">{t("colUnrealized")}</th>
                    <th className="text-right">{t("colRealized")}</th>
                  </tr>
                </thead>
                <tbody>
                  {allPositions.map((p, index) => {
                    const isShort = String(p.side || "").toLowerCase() === "short";
                    const sideLabel = p.side
                      ? p.side[0].toUpperCase() + p.side.slice(1).toLowerCase()
                      : "–";
                    const posMode = p.account_id
                      ? modeByAccount[p.account_id]
                      : undefined;
                    return (
                      <tr key={`${p.account_id}-${p.market}-${index}`}>
                        <td className="font-mono text-[12px]">{p.account_id}</td>
                        <td>
                          {posMode ? (
                            <ModePill mode={posMode} />
                          ) : (
                            <span className="text-[12px] text-[color:var(--text-muted)]">–</span>
                          )}
                        </td>
                        <td className="font-mono text-[12px] text-[color:var(--text-base)]">
                          {p.market || "–"}
                        </td>
                        <td>
                          <Pill tone={isShort ? "danger" : "ok"}>{sideLabel}</Pill>
                        </td>
                        <td className="text-right font-mono tabular-nums">{numberish(p.size)}</td>
                        <td className="text-right font-mono tabular-nums">{numberish(p.avg_entry_price)}</td>
                        <td className={`text-right font-mono tabular-nums ${pnlToneClass(p.unrealized_pnl_usd)}`}>
                          {moneySigned(p.unrealized_pnl_usd)}
                        </td>
                        <td className={`text-right font-mono tabular-nums ${pnlToneClass(p.realized_pnl_usd)}`}>
                          {moneySigned(p.realized_pnl_usd)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {summary && (
          <Card
            title={t("advancedPayloads")}
            description={t("advancedPayloadsDesc")}
          >
            <Advanced title={t("portfolioSummary")} storageKey="portfolio-raw-summary">
              <Json value={summary} />
            </Advanced>
            <Advanced title={t("pnlPayload")} storageKey="portfolio-raw-pnl">
              <Json value={pnl} />
            </Advanced>
            {health ? (
              <Advanced title={t("controlPlaneHealth")} storageKey="portfolio-raw-health">
                <Json value={health} />
              </Advanced>
            ) : null}
          </Card>
        )}
      </PageBody>
    </div>
  );
}

function severityRank(severity?: string): number {
  switch (severity) {
    case "trading_halted":
      return 4;
    case "action_required":
      return 3;
    case "warning":
      return 2;
    case "info":
      return 1;
    default:
      return 0;
  }
}

function AccountHealthCard({
  entry,
  tradeCount,
  onReconcile,
  busy,
}: {
  entry: ControlPlaneAccountHealth & { status?: string };
  tradeCount?: number;
  onReconcile: () => void;
  busy: boolean;
}) {
  const t = useTranslations("portfolio");
  const snapshot = entry.snapshot;
  const reservedShare =
    snapshot && snapshot.total_usd
      ? Math.min(1, entry.reserved_usd / Math.max(1, Number(snapshot.total_usd)))
      : 0;
  return (
    <div className="group rounded-lg border border-[color:var(--line)] p-4 space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <Link
          href={`/accounts/${encodeURIComponent(entry.account_id)}`}
          className="font-mono text-[13px] text-brand-300 hover:text-brand-200"
        >
          {entry.account_id}
        </Link>
        {entry.mode === "live" || entry.mode === "paper" ? (
          <ModePill mode={entry.mode} />
        ) : (
          <Pill tone={entry.mode === "canary" ? "warn" : "neutral"}>
            {enumLabel(MODE_LABEL_KEYS, t, entry.mode)}
          </Pill>
        )}
        <span className="text-[12px] text-[color:var(--text-muted)]">
          {entry.venue}/{entry.kind}
        </span>
        <span className="ml-auto">
          {/* Healthy snapshots read as a quiet dot+label; only degraded /
              stale / missing states escalate to a coloured Pill so the
              six-card grid doesn't paint every card green. */}
          {snapshot ? (
            snapshot.health === "ok" ? (
              <StatusDot
                tone="ok"
                label={t("snapshotWithHealth", {
                  health: enumLabel(HEALTH_LABEL_KEYS, t, snapshot.health),
                })}
              />
            ) : (
              <Pill tone={snapshotHealthTone(snapshot.health)}>
                {t("snapshotWithHealth", {
                  health: enumLabel(HEALTH_LABEL_KEYS, t, snapshot.health),
                })}
              </Pill>
            )
          ) : (
            <Pill tone="warn">{t("noSnapshot")}</Pill>
          )}
        </span>
      </div>

      {/* Zero-value bookkeeping fields (reserved / protections) stay
          hidden so a fresh paper account reads as 4 numbers, not 6. */}
      <div className="grid grid-cols-2 gap-2 text-[12px] sm:grid-cols-4">
        <div>
          <div className="text-[color:var(--text-muted)]">{t("totalLower")}</div>
          <div className="text-[color:var(--text-base)]">{money(snapshot?.total_usd)}</div>
        </div>
        <div>
          <div className="text-[color:var(--text-muted)]">{t("freeLower")}</div>
          <div className="text-[color:var(--text-base)]">
            {money(snapshot?.free_usd ?? snapshot?.available_usd)}
          </div>
        </div>
        <div>
          <div className="text-[color:var(--text-muted)]">{t("positionsLower")}</div>
          <div className="text-brand-300">
            {money(snapshot?.positions_value_usd)}
          </div>
        </div>
        <div>
          <div className="text-[color:var(--text-muted)]">{t("openPositionsLower")}</div>
          <div className="text-[color:var(--text-base)]">{entry.open_position_count}</div>
        </div>
        {entry.reserved_usd > 0 ? (
          <div>
            <div className="text-[color:var(--text-muted)]">{t("reservedLower")}</div>
            <div className="text-warn">{money(entry.reserved_usd)}</div>
          </div>
        ) : null}
        {entry.protection_count > 0 ? (
          <div>
            <div className="text-[color:var(--text-muted)]">{t("protectionsLower")}</div>
            <div className="text-[color:var(--text-base)]">{entry.protection_count}</div>
          </div>
        ) : null}
      </div>

      {snapshot && Number(snapshot.total_usd) > 0 ? (
        <div>
          <div className="flex justify-between text-[11px] text-[color:var(--text-muted)]">
            <span>{t("reservationUtilization")}</span>
            <span>{Math.round(reservedShare * 100)}%</span>
          </div>
          <div className="h-1 mt-0.5 rounded-full bg-[color:var(--line)] overflow-hidden">
            <div
              className={`h-full ${
                reservedShare > 0.7
                  ? "bg-danger"
                  : reservedShare > 0.4
                    ? "bg-warn"
                    : "bg-ok"
              }`}
              style={{ width: `${Math.round(reservedShare * 100)}%` }}
            />
          </div>
        </div>
      ) : null}

      <div className="flex items-center gap-2 text-[12px] text-[color:var(--text-muted)]">
        <span>
          {t("activeExecutorCount", { count: entry.active_executors.length })}
          {typeof tradeCount === "number" ? (
            <span className="ml-2">· {t("tradesLower")}: {tradeCount}</span>
          ) : null}
        </span>
        <span className="ml-auto flex gap-1.5 flex-wrap items-center">
          {/* "live disabled" is the default for paper accounts — repeating
              it on every card was pure noise, so only the live state gets a
              pill (danger red, same as <ModePill> semantics). The reconcile
              action is always visible — hover-only controls are unreachable
              on touch devices. */}
          {entry.live_trading_enabled ? (
            <Pill tone="danger">{t("liveOk")}</Pill>
          ) : null}
          <button
            onClick={onReconcile}
            disabled={busy}
            className="btn-ghost text-[12px] py-0.5 opacity-60 transition-opacity hover:opacity-100 focus-visible:opacity-100"
          >
            {busy ? t("running") : t("reconcile")}
          </button>
        </span>
      </div>

      {entry.active_executors.length > 0 ? (
        <div className="embedded-scroll max-h-32 space-y-1">
          {entry.active_executors.map((exec) => (
            <div
              key={exec.executor_id}
              className="flex items-center gap-2 text-[12px] font-mono text-[color:var(--text-muted)] border border-[color:var(--line)] rounded px-2 py-1"
            >
              <span className="text-[color:var(--text-base)] truncate">{exec.executor_id}</span>
              <Pill tone="brand">{exec.kind}</Pill>
              <span>{exec.state}</span>
              <span className="ml-auto text-[color:var(--text-muted)]">{exec.market}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
