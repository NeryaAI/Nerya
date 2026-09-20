"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useState } from "react";
import { useLocale } from "next-intl";
import Link from "next/link";
import type { SnapshotAccount } from "../../lib/portfolioSnapshot";
import { chartTime, financeMoney, financeTone, sumKnown } from "../../lib/financeDisplay";
import { PositionsView } from "./PositionsView";
import { ModePill } from "../ModePill";
import styles from "./FinanceSurface.module.css";

export function PortfolioSnapshot({ accounts }: { accounts: SnapshotAccount[] }) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const [selected, setSelected] = useState("");
  const account = accounts.find((a) => a.id === selected) || accounts[0];
  if (!account) return <p className="py-3 text-sm text-[color:var(--text-muted)]">{zh ? "这次查询没有返回账户记录。" : "No accounts were returned by this query."}</p>;
  const total = account.positionsKnown ? sumKnown(account.positions.map((p) => p.unrealized_pnl_usd)) : null;
  const time = chartTime(account.asOf);
  return <section className={styles.surface} data-testid="portfolio-snapshot">
    <div className={styles.header}><h2>{zh ? "账户快照" : "Account snapshot"}</h2><span className={styles.note}>{zh ? "来自本次任务，非实时刷新" : "From this task, not a live feed"}</span></div>
    <div className={styles.header} style={{ paddingTop: 0 }}><ChoiceSelect aria-label={zh ? "快照账户" : "Snapshot account"} value={account.id} onValueChange={setSelected}>{accounts.map((a) => <option key={a.id} value={a.id}>{a.id} · {a.mode || (zh ? "模式未提供" : "Mode not provided")}</option>)}</ChoiceSelect>{account.mode === "paper" || account.mode === "live" ? <ModePill mode={account.mode} /> : <span className={styles.note}>{zh ? "模式未提供" : "Mode not provided"}</span>}</div>
    <dl className={styles.metrics}>
      <div><dt>{zh ? "账户净值" : "Account equity"}</dt><dd>{financeMoney(account.equity, locale)}</dd></div><div><dt>{zh ? "现金余额" : "Cash balance"}</dt><dd>{financeMoney(account.cash, locale)}</dd></div>
      <div><dt>{zh ? "当前持仓浮盈亏" : "Open-position P&L"}</dt><dd className={financeTone(total)}>{financeMoney(total, locale, true)}</dd></div><div><dt>{zh ? "累计费用" : "Fees paid"}</dt><dd>{financeMoney(account.fees, locale)}</dd></div>
    </dl>
    <PositionsView key={account.id} positions={account.positions} modes={{ [account.id]: account.mode }} error={!account.positionsKnown ? (zh ? "查询未提供完整的仓位记录。" : "Complete position records were not supplied.") : undefined} />
    <footer className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-[color:var(--text-muted)]"><span>{zh ? "快照时间：" : "Snapshot time: "}{time === null ? (zh ? "未提供" : "Not provided") : new Date(time * 1000).toLocaleString(locale)}</span><Link href="/portfolio" className={styles.action}>{zh ? "打开资产与持仓" : "Open portfolio"}</Link></footer>
  </section>;
}
