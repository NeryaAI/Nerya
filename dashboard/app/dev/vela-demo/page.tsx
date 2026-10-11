"use client";

import { useState } from "react";
import { notFound } from "next/navigation";
import { CandleChart } from "../../../components/CandleChart";
import { AccountEquityCurveCard } from "../../../components/accounts/AccountEquityCurveCard";
import { BacktestChart } from "../../../components/backtest/BacktestChart";
import { BacktestMarketExplorer } from "../../../components/backtest/BacktestMarketExplorer";
import { FinancialChart } from "../../../components/finance/FinancialChart";
import type { ChartSeriesType } from "../../../lib/chartBlock";

// Frozen synthetic observations for renderer QA. Never reachable in a production build.
const start = 1720000000;
const candles = Array.from({ length: 72 }, (_, i) => {
  const open = 100 + i / 4 + Math.sin(i / 5) * 8, close = open + Math.cos(i) * 2;
  return { time: start + i * 3600, open, high: Math.max(open, close) + 2, low: Math.min(open, close) - 2, close, volume: 100 + i * 5 };
});
const panels = [{ id: "price", title: "Recorded test candles", market: "TEST/USD", type: "candlestick", series: [
  { kind: "candles", name: "OHLC", data: candles },
  { kind: "markers", name: "Recorded markers", data: [
    { id: "trade:0", time: candles[4].time, side: "buy", price: candles[4].close, text: "B", shape: "arrowUp", position: "belowBar" },
    { id: "trade:1", time: candles[22].time, side: "sell", price: candles[22].close, text: "S", shape: "arrowDown", position: "aboveBar" },
    { id: "gbs", time: candles[35].time, kind: "gbs", price: candles[35].close, text: "GBS", shape: "circle", position: "aboveBar" },
  ] },
] }];
const tables = [{ id: "trades", title: "Test executions", columns: ["market", "time", "side", "price", "qty", "fee"], rows: [
  ["TEST/USD", candles[4].time, "buy", candles[4].close, 1, .1],
  ["TEST/USD", candles[22].time, "sell", candles[22].close, 1, .1],
] }];

export default function VelaDemo() {
  const [mode, setMode] = useState<"candlestick" | "line" | "area">("candlestick");
  const [volume, setVolume] = useState(true);
  if (process.env.NODE_ENV !== "development") notFound();
  return <div className="mx-auto max-w-4xl space-y-8 p-4">
    <h1>Vela renderer fixtures · synthetic test data</h1>
    <section data-testid="vela-candles">
      <div className="mb-2 flex gap-2">{(["candlestick", "line", "area"] as const).map(value => <button className="btn btn-ghost" key={value} onClick={() => setMode(value)} aria-pressed={mode === value}>{value}</button>)}
        <button className="btn btn-ghost" onClick={() => setVolume(value => !value)} aria-pressed={volume}>Volume</button></div>
      <CandleChart candles={candles.map(candle => ({ ...candle, ts: candle.time }))} mode={mode} showVolume={volume} />
    </section>
    <section data-testid="vela-account"><AccountEquityCurveCard accountId="vela-fixture" /></section>
    <section data-testid="vela-backtest"><BacktestChart strategyId="vela-fixture" ts="fixture" /></section>
    <section data-testid="vela-executions"><BacktestMarketExplorer panels={panels} tables={tables} meta={{ markets: ["TEST/USD"] }} /></section>
    {(["area", "baseline", "histogram", "bar", "multi"] as const).map(type => <FinancialChart key={type} height={240} block={{
      kind: "chart", chart_id: `vela-${type}`, title: `Test ${type}`, chart_kind: type, path: "inline", source: { skill: "fixture", action: "synthetic", as_of: new Date(start * 1000).toISOString() },
      series: type === "multi" ? [
        { type: "line", name: "NAV", data: candles.map((c, i) => ({ time: c.time, value: 100 + i })) },
        { type: "line", name: "Sparse ratio", price_format: "percent", data: candles.filter((_, i) => i % 2 === 0).map((c, i) => ({ time: c.time, value: i / 100 })) },
      ] : [{ type: type as ChartSeriesType, name: type, base_value: 0, data: type === "bar" ? candles : candles.map((c, i) => ({ time: c.time, value: Math.sin(i / 5) * 4 })) }],
      overlays: [{ type: "price_line", price: 0, line_style: "dashed", title: "Reference" }],
    }} />)}
  </div>;
}
