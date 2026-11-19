"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { ChoiceSelect } from "../ChoiceSelect";

import { useEffect, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import type { PortfolioPosition } from "../../lib/api";
import { finiteNumber, financeMoney, financeNumber, financeTone, positionSide } from "../../lib/financeDisplay";
import { ChevronRightIcon } from "../icons";
import { ModePill } from "../ModePill";
import { FinanceReview } from "./FinanceReview";
import { InstrumentIdentity, instrumentLabel } from "./InstrumentIdentity";
import styles from "./FinanceSurface.module.css";

export function PositionsView({ positions, modes = {}, loading = false, error, title, filterRequest }: {
  positions: PortfolioPosition[]; modes?: Record<string, string>; loading?: boolean; error?: string | null; title?: string;
  filterRequest?: { market: string; count: number };
}) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const tp = useTranslations("predictionPosition");
  const predictionOnly = positions.length > 0 && positions.every(p => p.instrument?.type === "prediction_outcome");
  const [query, setQuery] = useState("");
  const [side, setSide] = useState("all");
  const [sort, setSort] = useState("market");
  const searchRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!filterRequest) return;
    setQuery(filterRequest.market); setSide("all");
    searchRef.current?.focus({ preventScroll: true });
    searchRef.current?.scrollIntoView({ block: "nearest" });
  }, [filterRequest?.count, filterRequest?.market]);
  const positionKeys = useMemo(() => new Map(positions.map((p, i) => [p, `${p.account_id}:${p.position_id || `${p.market}:${p.side}:${i}`}`])), [positions]);
  const rows = useMemo(() => positions.filter((p) => `${p.market || ""} ${p.account_id || ""} ${p.instrument?.label || ""} ${p.instrument?.outcome || ""}`.toLowerCase().includes(query.trim().toLowerCase()) && (predictionOnly || side === "all" || positionSide(p) === side)).sort((a, b) => {
    if (sort === "market") return instrumentLabel(a.market, a.instrument).localeCompare(instrumentLabel(b.market, b.instrument));
    const x = finiteNumber(a.unrealized_pnl_usd), y = finiteNumber(b.unrealized_pnl_usd);
    if (x === null) return y === null ? 0 : 1;
    if (y === null) return -1;
    return sort === "profit" ? y - x : x - y;
  }), [positions, query, side, sort, predictionOnly]);
  const dir = (p: PortfolioPosition) => positionSide(p) === "long" ? (i18nCopy(zh, "copy.components_finance_PositionsView.001")) : positionSide(p) === "short" ? (i18nCopy(zh, "copy.components_finance_PositionsView.002")) : (i18nCopy(zh, "copy.components_finance_PositionsView.003"));
  return <section className={styles.surface} data-testid="positions-view" aria-label={title || (i18nCopy(zh, "copy.components_finance_PositionsView.004"))}>
    <div className={styles.header}><h2>{title || (i18nCopy(zh, "copy.components_finance_PositionsView.005"))} <span className="ml-1 text-xs text-[color:var(--text-muted)]">{positions.length}</span></h2><span className={styles.note}>{i18nCopy(zh, "copy.components_finance_PositionsView.006")}</span></div>
    {error ? <p role="status" className="pb-2 text-xs text-warn">{error}</p> : null}
    {positions.length ? <div className={styles.filters}>
      <input ref={searchRef} aria-label={i18nCopy(zh, "copy.components_finance_PositionsView.007")} placeholder={i18nCopy(zh, "copy.components_finance_PositionsView.008")} value={query} onChange={(e) => setQuery(e.target.value)} />
      {!predictionOnly && <ChoiceSelect aria-label={i18nCopy(zh, "copy.components_finance_PositionsView.009")} value={side} onValueChange={setSide}><option value="all">{i18nCopy(zh, "copy.components_finance_PositionsView.010")}</option><option value="long">{i18nCopy(zh, "copy.components_finance_PositionsView.011")}</option><option value="short">{i18nCopy(zh, "copy.components_finance_PositionsView.012")}</option><option value="unknown">{i18nCopy(zh, "copy.components_finance_PositionsView.013")}</option></ChoiceSelect>}
      <ChoiceSelect aria-label={i18nCopy(zh, "copy.components_finance_PositionsView.014")} value={sort} onValueChange={setSort}><option value="market">{i18nCopy(zh, "copy.components_finance_PositionsView.015")}</option><option value="profit">{i18nCopy(zh, "copy.components_finance_PositionsView.016")}</option><option value="loss">{i18nCopy(zh, "copy.components_finance_PositionsView.017")}</option></ChoiceSelect>
      {query || side !== "all" ? <button type="button" onClick={() => { setQuery(""); setSide("all"); searchRef.current?.focus(); }}>{i18nCopy(zh, "copy.components_finance_PositionsView.018")}</button> : null}
    </div> : null}
    {loading && !positions.length ? <p className={styles.empty} role="status">{i18nCopy(zh, "copy.components_finance_PositionsView.019")}</p> : !rows.length ? <p className={styles.empty}>{query || side !== "all" ? (i18nCopy(zh, "copy.components_finance_PositionsView.020")) : error ? (i18nCopy(zh, "copy.components_finance_PositionsView.021")) : (i18nCopy(zh, "copy.components_finance_PositionsView.022"))}</p> : <>
      <div className={styles.columns} aria-hidden><span>{i18nCopy(zh, "copy.components_finance_PositionsView.023")}</span><span className="text-right">{i18nCopy(zh, "copy.components_finance_PositionsView.024")}</span><span className="text-right">{i18nCopy(zh, "copy.components_finance_PositionsView.025")}</span><span className="text-right">{i18nCopy(zh, "copy.components_finance_PositionsView.026")}</span></div>
      {rows.map((p, i) => <details className={styles.position} key={positionKeys.get(p)} data-testid="position-row">
        <summary className={styles.row} aria-label={`${instrumentLabel(p.market, p.instrument)} ${p.instrument?.outcome || dir(p)}`}>
          <span><InstrumentIdentity market={p.market} instrument={p.instrument} secondary={dir(p)} /><span className={styles.sub}>{p.account_id || (i18nCopy(zh, "copy.components_finance_PositionsView.029"))}</span></span>
          <span data-secondary>{financeNumber(p.size_base ?? p.size, locale)}<span className={styles.sub}>{p.instrument?.type === "prediction_outcome" || p.instrument?.quantity_unit === "shares" ? tp("shares") : i18nCopy(zh, "copy.components_finance_PositionsView.030")}</span></span>
          <span data-secondary>{financeNumber(p.avg_entry_price ?? p.avg_price, locale)}<span className={styles.sub}>{financeNumber(p.mark_price, locale)} {p.instrument?.price_unit}</span></span>
          <span className={financeTone(p.unrealized_pnl_usd)}>{financeMoney(p.unrealized_pnl_usd, locale, true)}<span className={styles.sub}>{i18nCopy(zh, "copy.components_finance_PositionsView.031")}</span></span><ChevronRightIcon size={13} />
        </summary>
        <dl className={styles.detail}>{[
          [p.instrument?.type === "prediction_outcome" || p.instrument?.quantity_unit === "shares" ? tp("shares") : i18nCopy(zh, "copy.components_finance_PositionsView.032"), financeNumber(p.size_base ?? p.size, locale)],
          [i18nCopy(zh, "copy.components_finance_PositionsView.033"), financeNumber(p.avg_entry_price ?? p.avg_price, locale)],
          [i18nCopy(zh, "copy.components_finance_PositionsView.034"), financeNumber(p.mark_price, locale)],
          ...(p.instrument?.type === "prediction_outcome" ? [] : [[i18nCopy(zh, "copy.components_finance_PositionsView.035"), financeMoney(p.notional_usd, locale)]]),
          [i18nCopy(zh, "copy.components_finance_PositionsView.036"), financeMoney(p.market_value_usd, locale)],
          [i18nCopy(zh, "copy.components_finance_PositionsView.037"), financeMoney(p.realized_pnl_usd, locale, true)],
        ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
          {p.instrument ? <><div><dt>{tp("originalId")}</dt><dd className="break-all">{p.market}</dd></div><div><dt>{tp("pricePerShare", { asset: p.instrument.price_unit || "—" })}</dt><dd>{financeNumber(p.mark_price, locale)}</dd></div></> : null}
          <div><dt>{i18nCopy(zh, "copy.components_finance_PositionsView.038")}</dt><dd>{modes[p.account_id] === "live" || modes[p.account_id] === "paper" ? <ModePill mode={modes[p.account_id]} /> : modes[p.account_id] || (i18nCopy(zh, "copy.components_finance_PositionsView.039"))}</dd></div>
          <div><dt>{i18nCopy(zh, "copy.components_finance_PositionsView.040")}</dt><dd>{p.shares?.filter(share => Math.abs(share.size_base) > 0).map(share => share.strategy_id).join(" · ") || (p.strategy_id === "__merged__" ? "—" : p.strategy_id || i18nCopy(zh, "copy.components_finance_PositionsView.041"))}</dd></div>
        </dl>
        <div className="flex flex-wrap items-center gap-2 px-3 pb-3"><FinanceReview position={p} mode={modes[p.account_id]} />{p.account_id ? <Link className={styles.action} href={`/accounts/${encodeURIComponent(p.account_id)}`}>{i18nCopy(zh, "copy.components_finance_PositionsView.042")}</Link> : null}</div>
      </details>)}
    </>}
  </section>;
}
