"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useEffect, useMemo, useRef, useState } from "react";
import { useLocale } from "next-intl";
import Link from "next/link";
import type { PortfolioPosition } from "../../lib/api";
import { finiteNumber, financeMoney, financeNumber, financeTone, positionSide } from "../../lib/financeDisplay";
import { ChevronRightIcon } from "../icons";
import { ModePill } from "../ModePill";
import { FinanceReview } from "./FinanceReview";
import styles from "./FinanceSurface.module.css";

export function PositionsView({ positions, modes = {}, loading = false, error, title, filterRequest }: {
  positions: PortfolioPosition[]; modes?: Record<string, string>; loading?: boolean; error?: string | null; title?: string;
  filterRequest?: { market: string; count: number };
}) {
  const locale = useLocale(), zh = locale.startsWith("zh");
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
  const rows = useMemo(() => positions.filter((p) => `${p.market || ""} ${p.account_id || ""}`.toLowerCase().includes(query.trim().toLowerCase()) && (side === "all" || positionSide(p) === side)).sort((a, b) => {
    if (sort === "market") return String(a.market || "").localeCompare(String(b.market || ""));
    const x = finiteNumber(a.unrealized_pnl_usd), y = finiteNumber(b.unrealized_pnl_usd);
    if (x === null) return y === null ? 0 : 1;
    if (y === null) return -1;
    return sort === "profit" ? y - x : x - y;
  }), [positions, query, side, sort]);
  const dir = (p: PortfolioPosition) => positionSide(p) === "long" ? (zh ? "多头" : "Long") : positionSide(p) === "short" ? (zh ? "空头" : "Short") : (zh ? "方向未提供" : "Side not provided");
  return <section className={styles.surface} data-testid="positions-view" aria-label={title || (zh ? "持仓" : "Positions")}>
    <div className={styles.header}><h2>{title || (zh ? "持仓" : "Positions")} <span className="ml-1 text-xs text-[color:var(--text-muted)]">{positions.length}</span></h2><span className={styles.note}>{zh ? "点开仓位查看明细" : "Open a position for details"}</span></div>
    {error ? <p role="status" className="pb-2 text-xs text-warn">{error}</p> : null}
    {positions.length ? <div className={styles.filters}>
      <input ref={searchRef} aria-label={zh ? "搜索仓位" : "Search positions"} placeholder={zh ? "搜索市场或账户" : "Search market or account"} value={query} onChange={(e) => setQuery(e.target.value)} />
      <ChoiceSelect aria-label={zh ? "仓位方向" : "Position side"} value={side} onValueChange={setSide}><option value="all">{zh ? "全部方向" : "All sides"}</option><option value="long">{zh ? "多头" : "Long"}</option><option value="short">{zh ? "空头" : "Short"}</option><option value="unknown">{zh ? "未知方向" : "Unknown"}</option></ChoiceSelect>
      <ChoiceSelect aria-label={zh ? "仓位排序" : "Sort positions"} value={sort} onValueChange={setSort}><option value="market">{zh ? "按市场" : "Market"}</option><option value="profit">{zh ? "浮盈优先" : "Highest P&L"}</option><option value="loss">{zh ? "浮亏优先" : "Lowest P&L"}</option></ChoiceSelect>
      {query || side !== "all" ? <button type="button" onClick={() => { setQuery(""); setSide("all"); searchRef.current?.focus(); }}>{zh ? "清除筛选" : "Clear filters"}</button> : null}
    </div> : null}
    {loading && !positions.length ? <p className={styles.empty} role="status">{zh ? "正在加载仓位…" : "Loading positions…"}</p> : !rows.length ? <p className={styles.empty}>{query || side !== "all" ? (zh ? "没有匹配的仓位，请调整筛选。" : "No matching positions. Adjust your filters.") : error ? (zh ? "仓位暂不可用，不代表已清仓。" : "Positions unavailable, not a confirmed empty account.") : (zh ? "此范围没有已记录的持仓。" : "No positions recorded in this scope.")}</p> : <>
      <div className={styles.columns} aria-hidden><span>{zh ? "市场 / 方向" : "Market / Side"}</span><span className="text-right">{zh ? "数量" : "Size"}</span><span className="text-right">{zh ? "开仓 / 标记价" : "Entry / Mark"}</span><span className="text-right">{zh ? "未实现盈亏 · USD" : "Unrealized P&L · USD"}</span></div>
      {rows.map((p, i) => <details className={styles.position} key={positionKeys.get(p)} data-testid="position-row">
        <summary className={styles.row} aria-label={`${p.market || (zh ? "未知市场" : "Unknown market")} ${dir(p)}`}>
          <span><strong>{p.market || (zh ? "市场未提供" : "Market not provided")}</strong><span className={styles.sub}>{dir(p)} <span className="ml-2">{p.account_id || (zh ? "账户未提供" : "Account not provided")}</span></span></span>
          <span data-secondary>{financeNumber(p.size_base ?? p.size, locale)}<span className={styles.sub}>{zh ? "报告数量" : "Reported size"}</span></span>
          <span data-secondary>{financeNumber(p.avg_entry_price ?? p.avg_price, locale)}<span className={styles.sub}>{financeNumber(p.mark_price, locale)}</span></span>
          <span className={financeTone(p.unrealized_pnl_usd)}>{financeMoney(p.unrealized_pnl_usd, locale, true)}<span className={styles.sub}>{zh ? "未实现盈亏" : "Unrealized P&L"}</span></span><ChevronRightIcon size={13} />
        </summary>
        <dl className={styles.detail}>{[
          [zh ? "持仓数量" : "Position size", financeNumber(p.size_base ?? p.size, locale)],
          [zh ? "开仓均价" : "Average entry", financeNumber(p.avg_entry_price ?? p.avg_price, locale)],
          [zh ? "标记价格" : "Mark price", financeNumber(p.mark_price, locale)],
          [zh ? "名义价值 · USD" : "Notional · USD", financeMoney(p.notional_usd, locale)],
          [zh ? "市值 · USD" : "Market value · USD", financeMoney(p.market_value_usd, locale)],
          [zh ? "该仓位已实现盈亏" : "Position realized P&L", financeMoney(p.realized_pnl_usd, locale, true)],
        ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
          <div><dt>{zh ? "模式" : "Mode"}</dt><dd>{modes[p.account_id] === "live" || modes[p.account_id] === "paper" ? <ModePill mode={modes[p.account_id]} /> : modes[p.account_id] || (zh ? "未提供" : "Not provided")}</dd></div>
          <div><dt>{zh ? "策略归属" : "Strategy attribution"}</dt><dd>{p.strategy_id || (zh ? "未提供，不能推断" : "Not provided")}</dd></div>
        </dl>
        <div className="flex flex-wrap items-center gap-2 px-3 pb-3"><FinanceReview position={p} mode={modes[p.account_id]} />{p.account_id ? <Link className={styles.action} href={`/accounts/${encodeURIComponent(p.account_id)}`}>{zh ? "查看账户" : "View account"}</Link> : null}</div>
      </details>)}
    </>}
  </section>;
}
