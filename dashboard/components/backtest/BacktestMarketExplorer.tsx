"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useLocale } from "next-intl";
import { createVelaChart, type VelaChart } from "../../lib/velaChart";
import type { BacktestChartData, BacktestPanel } from "../../lib/clientApi";
import { replayCandles, replayMarkers, replayNumber, replayTrades, type ReplayCandle, type ReplayMarker } from "../../lib/backtestMarket";
import { financeNumber } from "../../lib/financeDisplay";
import { useChartTheme } from "../../lib/chartTheme";
import { ChoiceSelect } from "../ChoiceSelect";
import { Icon } from "../icons";
import styles from "./BacktestMarketExplorer.module.css";

export function BacktestMarketExplorer({ panels, tables, meta }: {
  panels: BacktestPanel[]; tables: BacktestChartData["tables"]; meta: BacktestChartData["meta"];
}) {
  const locale = useLocale(), zh = locale.startsWith("zh"), id = useId();
  const availablePanels = useMemo<BacktestPanel[]>(() => {
    const recorded = replayTrades(tables, []).map(trade => trade.market);
    const declared = Array.isArray(meta.markets) ? meta.markets.filter((market): market is string => typeof market === "string") : [];
    const known = new Set(panels.map(panel => panel.market));
    const missing = [...new Set([...declared, ...recorded])].filter(market => !known.has(market));
    return [...panels, ...missing.map(market => ({ id: `missing:${market}`, market, title: market || (i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.001")), type: "candlestick", series: [] } satisfies BacktestPanel))];
  }, [panels, tables, meta, zh]);
  const [picked, setPicked] = useState("");
  const panel = availablePanels.find(item => item.id === picked) || availablePanels[0];
  const candles = useMemo(() => panel ? replayCandles(panel) : [], [panel]);
  const markers = useMemo(() => panel ? replayMarkers(panel) : [], [panel]);
  const allTrades = useMemo(() => replayTrades(tables, availablePanels), [tables, availablePanels]);
  const trades = allTrades.filter(trade => panel?.market ? trade.market === panel.market : !panel || availablePanels.length === 1 || !trade.market);
  const [showTrades, setShowTrades] = useState(true), [showGBS, setShowGBS] = useState(true);
  const [selected, setSelected] = useState(""), [page, setPage] = useState(0), [fit, setFit] = useState(0);
  const [side, setSide] = useState("all");
  const rows = trades.filter(trade => side === "all" || trade.side === side);
  const pageSize = 25, currentPage = Math.min(page, Math.max(0, Math.ceil(rows.length / pageSize) - 1));
  const visibleMarkers = useMemo(() => markers.filter(marker => marker.kind === "gbs" ? showGBS : showTrades), [markers, showGBS, showTrades]);
  const selectedTrade = trades.find(trade => trade.id === selected);
  const selectedMarker = markers.find(marker => marker.id === selected);
  const gbsCount = markers.filter(marker => marker.kind === "gbs").length;
  const format = (value: unknown) => { const number = replayNumber(value); return number === null ? "—" : financeNumber(number, locale); };
  const when = (time: number | null | undefined) => time ? new Date(time * 1000).toISOString().replace("T", " ").slice(0, 19) : "—";
  useEffect(() => { setSelected(""); setPage(0); setSide("all"); }, [panel?.id]);
  function selectMarker(markerId: string) {
    setSelected(markerId); setSide("all");
    const index = trades.findIndex(trade => trade.id === markerId);
    if (index >= 0) {
      setPage(Math.floor(index / pageSize));
      requestAnimationFrame(() => document.getElementById(`${id}-${markerId}`)?.scrollIntoView({ block: "nearest", inline: "nearest" }));
    }
  }
  function downloadTrades() {
    const columns = [...new Set(trades.flatMap(trade => Object.keys(trade.row)))];
    // CSV formula protection; displayed values and raw source remain unchanged.
    const cell = (value: unknown) => { let text = value == null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value); if (/^[=+@\t\r]/.test(text) || /^-[^\d.]/.test(text)) text = "'" + text; return '"' + text.replaceAll('"', '""') + '"'; };
    const csv = [columns.map(cell).join(","), ...trades.map(trade => columns.map(column => cell(trade.row[column])).join(","))].join("\r\n");
    const url = URL.createObjectURL(new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a"); anchor.href = url;
    anchor.download = `backtest-${(panel?.market || "trades").replace(/[^a-z0-9_-]/gi, "-")}-displayed.csv`;
    anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <div className={styles.explorer} data-testid="backtest-market-explorer" data-market={panel?.market || ""}>
    <div className={styles.toolbar}>
      <div className={styles.market}><label htmlFor={`${id}-market`}>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.002")}</label>
        <ChoiceSelect id={`${id}-market`} aria-label={i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.003")} value={panel?.id || ""} onValueChange={setPicked}>
          {availablePanels.map(item => <option key={item.id} value={item.id}>{item.market || item.title || (i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.004"))}</option>)}
        </ChoiceSelect><span className={styles.interval}>{panel?.interval || String(meta.tf || "")} · UTC</span>
      </div>
      <div className={styles.legend} aria-label={i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.005")}>
        <button type="button" className={styles.toggle} aria-pressed={showTrades} onClick={() => setShowTrades(value => !value)}><span className={styles.buy}>B</span><span className={styles.sell}>S</span>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.006")}</button>
        <button type="button" className={styles.toggle} aria-pressed={showGBS} onClick={() => setShowGBS(value => !value)}><span className={styles.gbs}>●</span>GBS <span>{gbsCount}</span></button>
        <button type="button" className={styles.button} onClick={() => { setSelected(""); setFit(value => value + 1); }} title={i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.007")}><Icon name="chart" size={14}/>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.008")}</button>
      </div>
    </div>
    {candles.length > 0 && <p className={styles.notice} data-testid="backtest-loaded-range">
      {i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.009")} {candles.length.toLocaleString(locale)} {i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.010")} · {when(candles[0].time)} – {when(candles.at(-1)?.time)} UTC
      {selectedMarker ? (i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.011")) : ""}
    </p>}
    {candles.length > 0 ? <MarketPlot key={panel?.id} candles={candles} markers={visibleMarkers} selected={selectedMarker} onSelect={selectMarker} fit={fit} locale={locale}/>
      : <div className={styles.empty} role="status">{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.012")}</div>}
    <p className={styles.notice}>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.013")}</p>
    {!gbsCount && <p className={styles.notice} data-testid="backtest-gbs-empty">{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.014")}</p>}
    {gbsCount > 0 && <p className={styles.notice}>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.015")}</p>}
    <div className={styles.heading}><div><h3>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.016")}<span>{trades.length}</span></h3></div>
      <div className={styles.legend}><ChoiceSelect aria-label={i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.017")} value={side} onValueChange={value => { setSide(value); setPage(0); }}>
        <option value="all">{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.018")}</option><option value="buy">{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.019")}</option><option value="sell">{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.020")}</option>
      </ChoiceSelect><button type="button" className={styles.button} disabled={!trades.length} onClick={downloadTrades}>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.021")}</button></div>
    </div>
    {!rows.length ? <div className={styles.empty} role="status">{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.022")}</div>
      : <div className={styles.tableScroll}><table className={styles.table} aria-label={i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.023")}>
        <thead><tr>{([i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.024"), i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.025"), i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.026"), i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.027"), i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.028")]).map(label => <th key={label} scope="col">{label}</th>)}</tr></thead>
        <tbody>{rows.slice(currentPage * pageSize, (currentPage + 1) * pageSize).map(trade => <tr key={trade.id} id={`${id}-${trade.id}`} className={styles.row} tabIndex={0} aria-selected={selected === trade.id} data-testid="backtest-trade-row" data-trade-id={trade.id} onClick={() => { setSelected(trade.id); setShowTrades(true); }} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelected(trade.id); setShowTrades(true); } }}>
          <td><time dateTime={trade.time ? new Date(trade.time * 1000).toISOString() : undefined}>{when(trade.time)}</time></td>
          <td className={trade.side === "buy" ? styles.buy : styles.sell}>{trade.side === "buy" ? (i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.029")) : trade.side === "sell" ? (i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.030")) : trade.side || "—"}</td>
          <td title={String(trade.row.price ?? "")}>{format(trade.row.price)}</td><td title={String(trade.row.qty ?? trade.row.size ?? "")}>{format(trade.row.qty ?? trade.row.size)}</td><td title={String(trade.row.fee ?? "")}>{format(trade.row.fee)}</td>
        </tr>)}</tbody></table></div>}
    <div className={styles.pagination}><span>{rows.length ? `${currentPage * pageSize + 1}–${Math.min((currentPage + 1) * pageSize, rows.length)} / ${rows.length}` : "0"}
      {replayNumber(meta.trade_count) !== null && Number(meta.trade_count) > allTrades.length ? ` · ${i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.031")}` : ""}</span>
      {rows.length > pageSize && <div><button type="button" className={styles.button} disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.032")}</button><button type="button" className={styles.button} disabled={(currentPage + 1) * pageSize >= rows.length} onClick={() => setPage(currentPage + 1)}>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.033")}</button></div>}
    </div>
    {(selectedTrade || selectedMarker) && <section className={styles.selection} data-testid="backtest-execution-detail" aria-live="polite">
      <h4>{selectedMarker?.kind === "gbs" ? (i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.034")) : (i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.035"))}</h4>
      <dl>{[[i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.036"), panel?.market || selectedTrade?.market || "—"], [i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.037"), when(selectedTrade?.time ?? selectedMarker?.execution_ts ?? selectedMarker?.time)],
        [i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.038"), format(selectedTrade?.row.price ?? selectedMarker?.price)], [i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.039"), String(selectedTrade?.row.reason || selectedMarker?.reason || "—")]].map(([label,value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      {!selectedMarker && <p className={styles.notice}>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.040")}</p>}
      <details><summary>{i18nCopy(zh, "copy.components_backtest_BacktestMarketExplorer.041")}</summary><pre>{JSON.stringify(selectedTrade?.row || selectedMarker, null, 2)}</pre></details>
    </section>}
  </div>;
}

function MarketPlot({ candles, markers, selected, onSelect, fit, locale }: {
  candles: ReplayCandle[]; markers: ReplayMarker[]; selected?: ReplayMarker;
  onSelect: (id: string) => void; fit: number; locale: string;
}) {
  const [node, setNode] = useState<HTMLDivElement | null>(null), [hover, setHover] = useState<ReplayCandle | null>(null);
  const theme = useChartTheme();
  const plot = useRef<VelaChart | null>(null);
  const onSelectRef = useRef(onSelect); onSelectRef.current = onSelect;
  useEffect(() => {
    if (!node || !candles.length) return;
    const chart = createVelaChart(node, { height: 330, theme, embedded: true, series: [{ type: "candlestick", name: "OHLC", data: candles }] });
    plot.current = chart;
    const candleMap = new Map(candles.map(candle => [candle.time * 1000, candle]));
    const crosshair = chart.onCrosshairMove(event => { const candle = event.time === null ? undefined : candleMap.get(event.time); setHover(old => old?.time === candle?.time ? old : candle || null); });
    const click = chart.onMarkerClick(id => onSelectRef.current(id));
    return () => { crosshair(); click(); plot.current = null; chart.destroy(); };
  }, [node, candles, theme]);
  useEffect(() => {
    const chart = plot.current;
    if (!chart) return;
    chart.setOverlays({ markers: markers.map(marker => ({ ...marker, size: selected?.id === marker.id ? 1.5 : 1 })),
      overlays: selected?.price !== null && selected?.price !== undefined ? [{ type: "price_line", price: selected.price, color: selected.color, line_style: "dashed", title: selected.text }] : [] });
    if (selected) { const index = candles.findIndex(candle => candle.time === selected.time); if (index >= 0) chart.focusIndex(index); }
  }, [markers, selected, node, candles, theme]);
  useEffect(() => { if (fit) plot.current?.fitContent(); }, [fit]);
  const current = hover || candles.at(-1);
  return <div className={styles.plot}><div className={styles.readout} aria-hidden="true">{current && <><span>{new Date(current.time * 1000).toISOString().replace("T", " ").slice(0,16)} UTC</span>{(["open","high","low","close"] as const).map((key,index) => <span key={key}>{["O","H","L","C"][index]} <strong>{financeNumber(current[key], locale)}</strong></span>)}</>}</div>
    <div ref={setNode} className={styles.canvas} role="img" aria-label={i18nCopy(locale.startsWith("zh"), "copy.components_backtest_BacktestMarketExplorer.042")} data-testid="backtest-market-chart" data-candle-count={candles.length} data-marker-count={markers.length} data-start={candles[0]?.time} data-end={candles.at(-1)?.time}/>
  </div>;
}
