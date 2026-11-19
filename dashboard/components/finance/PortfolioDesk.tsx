"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { ChoiceSelect } from "../ChoiceSelect";

import { useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
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
  const tp = useTranslations("predictionPosition");
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
      if (!result.ok || (result.account_id && result.account_id !== account.id) || !Array.isArray(result.points)) throw new Error(result.error || (i18nCopy(zh, "copy.components_finance_PortfolioDesk.001")));
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
    title: i18nCopy(zh, "copy.components_finance_PortfolioDesk.002"), subtitle: `${account?.id || ""} · ${range}`,
    source: { skill: "accounts", action: "equity_curve", as_of: current.points.length ? new Date(Number(current.points[current.points.length - 1].time) * 1000).toISOString() : "" },
    series: [{ type: "area", name: i18nCopy(zh, "copy.components_finance_PortfolioDesk.003"), data: current.points }],
    caption: i18nCopy(zh, "copy.components_finance_PortfolioDesk.004"),
  }), [requestKey, current.points, account?.id, zh, range]);
  if (!account) return <div className={styles.surface} data-testid="portfolio-desk"><p className={styles.empty} role="status">{!loaded ? (i18nCopy(zh, "copy.components_finance_PortfolioDesk.005")) : summaryError ? (i18nCopy(zh, "copy.components_finance_PortfolioDesk.006")) : (i18nCopy(zh, "copy.components_finance_PortfolioDesk.007"))}</p></div>;
  return <div className={styles.surface} data-testid="portfolio-desk">
    <div className={styles.header}>
      <div className="flex min-w-0 flex-wrap items-center gap-3"><h2>{i18nCopy(zh, "copy.components_finance_PortfolioDesk.008")}</h2>{account.mode === "paper" || account.mode === "live" ? <ModePill mode={account.mode} /> : <span className={styles.note}>{account.mode || (i18nCopy(zh, "copy.components_finance_PortfolioDesk.009"))}</span>}</div>
      <ChoiceSelect aria-label={i18nCopy(zh, "copy.components_finance_PortfolioDesk.010")} value={account.id} onValueChange={setSelected}>{accounts.map((a) => <option key={a.id} value={a.id}>{a.id} · {a.mode === "paper" ? (i18nCopy(zh, "copy.components_finance_PortfolioDesk.011")) : a.mode === "live" ? (i18nCopy(zh, "copy.components_finance_PortfolioDesk.012")) : a.mode}</option>)}</ChoiceSelect>
    </div>
    <p className={styles.note}>{account.kind === "prediction_market" ? tp("strategyScope") : i18nCopy(zh, "copy.components_finance_PortfolioDesk.013")}</p>
    {summaryError ? <p role="status" className="pt-2 text-xs text-warn">{i18nCopy(zh, "copy.components_finance_PortfolioDesk.014")}</p> : null}
    <dl className={styles.metrics} data-testid="account-metrics">{[
      [i18nCopy(zh, "copy.components_finance_PortfolioDesk.015"), account.equity_usd, false],
      [i18nCopy(zh, "copy.components_finance_PortfolioDesk.016"), account.cash_usd, false],
      [i18nCopy(zh, "copy.components_finance_PortfolioDesk.017"), unrealized, true],
      [i18nCopy(zh, "copy.components_finance_PortfolioDesk.018"), account.fees_paid_usd, false],
    ].map(([label, value, signed]) => <div key={String(label)}><dt>{label}</dt><dd className={signed ? financeTone(value) : undefined}>{financeMoney(value, locale, Boolean(signed))}</dd></div>)}</dl>
    {account.kind === "prediction_market" ? <details className="mb-4 text-xs" data-testid="prediction-cash-details"><summary className="cursor-pointer">{tp("cashDetails")}</summary>
      <dl className="mt-2 flex flex-wrap gap-6"><div><dt>{tp("available")}</dt><dd>{financeMoney(account.available_usd, locale)}</dd></div><div><dt>{tp("locked")}</dt><dd>{financeMoney(account.locked_usd, locale)}</dd></div></dl>
      {account.fees_status === "unknown" ? <p className="mt-2 text-[color:var(--text-muted)]">{tp("unknownFees")}</p> : null}
    </details> : null}
    <div className={styles.portfolioBody}>
    <div className={styles.curve}>
      <div className={styles.header} style={{ padding: "0 0 4px" }}><div role="group" aria-label={i18nCopy(zh, "copy.components_finance_PortfolioDesk.019")} className="flex gap-1">{(Object.keys(windows) as Window[]).map((value) => <button type="button" key={value} aria-pressed={value === range} onClick={() => setRange(value)}>{value}</button>)}</div><button type="button" onClick={() => setRefresh((n) => n + 1)} disabled={!current.done}>{i18nCopy(zh, "copy.components_finance_PortfolioDesk.020")}</button></div>
      {!current.done ? <div className="flex h-64 items-center justify-center text-sm text-[color:var(--text-muted)]" role="status">{i18nCopy(zh, "copy.components_finance_PortfolioDesk.021")}</div> : current.error ? <p role="alert" className="py-12 text-sm text-warn">{current.error}</p> : !current.points.length ? <p className={styles.empty}>{i18nCopy(zh, "copy.components_finance_PortfolioDesk.022")}</p> : <FinancialChart key={requestKey} block={chart} height={260} />}
    </div>
    <PositionExposure key={account.id} positions={rows} unavailable={Boolean(positionsError)} onMarket={(market) => setFilterRequest((old) => ({ account: account.id, market, count: (old?.count || 0) + 1 }))} />
    </div>
    <PositionsView key={account.id} positions={rows} modes={modes} error={positionsError} filterRequest={filterRequest?.account === account.id ? filterRequest : undefined} />
  </div>;
}
