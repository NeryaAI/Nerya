"use client";

// Dev-only verification page for the new ``chart`` NativeBlock kind.
// Renders fixture ChartBlock envelopes through the same TurnBlocks
// dispatcher real chat sessions use, so we exercise the full pipeline
// (envelope → unwrap → kind switch → ChartBlock card → ChartCanvas →
// lightweight-charts) without needing the Nerya daemon running.
//
// We also stub ``window.fetch`` for ``/api/proxy/charts/get`` so the
// bulk fixture exercises ``useChartData`` end-to-end without spinning
// up the backend. This lets us validate that the loading → ready
// transition paints correctly.

import { useEffect, useMemo, useState } from "react";
import { NativeBlocksTrack } from "../../../components/chat/TurnBlocks";
import type { ChartBlockShape, OHLCV } from "../../../lib/chartBlock";
import type { NativeBlockEnvelope } from "../../../lib/chat";
import { __resetChartDataCache } from "../../../lib/useChartData";

function makeEnvelope(block: ChartBlockShape, seq: number): NativeBlockEnvelope {
  return {
    seq,
    turn_id: "dev_demo",
    message_id: "dev_demo_msg",
    role: "assistant",
    ts: new Date().toISOString(),
    block: block as unknown as Record<string, unknown>,
    kind: "chart",
  };
}

function generateCandles(n: number): ChartBlockShape["series"][number]["data"] {
  const out: { time: number; open: number; high: number; low: number; close: number; volume?: number }[] = [];
  let price = 100;
  let t = Math.floor(Date.now() / 1000) - n * 86400;
  for (let i = 0; i < n; i += 1) {
    const drift = (Math.sin(i / 5) + Math.cos(i / 11)) * 1.5;
    const open = price;
    const close = price + drift + (Math.random() - 0.5) * 1.2;
    const high = Math.max(open, close) + Math.random() * 1.0;
    const low = Math.min(open, close) - Math.random() * 1.0;
    out.push({
      time: t,
      open: +open.toFixed(2),
      high: +high.toFixed(2),
      low: +low.toFixed(2),
      close: +close.toFixed(2),
      volume: Math.round(1000 + Math.random() * 500),
    });
    price = close;
    t += 86400;
  }
  return out;
}

function generateLine(n: number): ChartBlockShape["series"][number]["data"] {
  const out: { time: number; value: number }[] = [];
  let v = 1.0;
  let t = Math.floor(Date.now() / 1000) - n * 3600;
  for (let i = 0; i < n; i += 1) {
    v += (Math.random() - 0.45) * 0.04;
    out.push({ time: t, value: +v.toFixed(4) });
    t += 3600;
  }
  return out;
}

export default function ChartDemoPage() {
  const envelopes = useMemo<NativeBlockEnvelope[]>(() => {
    const candleBlock: ChartBlockShape = {
      kind: "chart",
      version: "v1",
      chart_id: "demo.candle.001",
      chart_kind: "candlestick",
      title: "BTCUSD · 1D · 60 days (synthetic)",
      subtitle: "inline demo",
      series: [
        { type: "candlestick", name: "ohlc", data: generateCandles(60) },
      ],
      time: { timezone: "UTC", format: "unix_seconds" },
      source: { skill: "demo", action: "candle", as_of: new Date().toISOString() },
      insights: [
        "Synthetic walk used to verify renderer.",
        "Open/High/Low/Close drawn from a sin+cos drift with jitter.",
      ],
      path: "inline",
    };

    const lineBlock: ChartBlockShape = {
      kind: "chart",
      version: "v1",
      chart_id: "demo.line.001",
      chart_kind: "line",
      title: "Synthetic indicator · hourly",
      series: [
        {
          type: "line",
          name: "indicator",
          data: generateLine(96),
          color: "#6b8cff",
          line_width: 2,
        },
      ],
      overlays: [
        {
          type: "price_line",
          price: 1.0,
          color: "#fbbf24",
          line_style: "dashed",
          title: "baseline",
          axis_label: true,
        },
      ],
      time: { timezone: "UTC", format: "unix_seconds" },
      source: { skill: "demo", action: "line", as_of: new Date().toISOString() },
      insights: ["Includes a dashed baseline at 1.0."],
      path: "inline",
    };

    const tinyBlock: ChartBlockShape = {
      kind: "chart",
      version: "v1",
      chart_id: "demo.tiny.001",
      chart_kind: "line",
      title: "Last 7 days · daily return",
      series: [
        {
          type: "line",
          name: "ret",
          color: "#10b981",
          data: [
            { time: Math.floor(Date.now() / 1000) - 6 * 86400, value: 0.012 },
            { time: Math.floor(Date.now() / 1000) - 5 * 86400, value: -0.004 },
            { time: Math.floor(Date.now() / 1000) - 4 * 86400, value: 0.018 },
            { time: Math.floor(Date.now() / 1000) - 3 * 86400, value: 0.006 },
            { time: Math.floor(Date.now() / 1000) - 2 * 86400, value: -0.011 },
            { time: Math.floor(Date.now() / 1000) - 1 * 86400, value: 0.022 },
            { time: Math.floor(Date.now() / 1000), value: 0.009 },
          ],
        },
      ],
      time: { timezone: "UTC", format: "unix_seconds" },
      source: { skill: "agent", action: "inline_summary", as_of: new Date().toISOString() },
      insights: ["7d sum: +5.2%", "Best: +2.2% on day -1"],
      path: "inline",
      ui: { height: 160 },
    };

    const bulkBlock: ChartBlockShape = {
      kind: "chart",
      version: "v1",
      chart_id: "demo.bulk.synthetic",
      chart_kind: "candlestick",
      title: "BTCUSD · 1D · 60 days (bulk)",
      subtitle: "bulk via stubbed fetcher",
      series: [{ type: "candlestick", name: "ohlc", data_uri: "nerya://chart/demo.bulk.synthetic#series/ohlc" }],
      time: { timezone: "UTC", format: "unix_seconds" },
      source: { skill: "markets", action: "get_quote", as_of: new Date().toISOString() },
      insights: ["Fetched lazily via /api/proxy/charts/get?id=…", "Stub adds a 250ms delay so you can see the loading skeleton."],
      path: "bulk",
      bulk_data_uri: "nerya://chart/demo.bulk.synthetic",
    };

    // ---- Kernel-injected scenario --------------------------------
    // Mirrors what ``AgentKernel._splice_chart_blocks`` produces in
    // a real turn: a ``run_shell`` tool_use → tool_result → chart
    // envelope right after the result, then the assistant's text
    // wrap-up. This is the *exact* layout the chat will render once
    // Once the full chart splice path is live, this demo doubles as visual
    // regression coverage for the splice ordering.
    const kernelToolUse: NativeBlockEnvelope = {
      seq: 5,
      turn_id: "dev_demo",
      message_id: "dev_demo_msg",
      role: "assistant",
      ts: new Date().toISOString(),
      block: {
        kind: "tool_use",
        call_id: "call-kernel-demo",
        skill_id: "native",
        action: "run_shell",
        payload: {
          command:
            "python -m nerya.skills.builtin.markets.scripts.get_candles --json '{\"market\":\"binance:BTC/USDT\",\"interval\":\"1d\",\"limit\":60,\"path\":\"bulk\"}'",
          description: "BTC/USDT 60d K-line",
        },
      },
      kind: "tool_use",
    };
    const kernelToolResult: NativeBlockEnvelope = {
      seq: 6,
      turn_id: "dev_demo",
      message_id: "dev_demo_msg",
      role: "tool",
      ts: new Date().toISOString(),
      block: {
        kind: "tool_result",
        call_id: "call-kernel-demo",
        skill_id: "native",
        action: "run_shell",
        ok: true,
        elapsed_ms: 412,
        result: "$ python -m nerya.skills.builtin.markets.scripts.get_candles ...\n[exit=0, took 412ms]\n\n## stdout\n{\"market\":\"binance:BTC/USDT\",\"chart_blocks\":[{...}]}\n",
      },
      kind: "tool_result",
    };
    const kernelChartBlock: ChartBlockShape = {
      kind: "chart",
      version: "v1",
      chart_id: "demo.kernel.injected",
      chart_kind: "candlestick",
      title: "binance:BTC/USDT · 1D · 60 bars",
      subtitle: "venue: binance · kernel-spliced",
      series: [
        {
          type: "candlestick",
          name: "ohlc",
          data_uri: "nerya://chart/demo.kernel.injected#series/ohlc",
        },
      ],
      time: { timezone: "UTC", format: "unix_seconds" },
      source: {
        skill: "markets",
        action: "get_candles",
        as_of: new Date().toISOString(),
      },
      insights: [
        "Kernel hook extracted chart_blocks from run_shell stdout.",
        "Spliced as a kind='chart' envelope right after the tool_result.",
      ],
      path: "bulk",
      bulk_data_uri: "nerya://chart/demo.kernel.injected",
    };
    const kernelChartEnvelope: NativeBlockEnvelope = {
      seq: 7,
      turn_id: "dev_demo",
      message_id: "dev_demo_msg",
      role: "tool",
      ts: new Date().toISOString(),
      block: {
        ...(kernelChartBlock as unknown as Record<string, unknown>),
        call_id: "call-kernel-demo",
      },
      kind: "chart",
    };

    return [
      makeEnvelope(candleBlock, 1),
      makeEnvelope(lineBlock, 2),
      makeEnvelope(tinyBlock, 3),
      makeEnvelope(bulkBlock, 4),
      kernelToolUse,
      kernelToolResult,
      kernelChartEnvelope,
    ];
  }, []);

  // Stub the bulk fetcher so the demo works without the daemon. We
  // intercept ``window.fetch`` for /api/proxy/charts/get and respond
  // with synthesised OHLCV. The 250ms delay shows the loading skeleton
  // before the canvas paints.
  const [stubbed, setStubbed] = useState(false);
  useEffect(() => {
    const original = window.fetch;
    const stubData: OHLCV[] = (() => {
      const out: OHLCV[] = [];
      let price = 28000;
      let t = Math.floor(Date.now() / 1000) - 60 * 86400;
      for (let i = 0; i < 60; i += 1) {
        const drift = (Math.sin(i / 7) + Math.cos(i / 13)) * 320;
        const open = price;
        const close = price + drift + (Math.random() - 0.5) * 220;
        const high = Math.max(open, close) + Math.random() * 180;
        const low = Math.min(open, close) - Math.random() * 180;
        out.push({
          time: t,
          open: +open.toFixed(2),
          high: +high.toFixed(2),
          low: +low.toFixed(2),
          close: +close.toFixed(2),
        });
        price = close;
        t += 86400;
      }
      return out;
    })();

    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
          ? input.toString()
          : input.url;
      if (url.includes("/api/proxy/charts/get")) {
        await new Promise((r) => setTimeout(r, 250));
        // Pull the requested id out of the query so we can route the
        // kernel-injected fixture to a slightly different stub
        // dataset; this gives the demo two distinct bulk charts that
        // both render via /api/proxy/charts/get without colliding in
        // the in-memory cache.
        let chartId = "demo.bulk.synthetic";
        try {
          const u = new URL(url, "http://x");
          chartId = u.searchParams.get("id") || chartId;
        } catch {
          /* keep default */
        }
        const title =
          chartId === "demo.kernel.injected"
            ? "binance:BTC/USDT · 1D · 60 bars"
            : "BTCUSD · 1D · 60 days (bulk)";
        return new Response(
          JSON.stringify({
            ok: true,
            chart_id: chartId,
            payload: {
              chart_id: chartId,
              title,
              series: [{ name: "ohlc", type: "candlestick", data: stubData }],
              as_of: new Date().toISOString(),
            },
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
      return original(input, init);
    }) as typeof fetch;

    __resetChartDataCache();
    setStubbed(true);
    return () => {
      window.fetch = original;
      __resetChartDataCache();
    };
  }, []);

  return (
    <div className="min-h-screen bg-ink-900 text-ink-100">
      <div className="mx-auto max-w-3xl px-6 py-10 space-y-4">
        <header className="space-y-1">
          <div className="text-[11px] uppercase tracking-wider text-ink-400">
            dev fixture
          </div>
          <h1 className="text-xl font-semibold">ChartBlock renderer demo</h1>
          <p className="text-sm text-ink-400">
            Renders the new <code>chart</code> NativeBlock kind through the
            real <code>NativeBlocksTrack</code> dispatcher. Inline blocks
            paint immediately; the bulk block exercises{" "}
            <code>useChartData</code> via a stubbed{" "}
            <code>/api/proxy/charts/get</code> response so it runs without
            the daemon.
          </p>
        </header>
        {stubbed ? (
          <NativeBlocksTrack envelopes={envelopes} live={false} />
        ) : (
          <div className="text-sm text-ink-400">Initialising stubbed fetcher…</div>
        )}
      </div>
    </div>
  );
}
