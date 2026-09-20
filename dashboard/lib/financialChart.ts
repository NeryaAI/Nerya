import type { ChartBlockShape, ChartSeries, ChartSeriesPoint } from "./chartBlock";
import { chartTime, finiteNumber } from "./financeDisplay";

export function cleanSeries(series: ChartSeries): ChartSeries {
  const rows = new Map<number, ChartSeriesPoint>();
  for (const point of series.data || []) {
    if (!point || typeof point !== "object") continue;
    const time = chartTime(point.time);
    if (time === null) continue;
    if (series.type === "candlestick" || series.type === "bar") {
      if (!("open" in point)) continue;
      const [open, high, low, close] = [point.open, point.high, point.low, point.close].map(finiteNumber);
      if (open === null || high === null || low === null || close === null || high < Math.max(open, low, close) || low > Math.min(open, high, close)) continue;
      const volume = finiteNumber(point.volume);
      rows.set(time, { time, open, high, low, close, ...(volume !== null && volume >= 0 ? { volume } : {}) });
    } else if ("value" in point) {
      const value = finiteNumber(point.value);
      if (value !== null) rows.set(time, { time, value });
    }
  }
  return { ...series, data: [...rows.entries()].sort(([a], [b]) => a - b).map(([, point]) => point) };
}
export function chartSummary(block: ChartBlockShape) {
  const series = block.series.map(cleanSeries);
  const primary = series.find((s) => s.type === "candlestick" && s.data?.length)
    || series.find((s) => s.price_format !== "volume" && s.data?.length) || series.find((s) => s.data?.length);
  const data = primary?.data || [], first = data[0], last = data[data.length - 1];
  const start = first ? ("open" in first ? first.open : first.value) : null;
  const end = last ? ("close" in last ? last.close : last.value) : null;
  const change = start !== null && end !== null ? end - start : null;
  const percent = start !== null && start > 0 && change !== null ? change / start * 100 : null;
  return { series, primary, data, start, end, change, percent, firstTime: first?.time, lastTime: last?.time };
}
