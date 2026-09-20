"use client";

import { useMemo, useState } from "react";
import { useLocale } from "next-intl";
import type { PortfolioPosition } from "../../lib/api";
import { positionExposure } from "../../lib/positionExposure";
import { financeMoney, financeNumber } from "../../lib/financeDisplay";
import styles from "./PositionExposure.module.css";

export function PositionExposure({ positions, unavailable = false, onMarket }: {
  positions: PortfolioPosition[]; unavailable?: boolean; onMarket?: (market: string) => void;
}) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const summary = useMemo(() => positionExposure(positions), [positions]);
  const [all, setAll] = useState(false);
  return <section className={styles.exposure} data-testid="position-exposure" aria-label={zh ? "仓位结构" : "Position exposure"}>
    <h3>{zh ? "仓位结构" : "Position exposure"}</h3>
    {unavailable ? <p className={styles.note}>{zh ? "仓位数据暂不可用，无法判断敞口。" : "Positions are unavailable. Exposure cannot be determined."}</p>
      : !summary.total ? <p className={styles.note}>{zh ? "没有已记录的持仓。" : "No positions recorded."}</p>
      : !summary.known ? <p className={styles.note}>{zh ? "仓位未提供名义价值。明细仍可查看，不推算资产占比。" : "No notional values supplied. Positions remain available; no allocation is inferred."}</p>
      : <>
        <p className={styles.label}>{summary.missing ? (zh ? "已知名义价值" : "Known gross notional") : (zh ? "总名义价值" : "Gross notional")}</p>
        <p className={styles.total}>{financeMoney(summary.gross, locale)}</p>
        <dl className={styles.directions}>{(["long", "short", "unknown"] as const).filter((side) => side !== "unknown" || summary.directions.unknown > 0).map((side) => <div key={side}><dt>{side === "long" ? (zh ? "多头" : "Long") : side === "short" ? (zh ? "空头" : "Short") : (zh ? "方向未确认" : "Unknown side")}</dt><dd>{financeMoney(summary.directions[side], locale)}</dd></div>)}</dl>
        <div className={styles.assets}>{(all ? summary.markets : summary.markets.slice(0, 5)).map((item) => {
          const share = summary.gross > 0 ? item.value / summary.gross * 100 : 0;
          const label = item.market || (zh ? "市场未提供" : "Unknown market");
          return <button key={item.market} type="button" className={styles.asset} disabled={!onMarket || !item.market} onClick={() => onMarket?.(item.market)} aria-label={zh ? `筛选 ${label} 仓位` : `Filter ${label} positions`}>
            <span className={styles.assetLine}><strong>{label}</strong><span>{financeNumber(share, locale, 1)}%</span></span>
            <span className={styles.track} role="meter" aria-label={zh ? `${label} 占已知名义价值比例` : `${label} share of known notional`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={share}><span style={{ width: `${share}%` }} /></span>
            <span className={styles.value}>{financeMoney(item.value, locale)}</span>
          </button>;
        })}</div>
        {summary.markets.length > 5 ? <button className={styles.more} type="button" onClick={() => setAll((value) => !value)}>{all ? (zh ? "收起资产" : "Show fewer") : (zh ? `查看全部 ${summary.markets.length} 个市场` : `Show all ${summary.markets.length} markets`)}</button> : null}
        {summary.missing ? <p className={styles.warning} role="status">{zh ? `${summary.missing} / ${summary.total} 个仓位缺少名义价值，以上比例仅覆盖已知部分。` : `${summary.missing} of ${summary.total} positions have no notional value. Shares cover known values only.`}</p> : null}
        <p className={styles.note}>{zh ? "按报告的名义价值绝对值统计，不是净资产占比。点击市场筛选仓位。" : "Absolute reported notional, not a share of account equity. Select a market to filter positions."}</p>
      </>}
  </section>;
}
