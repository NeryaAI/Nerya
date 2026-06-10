"use client";

import { useState } from "react";
import type { NativeBlock } from "../../../lib/chat";
import { Tag } from "./atoms";
import { recordOf } from "./helpers";

function parseEmbeddedJson(text: string): Record<string, unknown> {
  const marker = "[compacted_kept]";
  const markerIdx = text.indexOf(marker);
  const source = markerIdx >= 0 ? text.slice(markerIdx + marker.length).trim() : text;
  const jsonStart = source.search(/[{\[]/);
  if (jsonStart < 0) return {};
  try {
    return recordOf(JSON.parse(source.slice(jsonStart).trim()));
  } catch {
    return {};
  }
}

function parseFetchSummary(text: string): Record<string, unknown> {
  const match = text.match(/^web_fetch:\s*(.*?)\s*\((\d+)\s+chars\)/);
  if (!match) return {};
  const title = match[1].trim();
  const charCount = Number(match[2]);
  const out: Record<string, unknown> = {
    title,
    count: Number.isFinite(charCount) && charCount > 0 ? 1 : 0,
  };
  if (Number.isFinite(charCount) && charCount > 0) {
    out.content = text;
  }
  return out;
}

function parseJsonRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const rec = value as Record<string, unknown>;
    const data = rec.data;
    if (data && typeof data === "object" && !Array.isArray(data)) {
      return { ...rec, ...(data as Record<string, unknown>) };
    }
    const content = rec.content;
    if (Array.isArray(content)) {
      for (const part of content) {
        const row = recordOf(part);
        const partData = row.data;
        if (partData && typeof partData === "object" && !Array.isArray(partData)) {
          return { ...rec, ...(partData as Record<string, unknown>) };
        }
        const text = typeof row.text === "string" ? row.text.trim() : "";
        if (text.startsWith("{") || text.startsWith("[")) {
          const parsed = parseJsonRecord(text);
          if (Object.keys(parsed).length) return { ...rec, ...parsed };
        }
      }
    }
    if (typeof content === "string") {
      const parsed = parseJsonRecord(content);
      if (Object.keys(parsed).length) return { ...rec, ...parsed };
    }
    return rec;
  }
  if (typeof value === "string") {
    const text = value.trim();
    if (!text) return {};
    try {
      const parsed = JSON.parse(text);
      return recordOf(parsed);
    } catch {
      const summary = parseFetchSummary(text);
      const embedded = parseEmbeddedJson(text);
      if (Object.keys(embedded).length) {
        const merged: Record<string, unknown> = { ...summary };
        for (const [key, item] of Object.entries(embedded)) {
          if (item !== "" && item !== null && item !== undefined) {
            merged[key] = item;
          } else if (!(key in merged)) {
            merged[key] = item;
          }
        }
        const snippet = firstText(
          embedded.snippet,
          embedded.markdown,
          embedded.text,
          embedded.body,
        );
        if (snippet) {
          merged.content = snippet;
        } else if (merged.count === 0) {
          delete merged.content;
        }
        return merged;
      }
      return summary;
    }
  }
  return {};
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value;
  }
  return "";
}

function previewText(value: unknown, max = 320): string {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text) return "";
  const squashed = text.replace(/\s+/g, " ");
  return squashed.length > max ? `${squashed.slice(0, max)}...` : squashed;
}

function webSearchResults(block: NativeBlock): Array<{
  title: string;
  url: string;
  snippet: string;
  source?: string;
}> {
  const result = parseJsonRecord(block.result);
  const nestedSearch = recordOf(result.search);
  const candidates: unknown[] = [
    result.results,
    result.search_results,
    (result as Record<string, unknown>).hits,
    result.documents,
    nestedSearch.results,
  ];
  for (const c of candidates) {
    if (Array.isArray(c) && c.length) {
      return c.slice(0, 30).map((row) => {
        const r = recordOf(row);
        return {
          title: String(r.title || r.name || r.url || ""),
          url: String(r.url || r.link || r.href || ""),
          snippet: firstText(
            r.snippet,
            r.body,
            r.description,
            previewText(r.markdown || r.text),
          ),
          source: typeof r.source === "string" ? r.source : undefined,
        };
      });
    }
  }
  return [];
}

function resultCount(result: Record<string, unknown>, rows: unknown[], content: string): number {
  const count = result.count;
  if (typeof count === "number" && Number.isFinite(count)) return count;
  if (typeof count === "string" && count.trim() && !Number.isNaN(Number(count))) {
    return Number(count);
  }
  if (rows.length) return rows.length;
  return content.trim() ? 1 : 0;
}

export function WebCard({
  block,
  variant,
  pending = false,
}: {
  block: NativeBlock;
  variant: "use" | "result";
  pending?: boolean;
}) {
  const action = String(block.action || "").toLowerCase();
  const payload = recordOf(block.payload);
  const result = parseJsonRecord(block.result);
  const nestedSearch = recordOf(result.search);
  const isResult = variant === "result";
  const ok = block.ok !== false && !block.error && result.ok !== false;
  const isFetch = action === "web_fetch" || action === "web_search_fetch";
  const isSearch = action === "web_search" || action === "web_search_fetch";
  const allResults = webSearchResults(block);
  const results = isSearch ? allResults : [];
  const firstResult = allResults[0];
  const url = String(payload.url || result.url || firstResult?.url || "");
  const query = String(payload.query || result.query || nestedSearch.query || "");
  const content = firstText(result.content, result.text, result.body, result.markdown);
  const title = String(result.title || "");
  const hits = resultCount(result, results, content);
  const errors = [
    ...((Array.isArray(result.fallback_errors) ? result.fallback_errors : []) as unknown[]),
    ...((Array.isArray(result.fetch_errors) ? result.fetch_errors : []) as unknown[]),
    ...((Array.isArray(nestedSearch.fallback_errors) ? nestedSearch.fallback_errors : []) as unknown[]),
  ]
    .map((err) => {
      if (typeof err === "string") return err;
      const row = recordOf(err);
      return String(row.error || row.message || row.url || "").trim();
    })
    .filter(Boolean)
    .slice(0, 4);
  const [expanded, setExpanded] = useState(false);
  const previewLines = content.split("\n").slice(0, 6).join("\n");
  const hasMoreContent = content.length > previewLines.length;

  return (
    <div className="rounded-2xl border border-brand-500/15 bg-brand-500/[0.04] px-4 py-3 space-y-2.5">
      <div className="flex items-center justify-between gap-3 min-w-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="inline-flex items-center justify-center w-7 h-7 rounded-xl bg-brand-500/10 border border-brand-500/25 text-brand-200">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="9" />
              <path d="M3 12h18" />
              <path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18z" />
            </svg>
          </span>
          <div className="min-w-0">
            <div className="text-[11px] text-ink-500 font-medium">
              {action === "web_search"
                ? "Web search"
                : action === "web_fetch"
                ? "Fetch URL"
                : "Search + fetch"}
              {pending ? (
                <span className="ml-2 inline-flex items-center gap-1 text-fluid-400 normal-case tracking-normal">
                  <span className="typing-dot" />
                  <span>fetching</span>
                </span>
              ) : null}
            </div>
            <div className="text-[12.5px] text-ink-100 truncate">
              {query || url || "\u2014"}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {isResult ? (
            ok ? (
              <Tag tone={hits > 0 ? "ok" : "warn"}>{`${hits} hit${
                hits === 1 ? "" : "s"
              }`}</Tag>
            ) : (
              <Tag tone="err">{(block.error_kind as string | undefined) || "error"}</Tag>
            )
          ) : null}
          {typeof block.elapsed_ms === "number" ? <Tag>{block.elapsed_ms}ms</Tag> : null}
        </div>
      </div>

      {isResult && isSearch && results.length ? (
        <ul className="space-y-1.5 max-h-72 overflow-auto pr-1">
          {results.map((r, i) => (
            <li
              key={i}
              className="rounded-xl border border-brand-500/10 bg-brand-500/[0.04] px-3 py-2"
            >
              <a
                href={r.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[12.5px] text-brand-200 hover:text-brand-100 underline-offset-2 hover:underline cursor-pointer break-words"
              >
                {r.title || r.url}
              </a>
              {r.url ? (
                <div className="text-[10px] text-ink-500 font-mono truncate mt-0.5">
                  {r.url}
                </div>
              ) : null}
              {r.snippet ? (
                <div className="text-[11.5px] text-ink-300 leading-relaxed mt-1">
                  {r.snippet.length > 280 ? `${r.snippet.slice(0, 280)}\u2026` : r.snippet}
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      {isResult && isFetch && (content || title) ? (
        <div className="rounded-xl border border-brand-500/15 bg-ink-900/40 px-3 py-2.5">
          {title ? (
            <div className="text-[12.5px] font-semibold text-ink-100 mb-1 leading-snug">
              {title}
            </div>
          ) : null}
          {url ? (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[10.5px] text-brand-300 hover:text-brand-200 cursor-pointer break-all underline-offset-2 hover:underline"
            >
              {url}
            </a>
          ) : null}
          <div
            className={`mt-2 text-[12px] leading-relaxed text-ink-200 whitespace-pre-wrap font-sans transition-[max-height] ${
              expanded ? "max-h-[640px] overflow-auto" : "max-h-32 overflow-hidden"
            }`}
          >
            {expanded ? content : previewLines}
          </div>
          {hasMoreContent ? (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="mt-2 text-[12px] text-brand-300 hover:text-brand-200 cursor-pointer transition-colors"
            >
              {expanded ? "\u25BE collapse" : "\u25B8 expand full content"}
            </button>
          ) : null}
        </div>
      ) : null}

      {isResult && block.error ? (
        <div className="text-[12px] text-rose-300 break-words">{String(block.error)}</div>
      ) : null}
      {isResult && !block.error && errors.length ? (
        <div className="text-[11px] text-ink-400 leading-relaxed">
          {errors.join(" | ")}
        </div>
      ) : null}
    </div>
  );
}
