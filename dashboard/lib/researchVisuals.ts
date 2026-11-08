import { liveEventsToBlocks, type ChatThread } from "./chat";
import { isChartBlockShape, type ChartBlockShape } from "./chartBlock";
import { projectMessageBlocks } from "./streamTranscript";

export type ResearchNews = { title: string; url: string; source: string; published_at: string; summary?: string };
export type ResearchInstrument = {
  id: string; market: string; venue: string; name: string; interval: string;
  chartIds: string[]; news: ResearchNews[]; newsAsOf: string; newsStatus: string; seenAt: number;
};
export type ResearchVisuals = { instruments: ResearchInstrument[]; charts: ChartBlockShape[]; studies: ChartBlockShape[] };
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown, max = 240) => typeof value === "string" ? value.trim().slice(0, max) : "";

export function safeResearchUrl(value: unknown): string {
  try {
    const url = new URL(text(value, 2048));
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}
export function researchNews(value: unknown, limit = 12): ResearchNews[] {
  if (!Array.isArray(value)) return [];
  const byUrl = new Map<string, ResearchNews>();
  for (const raw of value.slice(0, 100)) {
    const row = object(raw), title = text(row.title), url = safeResearchUrl(row.url || row.link);
    if (!title || !url) continue;
    const date = text(row.published_at || row.published || row.date);
    const published = Number.isFinite(Date.parse(date)) ? new Date(date).toISOString() : "";
    byUrl.set(url, { title, url, source: text(row.source || row.publisher) || new URL(url).hostname,
      published_at: published, summary: text(row.summary, 600) || undefined });
  }
  return [...byUrl.values()].sort((a, b) => (Date.parse(b.published_at) || 0) - (Date.parse(a.published_at) || 0)).slice(0, limit);
}
export function instrumentId(market: string, venue = ""): string {
  const provider = venue.toLowerCase() || (market.includes(":") && /^[a-z0-9_-]+$/i.test(market.split(":")[0]) ? market.split(":")[0].toLowerCase() : "");
  const symbol = provider && market.toLowerCase().startsWith(provider + ":") ? market.slice(provider.length + 1) : market;
  return `${provider}:${symbol.toUpperCase()}`;
}
export const isMarketChart = (block: ChartBlockShape) => block.series.some(series => series?.type === "candlestick");

/** Structured tool output only. Never infer assets from assistant prose. */
export function collectResearchVisuals(thread: Pick<ChatThread, "messages"> | null | undefined): ResearchVisuals {
  const charts = new Map<string, ChartBlockShape>(), instruments = new Map<string, ResearchInstrument>();
  function addInstrument(value: unknown, seenAt: number, chartId = "") {
    const row = object(value), market = text(row.market || row.symbol, 160);
    if (!market || /[\r\n<>]/.test(market)) return;
    const venue = text(row.venue, 64).toLowerCase() || (market.includes(":") ? market.split(":")[0].toLowerCase() : "");
    const id = instrumentId(market, venue), old = instruments.get(id);
    instruments.set(id, {
      id, market, venue, name: text(row.name) && text(row.name) !== market ? text(row.name) : old?.name || market,
      interval: text(row.interval, 16) || old?.interval || "1h",
      chartIds: [...new Set([...(old?.chartIds || []), ...(chartId ? [chartId] : []), ...(typeof row.chart_id === "string" ? [row.chart_id] : [])])],
      news: researchNews([...(old?.news || []), ...researchNews(row.news)]),
      newsAsOf: text(row.news_as_of) || old?.newsAsOf || "",
      newsStatus: text(row.news_status) || old?.newsStatus || "not_requested", seenAt,
    });
  }
  function visit(value: unknown, seenAt: number, depth = 0, seen = new Set<unknown>()) {
    if (depth > 7 || value == null || seen.has(value)) return;
    if (typeof value === "string") {
      if (value.length > 1_000_000 || !/chart_blocks|chart_block|research_context/.test(value)) return;
      const start = value.indexOf("{"), end = value.lastIndexOf("}");
      if (start < 0 || end <= start) return;
      try { visit(JSON.parse(value.slice(start, end + 1)), seenAt, depth + 1, seen); return; } catch { /* Native summaries may precede the complete JSON part. */ }
      // ToolResult.text() appends one complete JSON line after the bounded
      // stdout/stderr summary. A partial stdout tail must not hide that part.
      const lines = value.split("\n");
      for (let i = lines.length - 1, attempts = 0; i >= 0 && attempts < 16; i -= 1) {
        const line = lines[i].trim();
        if (!line.startsWith("{")) continue;
        attempts += 1;
        try { visit(JSON.parse(line), seenAt, depth + 1, seen); return; } catch { /* Never evaluate partial or non-JSON text. */ }
      }
      return;
    }
    if (typeof value !== "object") return;
    seen.add(value);
    if (Array.isArray(value)) { value.slice(0, 100).forEach(row => visit(row, seenAt, depth + 1, seen)); return; }
    const row = object(value);
    if (row.kind === 'tool_use' || row.ok === false || row.is_error === true || (row.kind === "tool_result" && row.error)) return;
    const context = object(row.research_context);
    if (context.version === 1 && Array.isArray(context.instruments)) context.instruments.slice(0, 40).forEach(item => addInstrument(item, seenAt));
    if (isChartBlockShape(value)) {
      const old = charts.get(value.chart_id);
      // Marker envelopes are intentionally minimal. Do not let their later
      // arrival overwrite the richer, immutable descriptor from stdout_json.
      if (!old || (!old.research_context && value.research_context) || (!old.instrument && value.instrument)) charts.set(value.chart_id, value);
      if (isMarketChart(value)) {
        const explicit = object(value.instrument);
        if (explicit.market || explicit.symbol) addInstrument(explicit, seenAt, value.chart_id);
        else if (value.market || (value.source?.skill === "markets" && (/^[A-Za-z0-9_-]+:\S+$/.test(value.title.split("·")[0].trim()) || value.venue || value.subtitle?.match(/venue:\s*[\w-]+/i)))) {
          addInstrument({ market: value.market || value.title.split("·")[0].trim(), venue: value.venue || value.subtitle?.match(/venue:\s*([\w:-]+)/i)?.[1], interval: value.interval || value.title.split("·").map(s => s.trim()).find(s => /^\d+[mhdw]$/.test(s)) }, seenAt, value.chart_id);
        }
      }
      return;
    }
    for (const key of ["block", "chart_blocks", "chart_block", "result", "output", "stdout_json", "notes", "stdout", "data", "payload", "content"]) if (row[key] != null) visit(row[key], seenAt, depth + 1, seen);
  }
  for (const message of thread?.messages || []) {
    if (message.role !== "assistant") continue;
    for (const block of projectMessageBlocks(message)) visit(block, message.ts);
    for (const call of message.turn?.tool_trace || []) if (call.ok !== false && !call.error) visit(call.result, message.ts);
    for (const call of message.turn?.actions || []) if (!call.error) visit(call.result, message.ts);
  }
  const list = [...charts.values()];
  const marketIds = new Set([...instruments.values()].flatMap(item => item.chartIds));
  return { instruments: [...instruments.values()], charts: list, studies: list.filter(block => !marketIds.has(block.chart_id)) };
}

/** A bounded feed match is not a claim that all news has been searched. */
export function matchingResearchNews(value: unknown, instrument: ResearchInstrument): ResearchNews[] {
  const symbol = instrument.venue && instrument.market.toLowerCase().startsWith(instrument.venue + ":") ? instrument.market.slice(instrument.venue.length + 1) : instrument.market;
  const base = symbol.split(/[/:\-]/)[0].replace(/(USDT|USDC|BUSD|USD|PERP)$/i, "");
  const aliases: Record<string, string[]> = { BTC: ["bitcoin", "比特币"], ETH: ["ethereum", "以太坊"], SOL: ["solana"] };
  const terms = [base, ...(aliases[base.toUpperCase()] || []), ...(instrument.name !== instrument.market ? [instrument.name] : [])].filter(s => s.length >= 2);
  return researchNews(value, 100).filter(row => terms.some(term => {
    const words = `${row.title} ${row.summary || ""}`.toLowerCase();
    return /^[a-z0-9]+$/i.test(term) ? words.split(/[^a-z0-9]+/).includes(term.toLowerCase()) : words.includes(term.toLowerCase());
  })).slice(0, 12);
}
