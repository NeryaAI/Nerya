"use client";

import { useState } from "react";
import {
  type ChartBlockShape,
  isChartBlockShape,
} from "../../lib/chartBlock";
import { useChartData } from "../../lib/useChartData";
import { ChartCanvas } from "./ChartCanvas";
import { ChartPlaceholder } from "./ChartPlaceholder";
import { ChartIcon, ChevronDownIcon, ChevronRightIcon } from "../icons";

// ---------------------------------------------------------------------------
// Top-level chart block card.
// ---------------------------------------------------------------------------
//
// Layout: [icon] title  ·  source  ·  path-badge
//         [canvas height ~240px]
//         optional insights bullet list
//         optional warnings (if composer auto-promoted to bulk, etc.)
//
// The inline renderer is live today. The bulk branch surfaces an
// informative placeholder ("data is being fetched") and wires up to
// ``useChartData`` once the ``GET /charts/<id>`` endpoint is
// live. Until then, a bulk block stays visible with its insights and
// metadata so the agent's reasoning remains intact.

export function ChartBlock({ block }: { block: unknown }) {
  if (!isChartBlockShape(block)) {
    // Defensive: if the envelope was malformed we never crash a turn.
    // The generic block fallback in TurnBlocks will show the JSON.
    return null;
  }
  return <ChartBlockCard block={block} />;
}

function ChartBlockCard({ block: rawBlock }: { block: ChartBlockShape }) {
  const [showInsights, setShowInsights] = useState(true);
  const { block, loading, error, ready } = useChartData(rawBlock);
  const insights = block.insights ?? [];
  const warnings = block.warnings ?? [];
  const height = block.ui?.height ?? 240;

  let canvas: JSX.Element;
  if (ready) {
    canvas = <ChartCanvas block={block} height={height} />;
  } else if (loading) {
    canvas = (
      <ChartPlaceholder
        title={block.title || "Chart"}
        subtitle="Loading bulk artifact…"
        tone="loading"
        height={height}
      />
    );
  } else if (error) {
    canvas = (
      <ChartPlaceholder
        title={block.title || "Chart"}
        subtitle={`Failed to load: ${error}`}
        tone="error"
        height={height}
      />
    );
  } else {
    canvas = (
      <ChartPlaceholder
        title={block.title || "Chart"}
        subtitle="No data attached"
        tone="empty"
        height={height}
      />
    );
  }

  return (
    <div className="rounded-md border border-ink-700/70 bg-ink-800/40 px-3 py-2.5 space-y-2">
      <ChartHeader block={block} />
      {canvas}
      {warnings.length > 0 ? (
        <ul className="text-[11px] text-amber-300/80 space-y-0.5">
          {warnings.map((w, i) => (
            <li key={i}>· {w}</li>
          ))}
        </ul>
      ) : null}
      {insights.length > 0 ? (
        <div>
          <button
            type="button"
            onClick={() => setShowInsights((v) => !v)}
            className="flex items-center gap-1 text-[12px] text-ink-400 hover:text-white transition-colors"
          >
            {showInsights ? (
              <ChevronDownIcon size={10} />
            ) : (
              <ChevronRightIcon size={10} />
            )}
            <span>insights ({insights.length})</span>
          </button>
          {showInsights ? (
            <ul className="mt-1 space-y-0.5 text-[12px] text-ink-200">
              {insights.map((line, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-ink-500">·</span>
                  <span>{line}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ChartHeader({ block }: { block: ChartBlockShape }) {
  const skill = block.source?.skill || "";
  const action = block.source?.action || "";
  const sourceLabel = skill && action ? `${skill}.${action}` : skill || action;
  const pathTone =
    block.path === "bulk"
      ? "border-fluid-500/30 bg-fluid-500/[0.08] text-fluid-200"
      : "border-emerald-500/30 bg-emerald-500/[0.06] text-emerald-200";
  return (
    <div className="flex items-center justify-between gap-3 text-[11px]">
      <div className="flex items-center gap-2 min-w-0">
        <ChartIcon className="text-ink-400 shrink-0" size={14} />
        <div className="truncate">
          <span className="text-white font-medium">{block.title}</span>
          {block.subtitle ? (
            <span className="text-ink-400"> · {block.subtitle}</span>
          ) : null}
        </div>
      </div>
      <div className="flex items-center gap-2 text-[12px] shrink-0">
        {sourceLabel ? (
          <span className="font-mono text-ink-400">{sourceLabel}</span>
        ) : null}
        <span
          className={`rounded-md border px-1.5 py-0.5 ${pathTone}`}
          title={
            block.path === "bulk"
              ? "Data lives in workspace artifacts; fetched lazily."
              : "Data inlined in the turn envelope."
          }
        >
          {block.path}
        </span>
      </div>
    </div>
  );
}
