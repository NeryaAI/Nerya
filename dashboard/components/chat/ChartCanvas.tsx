"use client";

import dynamic from "next/dynamic";
import type { ChartCanvasInnerProps } from "./ChartCanvasInner";
import { ChartPlaceholder } from "./ChartPlaceholder";

// lightweight-charts touches ``document`` and ``ResizeObserver`` at
// module import time, so we pin the canvas to client-only via
// ``next/dynamic`` with ``ssr: false``. SSR-safe placeholder is
// rendered while the bundle streams in.
const ChartCanvasInner = dynamic<ChartCanvasInnerProps>(
  () => import("./ChartCanvasInner"),
  {
    ssr: false,
    loading: () => (
      <ChartPlaceholder title="Chart" subtitle="Loading renderer…" tone="loading" />
    ),
  }
);

export function ChartCanvas(props: ChartCanvasInnerProps) {
  return <ChartCanvasInner {...props} />;
}
