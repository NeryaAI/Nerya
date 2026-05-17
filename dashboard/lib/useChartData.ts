// useChartData — bridges the inline / bulk paths into a single
// "ready to render" ChartBlockShape.
//
// * inline blocks (every series carries `data`) resolve synchronously.
// * bulk blocks (envelope carries `bulk_data_uri` or any series carries
//   `data_uri`) trigger a single HTTP fetch through
//   `/api/proxy/charts/get?id=<chart_id>` and merge the resulting
//   payload back into the block.
//
// We intentionally don't pull in SWR or react-query for this — the
// dashboard ships without them, and a chart artifact is immutable
// (the chart_id encodes its content), so an in-component state machine
// + a module-level cache is enough to avoid duplicate fetches across
// re-renders and across multiple ChartBlock instances pointing at the
// same artifact.

"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  type ChartBlockShape,
  type ChartSeries,
  type ChartSeriesPoint,
  chartIdFromUri,
  isFullyInline,
} from "./chartBlock";

type CachedPayload = {
  chart_id: string;
  title?: string;
  as_of?: string;
  series: { name: string; type: string; data: ChartSeriesPoint[] }[];
};

type FetchState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; payload: CachedPayload }
  | { status: "error"; error: string };

const CACHE = new Map<string, FetchState>();
const INFLIGHT = new Map<string, Promise<CachedPayload>>();

function endpointFor(chartId: string): string {
  return `/api/proxy/charts/get?id=${encodeURIComponent(chartId)}`;
}

async function fetchChartPayload(chartId: string): Promise<CachedPayload> {
  const existing = INFLIGHT.get(chartId);
  if (existing) return existing;
  const promise = (async () => {
    const res = await fetch(endpointFor(chartId), {
      method: "GET",
      credentials: "same-origin",
      headers: { accept: "application/json" },
    });
    if (!res.ok) {
      throw new Error(`chart fetch failed: HTTP ${res.status}`);
    }
    const body = await res.json();
    if (!body || body.ok !== true) {
      const reason = body?.error || "unknown_error";
      throw new Error(`chart fetch rejected: ${reason}`);
    }
    const payload = body.payload as CachedPayload;
    if (!payload || !Array.isArray(payload.series)) {
      throw new Error("chart fetch payload missing series array");
    }
    return payload;
  })();
  INFLIGHT.set(chartId, promise);
  try {
    const result = await promise;
    return result;
  } finally {
    INFLIGHT.delete(chartId);
  }
}

function mergePayload(
  block: ChartBlockShape,
  payload: CachedPayload
): ChartBlockShape {
  // Match by series name. Series the artifact doesn't know about keep
  // their original (possibly inline) data so a hybrid block — some
  // bulk, some inline — round-trips cleanly.
  const byName = new Map(payload.series.map((s) => [s.name, s.data]));
  const merged: ChartSeries[] = block.series.map((series) => {
    const fresh = byName.get(series.name);
    if (!fresh) return series;
    return { ...series, data: fresh, data_uri: undefined };
  });
  return { ...block, series: merged };
}

export type ChartDataResult = {
  /** Block ready for rendering (inline series populated). */
  block: ChartBlockShape;
  /** True while we're fetching the bulk artifact. */
  loading: boolean;
  /** Error message from the last fetch attempt. */
  error: string | null;
  /** True when the rendered block has every series' data resolved. */
  ready: boolean;
};

export function useChartData(input: ChartBlockShape): ChartDataResult {
  // We want stable state across re-renders for the same chart_id. The
  // cache + in-flight maps ensure a single network round-trip even
  // when the same chart is rendered in multiple places.
  const chartId = useMemo(() => {
    if (input.path !== "bulk") return null;
    if (isFullyInline(input)) return null;
    const fromTop = chartIdFromUri(input.bulk_data_uri);
    if (fromTop) return fromTop;
    for (const series of input.series) {
      const id = chartIdFromUri(series.data_uri);
      if (id) return id;
    }
    return null;
  }, [input]);

  const [state, setState] = useState<FetchState>(() => {
    if (!chartId) return { status: "idle" };
    return CACHE.get(chartId) ?? { status: "idle" };
  });

  // Keep latest state in sync with the cache when chart_id flips
  // between renders (rare but possible if a parent rebuilds blocks).
  const lastChartId = useRef<string | null>(chartId);
  useEffect(() => {
    if (lastChartId.current === chartId) return;
    lastChartId.current = chartId;
    if (!chartId) {
      setState({ status: "idle" });
      return;
    }
    setState(CACHE.get(chartId) ?? { status: "idle" });
  }, [chartId]);

  useEffect(() => {
    if (!chartId) return;
    const cached = CACHE.get(chartId);
    if (cached && cached.status === "ready") return; // nothing to do
    let cancelled = false;
    setState({ status: "loading" });
    CACHE.set(chartId, { status: "loading" });
    fetchChartPayload(chartId)
      .then((payload) => {
        const next: FetchState = { status: "ready", payload };
        CACHE.set(chartId, next);
        if (!cancelled) setState(next);
      })
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        const next: FetchState = { status: "error", error: message };
        CACHE.set(chartId, next);
        if (!cancelled) setState(next);
      });
    return () => {
      cancelled = true;
    };
  }, [chartId]);

  if (!chartId) {
    return {
      block: input,
      loading: false,
      error: null,
      ready: isFullyInline(input),
    };
  }

  if (state.status === "ready") {
    const merged = mergePayload(input, state.payload);
    return {
      block: merged,
      loading: false,
      error: null,
      ready: isFullyInline(merged),
    };
  }
  if (state.status === "error") {
    return { block: input, loading: false, error: state.error, ready: false };
  }
  return { block: input, loading: true, error: null, ready: false };
}

/**
 * Test-only — clears the in-memory cache so dev fixtures and unit
 * tests can re-fetch artifacts after artifacts on disk change.
 */
export function __resetChartDataCache(): void {
  CACHE.clear();
  INFLIGHT.clear();
}
