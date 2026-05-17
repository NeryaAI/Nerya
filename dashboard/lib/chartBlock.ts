// Chart block schema (TypeScript mirror of nerya/agent/chart_block.py).
//
// First-class peer of text / thinking / tool_use / tool_result blocks
// in the workspace-native agent loop. The Agent picks ``path`` ("inline"
// | "bulk") explicitly; the dashboard renders both via the same
// ``ChartBlock`` component and uses ``useChartData`` to bridge the
// two paths.

export const CHART_BLOCK_VERSION = "v1";

export type ChartTimeFormat = "unix_seconds" | "business_day" | "iso8601";

export type ChartTime = {
  timezone: string;
  format: ChartTimeFormat;
};

export type ChartSource = {
  skill: string;
  action: string;
  as_of: string;
  cite_url?: string;
  artifact_path?: string;
};

export type ChartSeriesType =
  | "candlestick"
  | "line"
  | "area"
  | "baseline"
  | "histogram"
  | "bar";

export type OHLCV = {
  time: number | string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
};

export type TimeValue = {
  time: number | string;
  value: number;
};

export type ChartSeriesPoint = OHLCV | TimeValue;

export type ChartSeries = {
  type: ChartSeriesType;
  name: string;
  // Either ``data`` (inline) or ``data_uri`` (bulk slice). Set exactly
  // one. Top-level ``ChartBlock.bulk_data_uri`` covers the common
  // "all series share one artifact" case so individual ``data_uri``
  // is rare.
  data?: ChartSeriesPoint[];
  data_uri?: string;
  color?: string;
  line_style?: "solid" | "dashed" | "dotted";
  line_width?: 1 | 2 | 3;
  top_color?: string;
  bottom_color?: string;
  base_value?: number;
  price_format?: "price" | "percent" | "volume";
};

export type ChartOverlay =
  | {
      type: "marker";
      time: number | string;
      position?: "above" | "below" | "inBar";
      shape?: "arrow_up" | "arrow_down" | "circle" | "square";
      color?: string;
      text?: string;
      tooltip?: string;
    }
  | {
      type: "price_line";
      price: number;
      color?: string;
      line_style?: "solid" | "dashed";
      title?: string;
      axis_label?: boolean;
    }
  | {
      type: "region";
      from: number | string;
      to: number | string;
      color?: string;
      label?: string;
    }
  | {
      type: "annotation";
      time: number | string;
      text: string;
      href?: string;
    };

export type ChartPane = {
  id: string;
  height_ratio: number;
  series_ids: string[];
};

export type ChartUI = {
  height?: number;
  layout?: "compact" | "full";
  palette?: "brand" | "mono" | "diverging";
  show_volume?: boolean;
  show_legend?: boolean;
  crosshair_sync_group?: string;
};

export type ChartKind =
  | "candlestick"
  | "line"
  | "area"
  | "baseline"
  | "histogram"
  | "bar"
  | "multi";

export type ChartPath = "inline" | "bulk";

export type ChartBlockShape = {
  kind: "chart";
  version?: string;
  chart_id: string;
  chart_kind: ChartKind;
  title: string;
  subtitle?: string;
  series: ChartSeries[];
  overlays?: ChartOverlay[];
  panes?: ChartPane[];
  time?: ChartTime;
  default_range?: { from: number | string; to: number | string };
  source: ChartSource;
  caption?: string;
  insights?: string[];
  warnings?: string[];
  ui?: ChartUI;
  path: ChartPath;
  bulk_data_uri?: string;
  ts?: number;
  // Permissive — different providers may carry extra metadata.
  [key: string]: unknown;
};

/**
 * True when every series carries inline data. We can render
 * synchronously without fetching artifacts.
 */
export function isFullyInline(block: ChartBlockShape): boolean {
  if (!block.series || block.series.length === 0) return false;
  if (block.bulk_data_uri) return false;
  return block.series.every((s) => Array.isArray(s.data) && s.data.length > 0);
}

/**
 * Find the artifact id from a ``nerya://chart/<id>`` URI or a plain id
 * string. Returns null for malformed input so callers can degrade.
 */
export function chartIdFromUri(uri: string | undefined): string | null {
  if (!uri) return null;
  const trimmed = uri.trim();
  if (!trimmed) return null;
  // ``nerya://chart/<id>`` or ``nerya://chart/<id>#series/<name>``
  const match = trimmed.match(/^nerya:\/\/chart\/([A-Za-z0-9._-]+)/);
  if (match) return match[1];
  // Bare chart_id, e.g. "markets.get_quote.abc123"
  if (/^[A-Za-z0-9._-]+$/.test(trimmed)) return trimmed;
  return null;
}

/**
 * Best-effort runtime guard. Used by the chat block dispatcher to
 * decide whether a generic ``NativeBlock`` envelope is in fact a
 * chart block we can render. Stays permissive so renderers never
 * crash a turn — invalid blocks fall through to the generic JSON
 * collapsible.
 */
export function isChartBlockShape(value: unknown): value is ChartBlockShape {
  if (!value || typeof value !== "object") return false;
  const obj = value as Record<string, unknown>;
  if (obj.kind !== "chart") return false;
  if (typeof obj.chart_id !== "string") return false;
  if (typeof obj.title !== "string") return false;
  if (!Array.isArray(obj.series)) return false;
  if (obj.path !== "inline" && obj.path !== "bulk") return false;
  return true;
}
