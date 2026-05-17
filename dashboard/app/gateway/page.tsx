"use client";

/**
 * Gateway operations workspace. Self-contained: no jumps to Settings.
 *
 * Two sub-tabs:
 *
 *  • Channels  — Configured channels list + the full per-platform
 *                configuration form. Picking a platform from the form
 *                immediately filters the inline documentation panel to
 *                ONLY that platform's setup docs + secret fields, so
 *                the operator never wades through every platform's
 *                docs to find the one they care about.
 *  • Live      — Real-time event ticker fed by an EventSource against
 *                ``/gateway/events/stream`` (the proxy in
 *                ``app/api/proxy/[...path]/route.ts`` pipes Server-Sent
 *                Events straight through). Falls back to polling
 *                ``/gateway/events?since=N`` at 1.5s if the stream
 *                fails — the server-side ring buffer keeps the cursor
 *                monotonic so the two paths share state.
 *
 * Previously this page had a third "Docs" tab that listed *every*
 * platform's setup notes on one screen. That was a documentation dump,
 * not a workflow — the new Channels tab serves the same purpose
 * contextually (right-doc when you're configuring that specific
 * platform).
 */

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Card, PageBody, PageHeader, Pill } from "../../components/Page";
import { GatewayChannelsPanel } from "../../components/GatewayChannelsPanel";
import { Select } from "../../components/Select";
import { clientApi } from "../../lib/clientApi";
import type {
  GatewayChannelConfig,
  GatewayLiveEvent,
} from "../../lib/clientApi";

type SubTab = "channels" | "live";
type LiveTransport = "sse" | "poll" | "off";

const POLL_INTERVAL_MS = 1500;
const MAX_LIVE_ROWS = 200;

function formatTime(tsMs: number): string {
  if (!tsMs) return "";
  try {
    const d = new Date(tsMs);
    return d.toLocaleTimeString(undefined, { hour12: false });
  } catch {
    return "";
  }
}

function eventTone(ev: GatewayLiveEvent): "ok" | "warn" | "danger" | "brand" {
  if (ev.kind === "error") return "danger";
  if (ev.kind === "heartbeat") return "ok";
  if (ev.kind === "outbound") return "ok";
  if (ev.kind === "inbound") return "brand";
  if (ev.kind === "info") return "warn";
  return "warn";
}

function eventSummary(ev: GatewayLiveEvent): string {
  if (ev.kind === "error") {
    const parts: string[] = [];
    if (ev.reason) parts.push(ev.reason);
    if (ev.detail) parts.push(ev.detail);
    if (ev.hint) parts.push(`→ ${ev.hint}`);
    return parts.join(" · ") || "error";
  }
  if (ev.kind === "heartbeat") {
    return ev.status === "ok"
      ? "polling alive"
      : ev.note || ev.status || "heartbeat";
  }
  if (ev.kind === "info") {
    const parts: string[] = [];
    if (ev.reason) parts.push(ev.reason);
    if (ev.note) parts.push(ev.note);
    return parts.join(" · ") || "info";
  }
  if (ev.text) return ev.text;
  if (ev.phase) return ev.phase;
  if (typeof ev.command === "string" && ev.command) return `command ${ev.command}`;
  if (ev.kind === "outbound") return "agent reply";
  if (ev.kind === "inbound") return "user message";
  return ev.kind;
}

export default function GatewayPage() {
  const t = useTranslations("gatewayPage");
  const [tab, setTab] = useState<SubTab>("channels");

  // Live-tab state. Channel inventory is loaded here too so the Live
  // tab can render the channel filter even before the operator clicks
  // into Channels.
  const [liveChannels, setLiveChannels] = useState<GatewayChannelConfig[]>([]);
  const [events, setEvents] = useState<GatewayLiveEvent[]>([]);
  const [cursor, setCursor] = useState<number>(0);
  const [filterChannel, setFilterChannel] = useState<string>("");
  const [streaming, setStreaming] = useState(true);
  const [transport, setTransport] = useState<LiveTransport>("off");
  const [liveError, setLiveError] = useState<string | null>(null);
  const cursorRef = useRef<number>(0);
  const eventSourceRef = useRef<EventSource | null>(null);

  const loadLiveChannels = useCallback(async () => {
    try {
      const res = await clientApi.gatewayConfig();
      if (!res.ok) return;
      setLiveChannels(res.channels || []);
    } catch {
      // Non-fatal; the channel filter just stays empty.
    }
  }, []);

  useEffect(() => {
    void loadLiveChannels();
    const id = setInterval(loadLiveChannels, 15_000);
    return () => clearInterval(id);
  }, [loadLiveChannels]);

  const pollEvents = useCallback(async () => {
    try {
      const res = await clientApi.gatewayEvents({
        since: cursorRef.current,
        channel: filterChannel || undefined,
        limit: 50,
      });
      if (res.events && res.events.length) {
        setEvents((prev) => {
          const combined = [...prev, ...res.events];
          if (combined.length > MAX_LIVE_ROWS) {
            return combined.slice(combined.length - MAX_LIVE_ROWS);
          }
          return combined;
        });
      }
      const next = res.cursor ?? res.head ?? cursorRef.current;
      cursorRef.current = next;
      setCursor(next);
    } catch (e) {
      setLiveError(e instanceof Error ? e.message : String(e));
    }
  }, [filterChannel]);

  useEffect(() => {
    cursorRef.current = 0;
    setCursor(0);
    setEvents([]);
  }, [filterChannel]);

  useEffect(() => {
    if (!streaming || tab !== "live") {
      setTransport("off");
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      return;
    }

    let cancelled = false;
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    const startPolling = () => {
      if (cancelled) return;
      setTransport("poll");
      const tick = async () => {
        if (cancelled) return;
        await pollEvents();
      };
      void tick();
      pollTimer = setInterval(tick, POLL_INTERVAL_MS);
    };

    const stopPolling = () => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    if (typeof window !== "undefined" && typeof window.EventSource === "function") {
      const params = new URLSearchParams();
      if (cursorRef.current) params.set("since", String(cursorRef.current));
      if (filterChannel) params.set("channel", filterChannel);
      const url = `/api/proxy/gateway/events/stream${
        params.toString() ? `?${params}` : ""
      }`;
      const es = new EventSource(url);
      eventSourceRef.current = es;
      let openedOk = false;
      es.onopen = () => {
        if (cancelled) return;
        openedOk = true;
        setTransport("sse");
        setLiveError(null);
      };
      es.onmessage = (msg) => {
        if (cancelled) return;
        try {
          const ev = JSON.parse(msg.data) as GatewayLiveEvent;
          const seq = Number(ev.seq) || cursorRef.current;
          cursorRef.current = seq;
          setCursor(seq);
          setEvents((prev) => {
            const next = [...prev, ev];
            return next.length > MAX_LIVE_ROWS
              ? next.slice(next.length - MAX_LIVE_ROWS)
              : next;
          });
        } catch {
          // Skip malformed payload — keep the stream open.
        }
      };
      es.onerror = () => {
        if (cancelled) return;
        es.close();
        eventSourceRef.current = null;
        if (!openedOk) {
          startPolling();
        }
      };
      return () => {
        cancelled = true;
        stopPolling();
        es.close();
        eventSourceRef.current = null;
      };
    }

    startPolling();
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [filterChannel, pollEvents, streaming, tab]);

  const filteredEvents = useMemo(
    () =>
      filterChannel
        ? events.filter((ev) => ev.channel === filterChannel)
        : events,
    [events, filterChannel],
  );

  const tabs: { id: SubTab; label: string }[] = [
    { id: "channels", label: t("tabChannels") },
    { id: "live", label: t("tabLive") },
  ];

  return (
    <PageBody>
      <PageHeader
        eyebrow={t("eyebrow")}
        title={t("title")}
        description={t("description")}
      />

      <div className="flex flex-wrap gap-2">
        {tabs.map((entry) => (
          <button
            key={entry.id}
            type="button"
            onClick={() => setTab(entry.id)}
            className={`rounded-full border px-3 py-1 text-[11px] transition-colors ${
              tab === entry.id
                ? "border-brand-500/50 bg-brand-500/15 text-brand-100"
                : "border-brand-500/15 bg-ink-950/40 text-ink-300 hover:border-brand-500/30 hover:text-ink-100"
            }`}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {tab === "channels" ? <GatewayChannelsPanel /> : null}

      {tab === "live" ? (
        <Card
          title={t("liveTitle")}
          description={t("liveDescription")}
          actions={
            <div className="flex items-center gap-2">
              <div className="min-w-[180px]">
                <Select
                  value={filterChannel}
                  onChange={(value) => setFilterChannel(value)}
                  options={[
                    { value: "", label: t("filterAll") },
                    ...liveChannels.map((c) => ({
                      value: c.channel,
                      label: c.channel,
                    })),
                  ]}
                  size="sm"
                  ariaLabel={t("filterAll")}
                />
              </div>
              <button
                type="button"
                className="btn btn-ghost text-[11px]"
                onClick={() => setStreaming((v) => !v)}
              >
                {streaming ? t("pause") : t("resume")}
              </button>
              <Pill
                tone={
                  transport === "sse" ? "ok" : transport === "poll" ? "warn" : "brand"
                }
              >
                {transport === "sse"
                  ? t("transportSse")
                  : transport === "poll"
                  ? t("transportPoll")
                  : t("transportOff")}
              </Pill>
              <span className="text-[10px] text-ink-500">
                {t("cursorLabel", { cursor })}
              </span>
            </div>
          }
        >
          {liveError ? (
            <div className="mb-2 rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[12px] text-rose-100">
              {liveError}
            </div>
          ) : null}
          {filteredEvents.length === 0 ? (
            <div className="text-[12px] text-ink-500">{t("noEvents")}</div>
          ) : (
            <ol className="space-y-1.5">
              {filteredEvents
                .slice()
                .reverse()
                .map((ev) => (
                  <li
                    key={`${ev.seq}-${ev.kind}-${ev.channel}`}
                    className={`rounded-md border px-2 py-1.5 text-[11px] ${
                      ev.kind === "error"
                        ? "border-rose-500/30 bg-rose-500/10"
                        : ev.kind === "heartbeat"
                        ? "border-emerald-500/20 bg-emerald-500/5"
                        : "border-brand-500/10 bg-ink-950/40"
                    }`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-[10px] text-ink-500">
                        {formatTime(ev.ts_ms)}
                      </span>
                      <Pill tone={eventTone(ev)}>{ev.kind}</Pill>
                      <span className="font-mono text-[10px] text-ink-400">
                        {ev.platform}/{ev.channel}
                      </span>
                      {ev.session_id ? (
                        <span className="font-mono text-[10px] text-ink-500">
                          s:{String(ev.session_id).slice(0, 8)}
                        </span>
                      ) : null}
                      <span className="min-w-0 flex-1 truncate text-ink-200">
                        {eventSummary(ev)}
                      </span>
                    </div>
                    {ev.kind === "error" && ev.hint ? (
                      <div className="mt-1 text-[10px] text-rose-200/90">
                        → {ev.hint}
                      </div>
                    ) : null}
                  </li>
                ))}
            </ol>
          )}
        </Card>
      ) : null}
    </PageBody>
  );
}
