"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { clientApi } from "../../lib/clientApi";
import type {
  EquityPoint,
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

type PnlSummary = {
  realized_usd?: number;
  equity_usd?: number;
  [key: string]: unknown;
};

function money(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function numberish(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return value == null ? "—" : String(value);
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
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [positions, setPositions] = useState<PortfolioPosition[]>([]);
  const [pnl, setPnl] = useState<PnlSummary | null>(null);
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
  const liveAccounts = accounts.filter((a) => a.live_trading_enabled).length;
  const totalTrades = accounts.reduce(
    (sum, a) => sum + Number(a.trade_count || 0),
    0,
  );

  const totals = health?.totals;
  const hasHealth = !!health && health.accounts.length > 0;

  return (
    <div>
      <PageHeader
        title="Portfolio"
        description="Account-aware command surface: NAV, exposure, snapshot health, reservations, executors, protections, and reconciliation."
        actions={
          <div className="flex items-center gap-2">
            {killSwitch ? (
              <Pill tone={killSwitch.kill_switch ? "danger" : "ok"}>
                {killSwitch.kill_switch
                  ? "kill-switch ON"
                  : killSwitch.live_trading_enabled
                    ? "live trading"
                    : "paper only"}
              </Pill>
            ) : null}
            <button
              onClick={() => runReconcile()}
              disabled={!!reconcileBusy}
              className="btn-ghost text-xs"
              title="Run reconciliation across all accounts"
            >
              {reconcileBusy === "*" ? "Reconciling…" : "Reconcile all"}
            </button>
            <button
              onClick={load}
              disabled={loading}
              className="btn-ghost text-xs"
            >
              {loading ? "Refreshing…" : "Refresh"}
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
            className={`rounded-lg border px-4 py-3 text-sm ${
              worstReport.severity === "trading_halted"
                ? "border-[#ef4560]/50 bg-[#ef4560]/10 text-[#ef4560]"
                : "border-[#f5a524]/40 bg-[#f5a524]/10 text-[#f5a524]"
            }`}
          >
            <div className="flex items-center gap-2">
              <Pill tone={severityTone(worstReport.severity)}>
                {worstReport.severity}
              </Pill>
              <span className="font-mono text-[11px]">
                {worstReport.scope}
                {worstReport.account_id ? `:${worstReport.account_id}` : ""}
              </span>
              <span className="ml-auto text-[11px] text-ink-400">
                {formatTsShort(worstReport.ts)}
              </span>
            </div>
            <div className="mt-1 text-xs">
              {Number(worstReport.summary?.issue_count ?? 0)} drift issue(s).
              Operator action required before continuing live trading.
            </div>
          </div>
        ) : null}

        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <Kpi
            label="Equity"
            value={money(summary?.totals?.equity_usd ?? pnl?.equity_usd)}
            tone="brand"
            spark={equityValues}
          />
          <Kpi label="Cash" value={money(summary?.totals?.cash_usd)} />
          <Kpi
            label="Realized PnL"
            value={money(pnl?.realized_usd)}
            tone={Number(pnl?.realized_usd || 0) >= 0 ? "ok" : "danger"}
          />
          <Kpi
            label="Active executors"
            value={`${totals?.active_executors ?? 0}`}
            delta={`${totals?.active_protections ?? 0} protection rule(s)`}
            tone={(totals?.active_executors ?? 0) > 0 ? "warn" : "neutral"}
          />
        </div>

        {hasHealth ? (
          <Card
            title="Account health"
            description={`Snapshots, reservations, open positions, protections, and active executors per account. Live: ${
              totals?.live_accounts ?? 0
            } / ${totals?.accounts ?? 0}.`}
          >
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {(health?.accounts || []).map((entry) => (
                <AccountHealthCard
                  key={entry.account_id}
                  entry={entry}
                  onReconcile={() => runReconcile(entry.account_id)}
                  busy={reconcileBusy === entry.account_id}
                />
              ))}
            </div>
          </Card>
        ) : (
          accounts.length === 0 &&
          !loading &&
          !error && (
            <Card
              title="No accounts configured"
              description="Strategies need an account before they can trade."
            >
              <div className="text-sm text-ink-300">
                Use{" "}
                <a
                  className="text-brand-300 hover:text-brand-200"
                  href="/settings"
                >
                  Settings
                </a>{" "}
                to configure wallet/provider readiness, then add account
                definitions through the runtime workspace config.
              </div>
            </Card>
          )
        )}

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <Card
            title="Accounts"
            description={`${totalTrades} recorded trade${totalTrades === 1 ? "" : "s"} across all accounts.`}
          >
            {accounts.length === 0 ? (
              <Empty
                label={loading ? "Loading accounts…" : "No accounts found."}
              />
            ) : (
              <div className="space-y-3">
                {accounts.map((account) => (
                  <div
                    key={account.id}
                    className="rounded-lg border border-brand-500/10 bg-ink-900/40 p-3"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm text-ink-100">
                        {account.id}
                      </span>
                      <Pill
                        tone={account.live_trading_enabled ? "warn" : "brand"}
                      >
                        {account.live_trading_enabled ? "live" : "paper"}
                      </Pill>
                      <span className="ml-auto text-xs text-ink-400">
                        {account.mode}
                      </span>
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                      <div>
                        <div className="text-ink-500">cash</div>
                        <div className="text-ink-100">
                          {money(account.cash_usd)}
                        </div>
                      </div>
                      <div>
                        <div className="text-ink-500">equity</div>
                        <div className="text-brand-200">
                          {money(account.equity_usd)}
                        </div>
                      </div>
                      <div>
                        <div className="text-ink-500">positions</div>
                        <div className="text-ink-100">
                          {Object.keys(account.positions || {}).length}
                        </div>
                      </div>
                      <div>
                        <div className="text-ink-500">trades</div>
                        <div className="text-ink-100">{account.trade_count}</div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card
            title="Equity curve"
            description="Cumulative realized PnL plus current account equity."
          >
            {curve.length === 0 ? (
              <Empty label="No equity points yet." />
            ) : (
              <div>
                <Sparkline
                  values={equityValues}
                  width={420}
                  height={120}
                  tone="brand"
                />
                <div className="embedded-list-scroll-sm mt-3 text-xs font-mono text-ink-300 space-y-1">
                  {curve
                    .slice(-12)
                    .reverse()
                    .map((point, index) => (
                      <div
                        key={`${point.ts}-${index}`}
                        className="flex justify-between gap-3"
                      >
                        <span>{formatTsShort(point.ts)}</span>
                        <span className="text-ink-100">
                          {money(point.equity_usd)}
                        </span>
                      </div>
                    ))}
                </div>
              </div>
            )}
          </Card>

          <Card
            title="Reconciliation"
            description="Severity tagged drift detection across local ledger vs exchange truth."
          >
            {reports.length === 0 ? (
              <Empty label="No reconciliation reports yet." />
            ) : (
              <div className="embedded-list-scroll space-y-2">
                {reports.slice(0, 8).map((report) => (
                  <div
                    key={report.report_id}
                    className="flex items-start justify-between gap-2 border border-brand-500/10 rounded-md px-2.5 py-2 bg-ink-900/30"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Pill tone={severityTone(report.severity)}>
                          {report.severity}
                        </Pill>
                        <span className="font-mono text-[11px] text-ink-300">
                          {report.scope}
                          {report.account_id ? `:${report.account_id}` : ""}
                        </span>
                      </div>
                      <div className="text-[11px] text-ink-400 mt-0.5">
                        {Number(report.summary?.issue_count ?? 0)} issue(s)
                      </div>
                    </div>
                    <span className="text-[10px] text-ink-500 font-mono shrink-0">
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
            title={`Wallet portfolio (${walletPortfolio.length})`}
            description="Aggregated balances across every account bound to an on-chain wallet provider. Stablecoins (USDT/USDC/...) sum into NAV at par; other assets show as raw on-chain amounts."
          >
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>Account</th>
                    <th>Wallet</th>
                    <th>Mode</th>
                    <th>Health</th>
                    <th>Stable NAV</th>
                    <th>Assets</th>
                    <th>Snapshot</th>
                  </tr>
                </thead>
                <tbody>
                  {walletPortfolio.map((row) => {
                    const ts = Number(row.ts);
                    const tsLabel = Number.isFinite(ts)
                      ? formatTsShort(new Date(ts * 1000).toISOString())
                      : "—";
                    const assets = Object.entries(row.free_by_asset || {})
                      .sort(([, a], [, b]) => Number(b) - Number(a))
                      .slice(0, 3)
                      .map(([k, v]) => `${k}=${numberish(v)}`)
                      .join(" · ") || "—";
                    return (
                      <tr key={row.account_id}>
                        <td className="font-mono text-xs">
                          <Link
                            href={`/accounts/${encodeURIComponent(row.account_id)}`}
                            className="hover:text-brand-300"
                          >
                            {row.account_id}
                          </Link>
                        </td>
                        <td className="font-mono text-xs text-ink-300">
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
                        <td className="font-mono text-xs">
                          {money(row.nav_usd)}
                        </td>
                        <td className="font-mono text-[11px] text-ink-300">
                          {assets}
                        </td>
                        <td className="text-ink-400 font-mono text-[11px]">
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
          title={`Open positions (${allPositions.length})`}
          description="Flattened account positions with human-readable fields first."
        >
          {allPositions.length === 0 ? (
            <Empty label="No open positions." />
          ) : (
            <div className="embedded-table-scroll">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>Account</th>
                    <th>Market</th>
                    <th>Side</th>
                    <th>Size</th>
                    <th>Avg Entry</th>
                    <th>Unrealized</th>
                    <th>Realized</th>
                  </tr>
                </thead>
                <tbody>
                  {allPositions.map((p, index) => (
                    <tr key={`${p.account_id}-${p.market}-${index}`}>
                      <td className="font-mono text-xs">{p.account_id}</td>
                      <td className="font-mono text-xs text-ink-100">
                        {p.market || "—"}
                      </td>
                      <td>
                        <Pill
                          tone={
                            String(p.side || "").toLowerCase() === "short"
                              ? "danger"
                              : "ok"
                          }
                        >
                          {p.side || "—"}
                        </Pill>
                      </td>
                      <td>{numberish(p.size)}</td>
                      <td>{numberish(p.avg_entry_price)}</td>
                      <td
                        className={
                          Number(p.unrealized_pnl_usd || 0) < 0
                            ? "text-[#ef4560]"
                            : "text-accent-300"
                        }
                      >
                        {money(p.unrealized_pnl_usd)}
                      </td>
                      <td>{money(p.realized_pnl_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {summary && (
          <Card
            title="Advanced payloads"
            description="For debugging parity with the runtime API."
          >
            <details>
              <summary className="cursor-pointer text-xs text-ink-400 hover:text-ink-200">
                portfolio summary
              </summary>
              <div className="mt-2">
                <Json value={summary} />
              </div>
            </details>
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-ink-400 hover:text-ink-200">
                pnl payload
              </summary>
              <div className="mt-2">
                <Json value={pnl} />
              </div>
            </details>
            {health ? (
              <details className="mt-2">
                <summary className="cursor-pointer text-xs text-ink-400 hover:text-ink-200">
                  control plane health payload
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
  onReconcile,
  busy,
}: {
  entry: ControlPlaneAccountHealth & { status?: string };
  onReconcile: () => void;
  busy: boolean;
}) {
  const snapshot = entry.snapshot;
  const reservedShare =
    snapshot && snapshot.total_usd
      ? Math.min(1, entry.reserved_usd / Math.max(1, Number(snapshot.total_usd)))
      : 0;
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-900/40 p-3.5 space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <Link
          href={`/accounts/${encodeURIComponent(entry.account_id)}`}
          className="font-mono text-sm text-brand-200 hover:text-brand-100"
        >
          {entry.account_id}
        </Link>
        <Pill tone={modePill(entry.mode)}>{entry.mode}</Pill>
        <span className="text-[11px] text-ink-400">
          {entry.venue}/{entry.kind}
        </span>
        <span className="ml-auto">
          {snapshot ? (
            <Pill tone={snapshotHealthTone(snapshot.health)}>
              snapshot {snapshot.health}
            </Pill>
          ) : (
            <Pill tone="warn">no snapshot</Pill>
          )}
        </span>
      </div>

      <div className="grid grid-cols-3 gap-2 text-xs">
        <div>
          <div className="text-ink-500">total</div>
          <div className="text-ink-100">{money(snapshot?.total_usd)}</div>
        </div>
        <div>
          <div className="text-ink-500">free</div>
          <div className="text-ink-100">
            {money(snapshot?.free_usd ?? snapshot?.available_usd)}
          </div>
        </div>
        <div>
          <div className="text-ink-500">positions</div>
          <div className="text-brand-200">
            {money(snapshot?.positions_value_usd)}
          </div>
        </div>
        <div>
          <div className="text-ink-500">reserved</div>
          <div className={entry.reserved_usd > 0 ? "text-[#f5a524]" : "text-ink-100"}>
            {money(entry.reserved_usd)}
          </div>
        </div>
        <div>
          <div className="text-ink-500">open positions</div>
          <div className="text-ink-100">{entry.open_position_count}</div>
        </div>
        <div>
          <div className="text-ink-500">protections</div>
          <div className="text-ink-100">{entry.protection_count}</div>
        </div>
      </div>

      {snapshot && Number(snapshot.total_usd) > 0 ? (
        <div>
          <div className="flex justify-between text-[10px] text-ink-500">
            <span>reservation utilization</span>
            <span>{Math.round(reservedShare * 100)}%</span>
          </div>
          <div className="h-1.5 mt-0.5 rounded-full bg-ink-700/60 overflow-hidden">
            <div
              className={`h-full ${
                reservedShare > 0.7
                  ? "bg-[#ef4560]"
                  : reservedShare > 0.4
                    ? "bg-[#f5a524]"
                    : "bg-accent-500"
              }`}
              style={{ width: `${Math.round(reservedShare * 100)}%` }}
            />
          </div>
        </div>
      ) : null}

      <div className="flex items-center gap-2 text-[11px] text-ink-400">
        <span>
          {entry.active_executors.length} active executor
          {entry.active_executors.length === 1 ? "" : "s"}
        </span>
        <span className="ml-auto flex gap-1.5 flex-wrap">
          {entry.live_trading_enabled ? (
            <Pill tone="warn">live ok</Pill>
          ) : (
            <Pill tone="brand">live disabled</Pill>
          )}
          <button
            onClick={onReconcile}
            disabled={busy}
            className="btn-ghost text-[11px] py-0.5"
          >
            {busy ? "Running…" : "Reconcile"}
          </button>
          <Link
            href={`/accounts/${encodeURIComponent(entry.account_id)}`}
            className="btn-ghost text-[11px] py-0.5"
          >
            Inspect
          </Link>
        </span>
      </div>

      {entry.active_executors.length > 0 ? (
        <div className="embedded-scroll max-h-32 space-y-1">
          {entry.active_executors.map((exec) => (
            <div
              key={exec.executor_id}
              className="flex items-center gap-2 text-[11px] font-mono text-ink-300 border border-brand-500/10 rounded px-2 py-1"
            >
              <span className="text-ink-100 truncate">{exec.executor_id}</span>
              <Pill tone="brand">{exec.kind}</Pill>
              <span>{exec.state}</span>
              <span className="ml-auto text-ink-500">{exec.market}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
