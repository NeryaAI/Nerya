"use client";

import { useMemo, useState, type ReactNode } from "react";
import { useLocale } from "next-intl";
import type { ChartBlockShape } from "../../lib/chartBlock";
import { chartSummary } from "../../lib/financialChart";
import { chartTime, financeNumber } from "../../lib/financeDisplay";
import { ChartCanvas } from "../chat/ChartCanvas";
import styles from "./FinanceSurface.module.css";

export function ChartSummaryHeader({ block }: { block: ChartBlockShape }) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const summary = useMemo(() => chartSummary(block), [block]);
  const unit = summary.primary?.price_format === "percent" ? "%" : "";
  const asOf = chartTime(block.source?.as_of);
  return <div data-testid="chart-summary" className="min-w-0">
    <div className={styles.quoteHeader}>
    <div className="min-w-0"><h2 className="text-base font-semibold">{block.title}</h2><p className="mt-1 break-words text-xs text-[color:var(--text-muted)]">{block.subtitle || (zh ? "数据快照" : "Data snapshot")}</p></div>
    {summary.data.length ? <div className="min-w-0 space-y-1">
      <div><span className="text-[26px] font-semibold tabular-nums" data-testid="chart-last-value">{financeNumber(summary.end, locale, summary.end !== null && Math.abs(summary.end) >= 1 ? 2 : undefined)}{unit}</span><span className="ml-2 text-xs text-[color:var(--text-muted)]">{zh ? "最后观测值" : "Last observed"}</span></div>
      <span className="text-xs text-[color:var(--text-muted)]">{zh ? "所示区间变化" : "Change over shown range"} <strong className="ml-1 font-medium tabular-nums text-[color:var(--text-base)]">{summary.change !== null && summary.change > 0 ? "+" : ""}{financeNumber(summary.change, locale, summary.change !== null && Math.abs(summary.change) >= 1 ? 2 : undefined)}{unit ? (zh ? " 个百分点" : " pp") : ""}{!unit && summary.percent !== null ? ` (${summary.percent > 0 ? "+" : ""}${financeNumber(summary.percent, locale, 2)}%)` : ""}</strong></span>
    </div> : null}
    </div>
    <p className="mb-2 text-[11px] text-[color:var(--text-muted)]">{zh ? "数据快照" : "Data snapshot"}{asOf !== null ? ` · ${new Date(asOf * 1000).toLocaleString(locale)}` : (zh ? " · 更新时间未提供" : " · Timestamp not provided")}</p>
  </div>;
}

function ChartSource({ block }: { block: ChartBlockShape }) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const summary = useMemo(() => chartSummary(block), [block]);
  const time = (value: unknown) => { const ts = chartTime(value); return ts === null ? (zh ? "时间未提供" : "Time not provided") : new Date(ts * 1000).toLocaleString(locale); };
  return <details className="mt-2 pb-2 text-xs text-[color:var(--text-muted)]" data-testid="chart-source"><summary className="w-fit cursor-pointer py-1">{zh ? "来源与时间" : "Source and time"}</summary><dl className="grid gap-2 py-2">
      <div><dt>{zh ? "来源" : "Source"}</dt><dd className="break-all">{[block.source?.skill, block.source?.action].filter(Boolean).join(" / ") || (zh ? "未提供" : "Not provided")}</dd></div>
      <div><dt>{zh ? "数据截至" : "As of"}</dt><dd>{time(block.source?.as_of)}</dd></div>
      {summary.firstTime ? <div><dt>{zh ? "观测区间（本地时间）" : "Observation window (local time)"}</dt><dd>{time(summary.firstTime)} → {time(summary.lastTime)}</dd></div> : null}
      {block.source?.cite_url && /^https?:\/\//i.test(block.source.cite_url) ? <a className="underline" href={block.source.cite_url} target="_blank" rel="noopener noreferrer">{zh ? "查看原始来源" : "Open original source"}</a> : null}
    </dl></details>;
}

export function FinancialChart({ block, height = 280, controls }: { block: ChartBlockShape; height?: number; controls?: ReactNode }) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const [view, setView] = useState("chart"), [expanded, setExpanded] = useState(false);
  const [presentation, setPresentation] = useState("candles"), [reset, setReset] = useState(0);
  const [exportError, setExportError] = useState("");
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const seriesKey = (series: ChartBlockShape["series"][number]) => `${series.type}:${series.name}`;
  const [limit, setLimit] = useState(100);
  const normalized = useMemo(() => ({ ...block, series: chartSummary(block).series }), [block]);
  const visible = useMemo(() => ({ ...normalized,
    series: normalized.series.filter((series) => !hidden.has(seriesKey(series))).map((series) =>
      presentation === "line" && (series.type === "candlestick" || series.type === "bar")
        ? { ...series, type: "line" as const, data: (series.data || []).map((point) => ({ time: point.time, value: "close" in point ? point.close : point.value })) } : series),
    overlays: normalized.series[0] && hidden.has(seriesKey(normalized.series[0])) ? [] : normalized.overlays,
    ...(reset ? { default_range: undefined } : {}),
  }), [normalized, hidden, presentation, reset]);
  const hasCandles = normalized.series.some((series) => series.type === "candlestick" || series.type === "bar");
  const rows = normalized.series.flatMap((series) => (series.data || []).map((point) => ({ series, point })));
  const badPoints = block.series.reduce((n, s) => n + (s.data?.length || 0), 0) - rows.length;
  function exportData() {
    try {
      const textCell = (text: string) => `"${(/^[=+\-@\t\r]/.test(text) ? "'" + text : text).replace(/"/g, '""')}"`;
      const csv = ["series,time_utc,value,open,high,low,close,volume", ...rows.map(({ series, point }) =>
        [textCell(series.name), new Date(Number(point.time) * 1000).toISOString(), ...("open" in point
          ? ["", point.open, point.high, point.low, point.close, point.volume ?? ""]
          : [point.value, "", "", "", "", ""])].join(","))].join("\r\n");
      const url = URL.createObjectURL(new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" }));
      const anchor = document.createElement("a"); anchor.href = url;
      anchor.download = `${block.title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "-").slice(0, 80) || "chart"}.csv`;
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000); setExportError("");
    } catch { setExportError(zh ? "导出失败，可切换到数据视图查看。" : "Export failed. Open the data view to inspect the observations."); }
  }
  return <section className={styles.surface} data-testid="financial-chart" aria-label={block.title}>
    <ChartSummaryHeader block={normalized} />
    {controls}
    <div className={styles.header} style={{ paddingTop: 0, paddingBottom: 8 }}>
      <div className="flex items-center gap-1" role="group" aria-label={zh ? "图表显示" : "Chart display"}><button type="button" aria-pressed={view === "chart"} onClick={() => setView("chart")}>{zh ? "图表" : "Chart"}</button><button type="button" aria-pressed={view === "data"} onClick={() => setView("data")}>{zh ? "数据" : "Data"}</button></div>
      <div className="flex flex-wrap items-center gap-1">
        {hasCandles && view === "chart" ? <div role="group" aria-label={zh ? "图形类型" : "Plot style"} className="flex gap-1"><button type="button" aria-pressed={presentation === "candles"} onClick={() => setPresentation("candles")}>{zh ? "K线" : "Candles"}</button><button type="button" aria-pressed={presentation === "line"} onClick={() => setPresentation("line")}>{zh ? "走势" : "Line"}</button></div> : null}
        {view === "chart" ? <button type="button" onClick={() => setReset((n) => n + 1)}>{zh ? "重置视图" : "Reset view"}</button> : null}
        <button type="button" onClick={exportData} disabled={!rows.length}>{zh ? "导出 CSV" : "Export CSV"}</button>
        <button type="button" aria-pressed={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? (zh ? "还原图表" : "Restore chart") : (zh ? "放大图表" : "Expand chart")}</button>
      </div>
    </div>
    {view === "chart" ? <>
      {normalized.series.length > 1 ? <div className="mb-2 flex flex-wrap gap-1" role="group" aria-label={zh ? "图例" : "Chart legend"}>{normalized.series.map((s, i) => <button type="button" className={styles.action} key={`${s.name}:${i}`} aria-pressed={!hidden.has(seriesKey(s))} onClick={() => setHidden((previous) => { const next = new Set(previous); const key = seriesKey(s); if (next.has(key)) next.delete(key); else next.add(key); return next; })}>{!hidden.has(seriesKey(s)) ? "✓ " : ""}{s.name}</button>)}</div> : null}
      {visible.series.some((s) => s.data?.length) ? <ChartCanvas key={`${block.chart_id}:${reset}`} block={visible} height={expanded ? 460 : Math.min(360, Math.max(220, height))} /> : <p className={styles.empty}>{rows.length ? (zh ? "所有序列已隐藏，点击图例恢复。" : "All series are hidden. Select a legend item to restore it.") : (zh ? "没有有效观测数据。" : "No valid observations.")}</p>}
    </> : <div className="max-h-[420px] overflow-auto rounded border border-[color:var(--line)]" data-testid="chart-data-table"><table className="table w-full text-xs"><thead><tr>{[zh ? "序列" : "Series", zh ? "时间" : "Time", zh ? "值 / 开高低收" : "Value / OHLC"].map((label) => <th key={label}>{label}</th>)}</tr></thead><tbody>{rows.slice(0, limit).map(({ series, point }, i) => <tr key={`${series.name}:${i}`}><td>{series.name}</td><td className="whitespace-nowrap">{new Date(Number(point.time) * 1000).toLocaleString(locale)}</td><td className="tabular-nums">{"open" in point ? [point.open, point.high, point.low, point.close].map((v) => financeNumber(v, locale)).join(" / ") : financeNumber(point.value, locale)}</td></tr>)}</tbody></table>{rows.length > limit ? <button className={styles.action} type="button" onClick={() => setLimit((n) => n + 100)}>{zh ? "加载更多观测值" : "Load more observations"}</button> : null}</div>}
    <ChartSource block={normalized} />
    {exportError ? <p role="alert" className="mt-2 text-xs text-warn">{exportError}</p> : null}
    {badPoints > 0 ? <p role="status" className="mt-2 text-xs text-warn">{zh ? `${badPoints} 条无效或重复观测未绘制。` : `${badPoints} invalid or duplicate observations were not plotted.`}</p> : null}
    {block.warnings?.length ? <div role="status" className="mt-3 space-y-1 text-xs text-warn">{block.warnings.map((warning, i) => <p key={i}>{warning}</p>)}</div> : null}
    {block.insights?.length ? <details className="mt-3 text-sm"><summary className="w-fit cursor-pointer py-2">{zh ? "分析要点" : "Analysis notes"} ({block.insights.length})</summary><div className="space-y-2 text-[13px] leading-relaxed">{block.insights.map((line, i) => <p key={i}>{line}</p>)}</div></details> : null}
    {block.caption ? <p className={`${styles.note} mt-3`}>{block.caption}</p> : null}
  </section>;
}
