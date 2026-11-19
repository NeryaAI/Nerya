"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import type { PortfolioPosition } from "../../lib/api";
import { positionExposure } from "../../lib/positionExposure";
import { financeMoney, financeNumber } from "../../lib/financeDisplay";
import styles from "./PositionExposure.module.css";
import { instrumentLabel } from "./InstrumentIdentity";

export function PositionExposure({ positions, unavailable = false, onMarket }: {
  positions: PortfolioPosition[]; unavailable?: boolean; onMarket?: (market: string) => void;
}) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const tp = useTranslations("predictionPosition");
  const predictionOnly = positions.length > 0 && positions.every(p => p.instrument?.type === "prediction_outcome");
  const summary = useMemo(() => positionExposure(positions), [positions]);
  const [all, setAll] = useState(false);
  return <section className={styles.exposure} data-testid="position-exposure" aria-label={i18nCopy(zh, "copy.components_finance_PositionExposure.001")}>
    <h3>{predictionOnly ? tp("positionValue") : i18nCopy(zh, "copy.components_finance_PositionExposure.002")}</h3>
    {unavailable ? <p className={styles.note}>{i18nCopy(zh, "copy.components_finance_PositionExposure.003")}</p>
      : !summary.total ? <p className={styles.note}>{i18nCopy(zh, "copy.components_finance_PositionExposure.004")}</p>
      : !summary.known ? <p className={styles.note}>{i18nCopy(zh, "copy.components_finance_PositionExposure.005")}</p>
      : <>
        <p className={styles.label}>{predictionOnly ? tp("knownValue") : summary.missing ? (i18nCopy(zh, "copy.components_finance_PositionExposure.006")) : (i18nCopy(zh, "copy.components_finance_PositionExposure.007"))}</p>
        <p className={styles.total}>{financeMoney(summary.gross, locale)}</p>
        {!predictionOnly && <dl className={styles.directions}>{(["long", "short", "unknown"] as const).filter((side) => side !== "unknown" || summary.directions.unknown > 0).map((side) => <div key={side}><dt>{side === "long" ? (i18nCopy(zh, "copy.components_finance_PositionExposure.008")) : side === "short" ? (i18nCopy(zh, "copy.components_finance_PositionExposure.009")) : (i18nCopy(zh, "copy.components_finance_PositionExposure.010"))}</dt><dd>{financeMoney(summary.directions[side], locale)}</dd></div>)}</dl>}
        <div className={styles.assets}>{(all ? summary.markets : summary.markets.slice(0, 5)).map((item) => {
          const share = summary.gross > 0 ? item.value / summary.gross * 100 : 0;
          const instrument = positions.find(p => p.market === item.market)?.instrument;
          const label = instrument ? `${instrumentLabel(item.market, instrument)} · ${instrument.outcome || "—"}` : item.market || (i18nCopy(zh, "copy.components_finance_PositionExposure.011"));
          return <button key={item.market} type="button" className={styles.asset} disabled={!onMarket || !item.market} onClick={() => onMarket?.(item.market)} aria-label={i18nCopy(zh, "copy.components_finance_PositionExposure.012", { value0: label })}>
            <span className={styles.assetLine}><strong>{label}</strong><span>{financeNumber(share, locale, 1)}%</span></span>
            <span className={styles.track} role="meter" aria-label={i18nCopy(zh, "copy.components_finance_PositionExposure.013", { value0: label })} aria-valuemin={0} aria-valuemax={100} aria-valuenow={share}><span style={{ width: `${share}%` }} /></span>
            <span className={styles.value}>{financeMoney(item.value, locale)}</span>
          </button>;
        })}</div>
        {summary.markets.length > 5 ? <button className={styles.more} type="button" onClick={() => setAll((value) => !value)}>{all ? (i18nCopy(zh, "copy.components_finance_PositionExposure.014")) : (i18nCopy(zh, "copy.components_finance_PositionExposure.015", { value0: summary.markets.length }))}</button> : null}
        {summary.missing ? <p className={styles.warning} role="status">{predictionOnly ? tp("unpricedPositions", { missing: summary.missing, total: summary.total }) : i18nCopy(zh, "copy.components_finance_PositionExposure.016", { value0: summary.missing, value1: summary.total })}</p> : null}
        <p className={styles.note}>{predictionOnly ? tp("valuationNote") : i18nCopy(zh, "copy.components_finance_PositionExposure.017")}</p>
      </>}
  </section>;
}
