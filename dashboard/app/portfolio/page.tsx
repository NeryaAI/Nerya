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
  Card,
  Empty,
  ErrorBanner,
  Json,
  Kpi,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import { Sparkline } from "../../components/Sparkline";
import { formatTsShort } from "../../lib/format";

function money(value: unknown): string {
  let n = Number(value);
  if (!Number.isFinite(n)) return "-";
  // Avoid the confusing "$-0" rendering for tiny negative values.
  if (Math.abs(n) < 0.005) n = 0;
  const abs = Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
  return n < 0 ? `-$${abs}` : `$${abs}`;
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
    case "info":
      return "ok";
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

function modePill(mode: string): "ok" | "warn" | "danger" | "brand" | "neutral" {
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
  const [reconcileBusy, setReconcileBusy] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [
        summaryRes,
        positionsRes,
        pnlRes,
        curveRes,
        healthRes,
        reportsRes,
        killRes,
        walletPortfolioRes,
      ] = await Promise.all([
        clientApi.portfolioSummary(),
        clientApi.portfolioPositions().catch(() => ({ positions: [] })),
        clientApi.portfolioPnl().catch(() => null),
        clientApi
          .portfolioEquityCurve(160)
          .catch(() => ({ points: [], equity_usd: 0 })),
        clientApi.portfolioHealth().catch(() => null),
        clientApi
          .controlReconciliationReports({ limit: 12 })
          .catch(() => ({ reports: [], worst_recent: null, filter: {} })),
        clientApi.controlKillSwitchGet().catch(() => null),
        clientApi
          .walletPortfolio({})
          .catch(() => ({ ok: false, accounts: [] })),
      ]);
      setSummary(summaryRes);
      setPositions(positionsRes.positions || []);
      setPnl(pnlRes);
      setCurve(curveRes.points || []);
      setHealth(healthRes);
      setReports(reportsRes.reports || []);
      setWorstReport(reportsRes.worst_recent ?? null);
      setKillSwitch(killRes);
      setWalletPortfolio(walletPortfolioRes.accounts || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
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
              <Pill tone={killSwitch.kill_switch ? "danger" : "ok"}>
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

        {worstReport &&
        (worstReport.severity === "action_required" ||
          worstReport.severity === "trading_halted") ? (
          <div
            className={`rounded-lg border px-4 py-3 text-[13px] ${
              worstReport.severity === "trading_halted"
                ? "border-rose-500/40 bg-rose-500/10 text-rose-300"
                : "border-amber-500/40 bg-amber-500/10 text-amber-300"
            }`}
          >
            <div className="flex items-center gap-2">
              <Pill tone={severityTone(worstReport.severity)}>
                {worstReport.severity.replace("_", " ")}
              </Pill>
              <span className="font-mono text-[12px]">
                {worstReport.scope}
                {worstReport.account_id ? `:${worstReport.account_id}` : ""}
              </span>
              <span className="ml-auto text-[12px] text-[color:var(--text-muted)]">
                {formatTsShort(worstReport.ts)}
              </span>
            </div>
            <div className="mt-1 text-[12.5px]">
              {t("driftIssues", { count: Number(worstReport.summary?.issue_count ?? 0) })}
            </div>
          </div>
        ) : null}

        <section className="grid grid-cols-2 md:grid-cols-4 gap-x-8 gap-y-4">
          <Kpi
            inline
            label={t("equity")}
            value={money(summary?.totals?.equity_usd ?? pnl?.equity_usd)}
            tone="brand"
          />
          <Kpi
            inline
            label={t("cash")}
            value={money(summary?.totals?.cash_usd)}
          />
          <Kpi
            inline
            label={t("realizedPnl")}
            value={money(pnl?.realized_usd)}
            tone={Number(pnl?.realized_usd || 0) >= 0 ? "ok" : "danger"}
          />
          <Kpi
            inline
            label={t("unrealizedPnl")}
            value={money(pnl?.unrealized_usd)}
            tone={Number(pnl?.unrealized_usd || 0) >= 0 ? "ok" : "danger"}
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
                  className="rounded-lg border border-[color:var(--line)] p-3"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[13px] text-[color:var(--text-base)]">
                      {account.id}
                    </span>
                    <Pill tone={account.live_trading_enabled ? "warn" : "brand"}>
                      {account.live_trading_enabled ? t("live") : t("paper")}
                    </Pill>
                    <span className="ml-auto text-[12px] text-[color:var(--text-muted)]">
                      {account.mode}
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
        ) : !loading && !error ? (
          <Card
            title={t("noAccountsTitle")}
            description={t("noAccountsDesc")}
          >
            <div className="text-sm text-ink-300">
              {t("noAccountsUse")}{" "}
              <a
                className="text-brand-300 hover:text-brand-200"
                href="/settings"
              >
                {t("settingsLink")}
              </a>{" "}
              {t("noAccountsHint")}
            </div>
          </Card>
        ) : null}

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          <Card
            title={t("equityCurve")}
            description={t("equityCurveDesc")}
          >
            {curve.length === 0 ? (
              <Empty label={t("noEquityPoints")} />
            ) : (
              <div>
                <Sparkline
                  values={equityValues}
                  width={420}
                  height={120}
                  tone="brand"
                  fill
                />
                <div className="embedded-list-scroll-sm mt-3 text-[12px] font-mono text-[color:var(--text-muted)] space-y-1">
                  {curve
                    .slice(-12)
                    .reverse()
                    .map((point, index) => (
                      <div
                        key={`${point.ts}-${index}`}
                        className="flex justify-between gap-3"
                      >
                        <span>{formatTsShort(point.ts)}</span>
                        <span className="text-[color:var(--text-base)]">
                          {money(point.equity_usd)}
                        </span>
                      </div>
                    ))}
                </div>
              </div>
            )}
          </Card>

          <Card
            title={t("reconciliation")}
            description={t("reconciliationDesc")}
          >
            {reports.length === 0 ? (
              <Empty label={t("noReconciliation")} />
            ) : (
              <div className="embedded-list-scroll space-y-2">
                {reports.slice(0, 8).map((report) => (
                  <div
                    key={report.report_id}
                    className="flex items-start justify-between gap-2 border border-[color:var(--line)] rounded-md px-2.5 py-2"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Pill tone={severityTone(report.severity)}>
                          {report.severity.replace("_", " ")}
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
                    <span className="text-[11px] text-[color:var(--text-muted)] font-mono shrink-0">
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
                    <th>{t("colStableNav")}</th>
                    <th>{t("colAssets")}</th>
                    <th>{t("colSnapshot")}</th>
                  </tr>
                </thead>
                <tbody>
                  {walletPortfolio.map((row) => {
                    const ts = Number(row.ts);
                    const tsLabel = Number.isFinite(ts)
                      ? formatTsShort(new Date(ts * 1000).toISOString())
                      : "–";
                    const assets = Object.entries(row.free_by_asset || {})
                      .sort(([, a], [, b]) => Number(b) - Number(a))
                      .slice(0, 3)
                      .map(([k, v]) => `${k}=${numberish(v)}`)
                      .join(" · ") || "–";
                    return (
                      <tr key={row.account_id}>
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
                          <Pill
                            tone={row.mode === "live" ? "danger" : "brand"}
                          >
                            {row.mode}
                          </Pill>
                        </td>
                        <td>
                          <Pill tone={snapshotHealthTone(row.health)}>
                            {row.health}
                          </Pill>
                        </td>
                        <td className="font-mono text-[12px]">
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
          {allPositions.length === 0 ? (
            <div className="px-5 py-4">
              <Empty label={t("noOpenPositions")} />
            </div>
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr>
                    <th>{t("colAccount")}</th>
                    <th>{t("colMarket")}</th>
                    <th>{t("colSide")}</th>
                    <th>{t("colSize")}</th>
                    <th>{t("colAvgEntry")}</th>
                    <th>{t("colUnrealized")}</th>
                    <th>{t("colRealized")}</th>
                  </tr>
                </thead>
                <tbody>
                  {allPositions.map((p, index) => {
                    const isShort = String(p.side || "").toLowerCase() === "short";
                    const sideLabel = p.side
                      ? p.side[0].toUpperCase() + p.side.slice(1).toLowerCase()
                      : "–";
                    return (
                      <tr key={`${p.account_id}-${p.market}-${index}`}>
                        <td className="font-mono text-[12px]">{p.account_id}</td>
                        <td className="font-mono text-[12px] text-[color:var(--text-base)]">
                          {p.market || "–"}
                        </td>
                        <td>
                          <Pill tone={isShort ? "danger" : "ok"}>{sideLabel}</Pill>
                        </td>
                        <td>{numberish(p.size)}</td>
                        <td>{numberish(p.avg_entry_price)}</td>
                        <td
                          className={
                            Number(p.unrealized_pnl_usd || 0) < 0
                              ? "text-rose-500"
                              : "text-emerald-500"
                          }
                        >
                          {money(p.unrealized_pnl_usd)}
                        </td>
                        <td>{money(p.realized_pnl_usd)}</td>
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
            <details>
              <summary className="cursor-pointer text-[12px] text-[color:var(--text-muted)] hover:text-[color:var(--text-base)]">
                {t("portfolioSummary")}
              </summary>
              <div className="mt-2">
                <Json value={summary} />
              </div>
            </details>
            <details className="mt-2">
              <summary className="cursor-pointer text-[12px] text-[color:var(--text-muted)] hover:text-[color:var(--text-base)]">
                {t("pnlPayload")}
              </summary>
              <div className="mt-2">
                <Json value={pnl} />
              </div>
            </details>
            {health ? (
              <details className="mt-2">
                <summary className="cursor-pointer text-[12px] text-[color:var(--text-muted)] hover:text-[color:var(--text-base)]">
                  {t("controlPlaneHealth")}
                </summary>
                <div className="mt-2">
                  <Json value={health} />
                </div>
              </details>
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
    <div className="group rounded-lg border border-[color:var(--line)] p-3.5 space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <Link
          href={`/accounts/${encodeURIComponent(entry.account_id)}`}
          className="font-mono text-[13px] text-brand-300 hover:text-brand-200"
        >
          {entry.account_id}
        </Link>
        <Pill tone={modePill(entry.mode)}>{entry.mode}</Pill>
        <span className="text-[12px] text-[color:var(--text-muted)]">
          {entry.venue}/{entry.kind}
        </span>
        <span className="ml-auto">
          {snapshot ? (
            <Pill tone={snapshotHealthTone(snapshot.health)}>
              {t("snapshotWithHealth", { health: snapshot.health })}
            </Pill>
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
            <div className="text-amber-500">{money(entry.reserved_usd)}</div>
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
                  ? "bg-rose-500"
                  : reservedShare > 0.4
                    ? "bg-amber-500"
                    : "bg-emerald-500"
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
              pill. Reconcile reveals on hover; "Inspect" was removed because
              the account id link above opens the same page. */}
          {entry.live_trading_enabled ? (
            <Pill tone="warn">{t("liveOk")}</Pill>
          ) : null}
          <button
            onClick={onReconcile}
            disabled={busy}
            className="btn-ghost text-[12px] py-0.5 opacity-0 transition-opacity focus-visible:opacity-100 group-hover:opacity-100"
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
