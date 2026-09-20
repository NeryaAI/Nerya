"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useEffect, useMemo, useState } from "react";
import { useLocale } from "next-intl";
import type { PortfolioPosition, PortfolioSummary } from "../../lib/api";
import type { ChartBlockShape, TimeValue } from "../../lib/chartBlock";
import { clientApi } from "../../lib/clientApi";
import { chartTime, finiteNumber, financeMoney, financeTone, sumKnown } from "../../lib/financeDisplay";
import { ModePill } from "../ModePill";
import { FinancialChart } from "./FinancialChart";
import { PositionsView } from "./PositionsView";
import { PositionExposure } from "./PositionExposure";
import styles from "./FinanceSurface.module.css";

const windows = { "24H": 86400, "7D": 604800, "30D": 2592000 };
type Window = keyof typeof windows;
export function PortfolioDesk({ accounts, positions, loaded, positionsError, summaryError }: {
  accounts: PortfolioSummary["accounts"]; positions: PortfolioPosition[]; loaded: boolean;
  positionsError?: string | null; summaryError?: string | null;
}) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const [selected, setSelected] = useState("");
  const [filterRequest, setFilterRequest] = useState<{ account: string; market: string; count: number }>();
  const account = accounts.find((a) => a.id === selected) || accounts[0];
  const [range, setRange] = useState<Window>("7D"), [refresh, setRefresh] = useState(0);
  const requestKey = `${account?.id}:${range}:${refresh}`;
  const [curve, setCurve] = useState<{ key: string; points: TimeValue[]; error: string; done: boolean }>({ key: "", points: [], error: "", done: false });
  useEffect(() => {
    if (!account?.id) return;
    let cancelled = false;
    const end = Math.floor(Date.now() / 1000), since = end - windows[range];
    setCurve({ key: requestKey, points: [], error: "", done: false });
    clientApi.accountsEquityCurve({ account_id: account.id, since_ts: since, limit: 1500, bucket_seconds: range === "24H" ? 300 : range === "7D" ? 1800 : 7200 }).then((result) => {
      if (cancelled) return;
      if (!result.ok || (result.account_id && result.account_id !== account.id) || !Array.isArray(result.points)) throw new Error(result.error || (zh ? "净值曲线暂不可用" : "Equity history unavailable"));
      const points = new Map<number, TimeValue>();
      for (const point of result.points) {
        const time = chartTime(point.ts), value = finiteNumber(point.nav_usd);
        if (time !== null && value !== null && time >= since && time <= end) points.set(time, { time, value });
      }
      setCurve({ key: requestKey, points: [...points.values()].sort((a, b) => Number(a.time) - Number(b.time)), error: "", done: true });
    }).catch((error: unknown) => { if (!cancelled) setCurve({ key: requestKey, points: [], error: error instanceof Error ? error.message : String(error), done: true }); });
    return () => { cancelled = true; };
  }, [account?.id, range, refresh]);
  const rows = positions.filter((p) => p.account_id === account?.id);
  const modes = Object.fromEntries(accounts.map((a) => [a.id, a.mode]));
  const unrealized = positionsError ? null : sumKnown(rows.map((p) => p.unrealized_pnl_usd));
  const current = curve.key === requestKey ? curve : { points: [], done: false, error: "" };
  const chart = useMemo<ChartBlockShape>(() => ({ kind: "chart", chart_id: `equity:${requestKey}`, chart_kind: "area", path: "inline",
    title: zh ? "账户净值 · USD" : "Account equity · USD", subtitle: `${account?.id || ""} · ${range}`,
    source: { skill: "accounts", action: "equity_curve", as_of: current.points.length ? new Date(Number(current.points[current.points.length - 1].time) * 1000).toISOString() : "" },
    series: [{ type: "area", name: zh ? "账户净值" : "Account equity", data: current.points }],
    caption: zh ? "只展示该账户实际记录的快照。净值变化可能包含出入金，不等于策略收益率。" : "Recorded snapshots for this account only. Equity changes may include deposits or withdrawals and are not strategy returns.",
  }), [requestKey, current.points, account?.id, zh, range]);
  if (!account) return <div className={styles.surface} data-testid="portfolio-desk"><p className={styles.empty} role="status">{!loaded ? (zh ? "正在加载账户…" : "Loading accounts…") : summaryError ? (zh ? "账户数据暂不可用，不能据此判断资产为零。" : "Account data unavailable. This does not mean zero assets.") : (zh ? "连接账户后，在这里查看净值和仓位。" : "Connect an account to review equity and positions here.")}</p></div>;
  return <div className={styles.surface} data-testid="portfolio-desk">
    <div className={styles.header}>
      <div className="flex min-w-0 flex-wrap items-center gap-3"><h2>{zh ? "资产与持仓" : "Assets and positions"}</h2>{account.mode === "paper" || account.mode === "live" ? <ModePill mode={account.mode} /> : <span className={styles.note}>{account.mode || (zh ? "模式未提供" : "Mode not provided")}</span>}</div>
      <ChoiceSelect aria-label={zh ? "查看账户" : "Account scope"} value={account.id} onValueChange={setSelected}>{accounts.map((a) => <option key={a.id} value={a.id}>{a.id} · {a.mode === "paper" ? (zh ? "模拟" : "Paper") : a.mode === "live" ? (zh ? "实盘" : "Live") : a.mode}</option>)}</ChoiceSelect>
    </div>
    <p className={styles.note}>{zh ? "以下数据仅属于所选账户，实盘与模拟资金不合并。" : "Figures belong to the selected account. Live and simulated funds are never combined."}</p>
    {summaryError ? <p role="status" className="pt-2 text-xs text-warn">{zh ? "账户刷新失败，显示上次记录。" : "Account refresh failed. Showing previous records."}</p> : null}
    <dl className={styles.metrics} data-testid="account-metrics">{[
      [zh ? "账户净值" : "Account equity", account.equity_usd, false],
      [zh ? "现金余额" : "Cash balance", account.cash_usd, false],
      [zh ? "当前持仓浮盈亏" : "Open-position P&L", unrealized, true],
      [zh ? "账户累计费用" : "Account fees paid", account.fees_paid_usd, false],
    ].map(([label, value, signed]) => <div key={String(label)}><dt>{label}</dt><dd className={signed ? financeTone(value) : undefined}>{financeMoney(value, locale, Boolean(signed))}</dd></div>)}</dl>
    <div className={styles.portfolioBody}>
    <div className={styles.curve}>
      <div className={styles.header} style={{ padding: "0 0 4px" }}><div role="group" aria-label={zh ? "净值时间范围" : "Equity time range"} className="flex gap-1">{(Object.keys(windows) as Window[]).map((value) => <button type="button" key={value} aria-pressed={value === range} onClick={() => setRange(value)}>{value}</button>)}</div><button type="button" onClick={() => setRefresh((n) => n + 1)} disabled={!current.done}>{zh ? "刷新曲线" : "Refresh curve"}</button></div>
      {!current.done ? <div className="flex h-64 items-center justify-center text-sm text-[color:var(--text-muted)]" role="status">{zh ? "正在加载所选账户的净值…" : "Loading this account’s equity…"}</div> : current.error ? <p role="alert" className="py-12 text-sm text-warn">{current.error}</p> : !current.points.length ? <p className={styles.empty}>{zh ? "所选时间范围没有净值快照。" : "No equity snapshots in this time window."}</p> : <FinancialChart key={requestKey} block={chart} height={260} />}
    </div>
    <PositionExposure key={account.id} positions={rows} unavailable={Boolean(positionsError)} onMarket={(market) => setFilterRequest((old) => ({ account: account.id, market, count: (old?.count || 0) + 1 }))} />
    </div>
    <PositionsView key={account.id} positions={rows} modes={modes} error={positionsError} filterRequest={filterRequest?.account === account.id ? filterRequest : undefined} />
  </div>;
}
