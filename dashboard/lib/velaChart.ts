import { NativeRenderer, Vela, type IndicatorModel, type OHLCV as VelaBar } from "@luxalgo/vela";
import type { ChartOverlay, ChartSeries, ChartSeriesPoint } from "./chartBlock";
import type { ChartTheme } from "./chartTheme";
import { chartTime } from "./financeDisplay";
import { cleanSeries } from "./financialChart";

export type VelaSeries = Omit<ChartSeries, "data"> & { data?: (ChartSeriesPoint & { color?: string })[] };
export type VelaMarker = {
  id?: string;
  time: number;
  position: "aboveBar" | "belowBar" | "inBar";
  shape: string;
  color: string;
  text?: string;
  size?: number;
};

type Options = {
  height: number;
  theme: ChartTheme;
  series: VelaSeries[];
  overlays?: ChartOverlay[];
  markers?: VelaMarker[];
  range?: { from: number | string; to: number | string };
  embedded?: boolean;
};

type ModelSeries = IndicatorModel["series"][number];
const UP = "#10d993", DOWN = "#ef4560", BRAND = "#b48bff";

/** Keep Nerya's second-based data contract; convert only at Vela's boundary. */
export function velaTime(time: number | string): number {
  return (chartTime(time) ?? 0) * 1000;
}

function model(id: string, paneId: string, series: ModelSeries[] = []): IndicatorModel {
  return { id, title: id, overlay: paneId === "price", paneHint: paneId === "price" ? "price" : "new",
    paneId, legend: false, series, fills: [], backgrounds: [], priceLines: [], inputs: [], inputValues: {} };
}

/** A single native Vela renderer for candles, account curves and Agent artifacts. */
export function createVelaChart(container: HTMLElement, options: Options) {
  const series: VelaSeries[] = options.series.map(row => {
    const colors = new Map((row.data || []).map(point => [velaTime(point.time), point.color]));
    const cleaned = cleanSeries(row);
    return { ...cleaned, data: cleaned.data?.map(point => ({ ...point, color: colors.get(velaTime(point.time)) })) };
  });
  const times = [...new Set(series.flatMap(row => (row.data || []).map(point => velaTime(point.time))))].sort((a, b) => a - b);
  const primary = series.find(row => row.type === "candlestick" || row.type === "bar") || series[0];
  const primaryData = new Map((primary?.data || []).map(point => [velaTime(point.time), point]));
  // The base bars supply Vela's shared time axis. Value charts keep this base hidden.
  const bars: VelaBar[] = times.map(time => {
    const point = primaryData.get(time);
    if (point && "close" in point) return { ...point, time };
    const value = point && "value" in point ? point.value : 0;
    return { time, open: value, high: value, low: value, close: value };
  });
  const renderer = new NativeRenderer();
  container.style.height = `${options.height}px`;
  container.dataset.chartRenderer = "vela";
  const chart = new Vela(container, { data: bars, height: options.height, live: false, drawings: false,
    volume: false, animations: false, currentPriceLine: false,
    theme: { background: "transparent", textColor: options.theme.text, gridColor: options.theme.grid,
      borderColor: options.theme.grid, upColor: UP, downColor: DOWN, fontFamily: "Inter, system-ui, sans-serif" } }, { renderer });
  // Injected renderers do not receive Vela's constructor display options.
  chart.renderer.set({ candleVisible: false, currentPriceLine: false, priceLabel: false, countdown: false,
    intro: false, animZoom: 0, animPan: 0, animScroll: 0, animAutoscale: 0, animLiveBar: 0 });
  let disposed = false;
  let overlayHandle: ReturnType<NativeRenderer["mountIndicator"]> | undefined;
  let markers = options.markers || [];
  let overlays = options.overlays || [];
  const ready = chart.ready().then(async () => {
    if (disposed) return;
    series.forEach((row, index) => {
      const id = `nerya-series-${index}`;
      const paneId = row.price_format === "volume" ? "volume" : "price";
      if (paneId === "volume") renderer.ensurePane({ id: paneId, kind: "study", order: 1, heightWeight: .22 });
      const data = new Map((row.data || []).map(point => [velaTime(point.time), point]));
      const spec = { id, title: row.name, paneId };
      let plots: ModelSeries[];
      if (row.type === "candlestick" || row.type === "bar") {
        // Vela's candle arrays are index-aligned. Anchor each contiguous run so a
        // gap or a second series' extra timestamp never moves a recorded candle.
        let run: VelaBar[] = [];
        const flush = () => {
          if (!run.length) return;
          const runId = `${id}-${run[0].time}`;
          const output = model(runId, paneId, [{ ...spec, id: runId, kind: row.type === "bar" ? "bar" : "candle", bars: run,
            style: { up: row.color || UP, down: DOWN, wickUp: row.color || UP, wickDown: DOWN } }]);
          output.anchorTime = run[0].time;
          renderer.mountIndicator(output);
          run = [];
        };
        for (const time of times) {
          const point = data.get(time);
          if (point && "close" in point) run.push({ ...point, time });
          else flush();
        }
        flush();
        return;
      } else {
        const points = times.map(time => {
          const point = data.get(time);
          return { time, value: point && "value" in point ? point.value : null, color: point?.color };
        });
        const style = { color: row.color || BRAND, width: row.line_width || 2, lineStyle: row.line_style || "solid" as const,
          ...(row.type === "histogram" ? { base: row.base_value ?? 0 } : {}) };
        if (row.type === "baseline") {
          const base = row.base_value ?? 0;
          plots = [true, false].map(above => ({ ...spec, id: `${id}-${above ? "up" : "down"}`, kind: "line" as const,
            points: points.map(point => ({ ...point, value: point.value === null ? null : above ? Math.max(base, point.value) : Math.min(base, point.value) })),
            style: { ...style, color: above ? row.top_color || UP : row.bottom_color || DOWN } }));
          plots.push({ ...spec, id: `${id}-base`, kind: "line", visible: false, points: times.map(time => ({ time, value: base })), style });
        } else {
          plots = [{ ...spec, kind: row.type === "histogram" ? "histogram" : row.type === "area" ? "area" : "line", points, style }];
        }
      }
      const output = model(id, paneId, plots);
      if (row.type === "baseline") {
        output.fills = [true, false].map(above => ({ id: `${id}-fill-${above}`, paneId,
          fromSeriesId: `${id}-${above ? "up" : "down"}`, toSeriesId: `${id}-base`,
          color: above ? row.top_color || "rgba(16,217,147,.2)" : row.bottom_color || "rgba(239,69,96,.2)" }));
      }
      // Ratios/percent series need their own scale beside price/NAV series.
      output.ownScale = row.price_format === "percent" && series.some(item => !item.price_format || item.price_format === "price");
      renderer.mountIndicator(output);
    });
    renderOverlays();
    if (options.range) chart.setVisibleRange({ from: velaTime(options.range.from), to: velaTime(options.range.to) });
    else chart.setVisibleRangePreset("ALL");
    if (options.embedded) container.querySelectorAll("canvas").forEach(canvas => { canvas.style.touchAction = "pan-y"; });
    // The native renderer paints injected models on its next animation frame.
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
    if (!disposed) container.dataset.chartReady = "true";
  });

  function renderOverlays() {
    if (disposed) return;
    if (overlayHandle) renderer.removeIndicator(overlayHandle);
    const output = model("nerya-overlays", "price");
    const allMarkers: VelaMarker[] = [...markers];
    overlays.forEach((overlay, index) => {
      if (overlay.type === "marker" || overlay.type === "annotation") {
        allMarkers.push({ time: (chartTime(overlay.time) ?? 0), position: overlay.type === "marker" && overlay.position === "below" ? "belowBar" : overlay.type === "marker" && overlay.position === "inBar" ? "inBar" : "aboveBar",
          shape: overlay.type === "marker" ? ({ arrow_up: "arrowUp", arrow_down: "arrowDown", square: "square", circle: "circle" }[overlay.shape || "circle"]) : "circle",
          color: overlay.type === "marker" ? overlay.color || "#f5a524" : "#f5a524", text: overlay.text });
      } else if (overlay.type === "price_line" && Number.isFinite(overlay.price)) {
        output.priceLines.push({ id: `line-${index}`, paneId: "price", price: overlay.price,
          color: overlay.color || "#f5a524", lineStyle: overlay.line_style || "solid", width: 1, title: overlay.title });
      } else if (overlay.type === "region") {
        output.backgrounds.push({ id: `region-${index}`, paneId: "price", from: velaTime(overlay.from), to: velaTime(overlay.to), color: overlay.color || "rgba(180,139,255,.12)" });
      }
    });
    // NativeRenderer paints labels; the neutral marker-series primitive is ignored.
    output.labels = allMarkers.filter(marker => chartTime(marker.time) !== null).map((marker, index) => ({
      id: marker.id || `marker-${index}`, paneId: "price", xloc: "bar_time", x: velaTime(marker.time),
      y: primaryData.get(velaTime(marker.time)) ? bars.find(bar => bar.time === velaTime(marker.time))?.close || 0 : 0,
      yloc: marker.position === "aboveBar" ? "abovebar" : marker.position === "belowBar" ? "belowbar" : "price",
      style: marker.shape === "arrowUp" ? "arrowup" : marker.shape === "arrowDown" ? "arrowdown" : marker.shape === "square" ? "square" : "circle",
      color: marker.color, textColor: marker.color, text: marker.text, tooltip: marker.text,
      size: marker.size && marker.size > 1 ? "large" : "small", textAlign: "center", fontFamily: "default",
    }));
    overlayHandle = renderer.mountIndicator(output);
  }

  // Embedded charts let the conversation own wheel/vertical touch scrolling.
  const releaseWheel = (event: WheelEvent) => event.stopPropagation();
  if (options.embedded) {
    container.addEventListener("wheel", releaseWheel, { capture: true, passive: true });
    container.style.touchAction = "pan-y";
  }
  const observer = new ResizeObserver(() => { if (!disposed) chart.resize(); });
  observer.observe(container);
  return {
    chart, ready,
    setOverlays(next: { markers?: VelaMarker[]; overlays?: ChartOverlay[] }) {
      markers = next.markers ?? markers;
      overlays = next.overlays ?? overlays;
      void ready.then(() => renderOverlays());
    },
    fitContent() { void ready.then(() => { if (!disposed) chart.setVisibleRangePreset("ALL"); }); },
    focusIndex(index: number) {
      void ready.then(() => { if (!disposed && times.length) chart.setVisibleRange({ from: times[Math.max(0, index - 18)], to: times[Math.min(times.length - 1, index + 18)] }); });
    },
    onCrosshairMove: chart.renderer.onCrosshairMove.bind(chart.renderer),
    onMarkerClick(callback: (id: string) => void) {
      return chart.renderer.onClick(event => {
        const marker = markers.find(item => item.id && velaTime(item.time) === event.time);
        if (marker?.id) callback(marker.id);
      });
    },
    destroy() {
      disposed = true;
      observer.disconnect();
      container.removeEventListener("wheel", releaseWheel, true);
      chart.destroy();
      delete container.dataset.chartReady;
    },
  };
}

export type VelaChart = ReturnType<typeof createVelaChart>;
