"use client";

/**
 * BrowserSessionPanel — interactive browser session driver, simplified.
 *
 * Workflow-first surface mounted as the "Session" sub-tab of `/browsers`.
 * Replaces the legacy `/browser-session` page's complex driver UI:
 *
 *   - CDP mode is always on (the only mode that supports click/type/etc).
 *   - Render-mode toggle (TEXT/HTML/IFRAME) is gone — we always show the
 *     latest text body and a screenshot, which is what operators actually
 *     read.
 *   - Separate JS `eval` input is gone (was footgun; rarely used by hand).
 *   - Console + Network viewers are first-class panels, fed by the
 *     existing `get_console` / `get_network` CDP actions.
 *   - Errors surface via dialogs.toast(), never a top-of-page banner.
 *
 * Kept (the user's explicit list of must-haves):
 *   - Open / Navigate
 *   - Click by (x, y)
 *   - Click by selector
 *   - Scroll (dx, dy)
 *   - Type (text into focused or selected element)
 *   - Press key (Enter, Tab, ...)
 *
 * All state is owned by this component; the parent /browsers page just
 * mounts it inside its "Session" tab.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Card, Pill } from "./Page";
import { Select } from "./Select";
import { clientApi } from "../lib/clientApi";
import { toast } from "../lib/dialogs";
import type {
  BrowserConsoleEvent,
  BrowserCdpAction,
  BrowserNetworkEvent,
  BrowserSessionEnvelope,
  BrowserSessionListResponse,
  BrowserSessionRecord,
  BrowsersStatus,
} from "../lib/clientApi";

const DEFAULT_URL = "https://example.com";

function summariseBytes(n?: number): string {
  if (!n) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function fmtTs(ts?: string): string {
  if (!ts) return "–";
  // ISO-ish "2026-05-13T18:12:39.375Z" → "18:12:39"
  const m = /T(\d{2}:\d{2}:\d{2})/.exec(ts);
  return m ? m[1] : ts;
}

export function BrowserSessionPanel() {
  const [browsers, setBrowsers] = useState<BrowsersStatus | null>(null);
  const [sessions, setSessions] = useState<BrowserSessionListResponse | null>(
    null,
  );
  const [activeSessionId, setActiveSessionId] = useState<string>("");
  const [activeRecord, setActiveRecord] = useState<BrowserSessionRecord | null>(
    null,
  );
  const [urlDraft, setUrlDraft] = useState<string>(DEFAULT_URL);
  const [engineDraft, setEngineDraft] = useState<string>("");
  const [busy, setBusy] = useState<string>("");
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);

  // Action inputs (flat, no toggle gating)
  const [clickX, setClickX] = useState<string>("");
  const [clickY, setClickY] = useState<string>("");
  const [selector, setSelector] = useState<string>("");
  const [typeText, setTypeText] = useState<string>("");
  const [pressKey, setPressKey] = useState<string>("Enter");
  const [scrollDx, setScrollDx] = useState<string>("0");
  const [scrollDy, setScrollDy] = useState<string>("400");

  // Console + Network state
  const [consoleEvents, setConsoleEvents] = useState<BrowserConsoleEvent[]>([]);
  const [networkEvents, setNetworkEvents] = useState<BrowserNetworkEvent[]>([]);
  const [consoleFilter, setConsoleFilter] = useState<string>(""); // "", "log", "warn", "error"
  const [networkApiOnly, setNetworkApiOnly] = useState<boolean>(false);

  // Track inflight to avoid overlapping refresh loops
  const inflightRef = useRef<boolean>(false);

  const refreshBrowsers = useCallback(async () => {
    try {
      const next = await clientApi.browsersStatus();
      setBrowsers(next);
      setEngineDraft((current) => current || next.selected || "");
    } catch (e) {
      toast({
        tone: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const next = await clientApi.browserSessionList();
      setSessions(next);
    } catch {
      // ignore — keep last list
    }
  }, []);

  const refreshActive = useCallback(async (sid: string) => {
    if (!sid) {
      setActiveRecord(null);
      return;
    }
    try {
      const next = await clientApi.browserSessionGet(sid);
      setActiveRecord(next);
    } catch (e) {
      toast({
        tone: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }, []);

  useEffect(() => {
    void refreshBrowsers();
    void refreshSessions();
  }, [refreshBrowsers, refreshSessions]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = window.setInterval(() => {
      void refreshSessions();
      if (activeSessionId) void refreshActive(activeSessionId);
    }, 4000);
    return () => window.clearInterval(id);
  }, [autoRefresh, activeSessionId, refreshSessions, refreshActive]);

  const installedEngines = useMemo(
    () => (browsers?.engines || []).filter((e) => e.installed),
    [browsers],
  );
  const noEnginesInstalled = installedEngines.length === 0;

  /** Run any CDP action; toast on error, refresh active record on success. */
  const runAction = useCallback(
    async (
      action: BrowserCdpAction,
      payload: Record<string, unknown> | undefined,
      busyKey: string,
      onSuccess?: (res: Awaited<
        ReturnType<typeof clientApi.browserSessionCdpAction>
      >) => void,
    ) => {
      if (!activeSessionId) {
        toast({ tone: "warn", message: "Open a session first." });
        return;
      }
      setBusy(busyKey);
      try {
        const res = await clientApi.browserSessionCdpAction({
          session_id: activeSessionId,
          action,
          payload,
        });
        if (!res.ok) {
          const lines = [res.error, res.detail, res.hint].filter(
            Boolean,
          ) as string[];
          toast({
            tone: "error",
            message: lines.join(" · ") || `${action} failed`,
          });
        } else {
          onSuccess?.(res);
        }
        await refreshActive(activeSessionId);
      } catch (e) {
        toast({
          tone: "error",
          message: e instanceof Error ? e.message : String(e),
        });
      } finally {
        setBusy("");
      }
    },
    [activeSessionId, refreshActive],
  );

  async function handleStart() {
    setBusy("start");
    try {
      const body: Record<string, unknown> = {
        url: urlDraft.trim() || DEFAULT_URL,
        engine: engineDraft.trim() || undefined,
        session_id: activeSessionId || undefined,
      };
      const res: BrowserSessionEnvelope = await clientApi.browserSessionCdpOpen(
        body as { url: string },
      );
      if (!res.ok) {
        toast({
          tone: "error",
          message:
            [res.error, (res as Record<string, unknown>).detail].filter(Boolean)
              .join(" · ") || "open session failed",
        });
        return;
      }
      setActiveSessionId(res.session_id);
      await Promise.all([refreshSessions(), refreshActive(res.session_id)]);
      toast({
        tone: "ok",
        message: `Session ${res.session_id} → ${res.current_url || urlDraft}`,
      });
    } catch (e) {
      toast({
        tone: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy("");
    }
  }

  async function handleNavigate() {
    if (!activeSessionId) {
      void handleStart();
      return;
    }
    await runAction(
      "goto",
      { url: urlDraft.trim() || DEFAULT_URL },
      "navigate",
    );
  }

  async function handleScreenshot() {
    if (!activeSessionId) return;
    setBusy("screenshot");
    try {
      const res = await clientApi.browserSessionCdpScreenshot({
        session_id: activeSessionId,
      });
      if (!res.ok) {
        toast({
          tone: "error",
          message:
            [res.error, res.detail, res.stderr_tail].filter(Boolean).join(" · ") ||
            "screenshot failed",
        });
        return;
      }
      await refreshActive(activeSessionId);
    } catch (e) {
      toast({
        tone: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy("");
    }
  }

  async function handleClose() {
    if (!activeSessionId) return;
    setBusy("close");
    try {
      await clientApi.browserSessionCdpClose(activeSessionId);
      await clientApi.browserSessionClose(activeSessionId);
      const closedSid = activeSessionId;
      setActiveSessionId("");
      setActiveRecord(null);
      setConsoleEvents([]);
      setNetworkEvents([]);
      await refreshSessions();
      toast({ tone: "ok", message: `Session ${closedSid} closed.` });
    } catch (e) {
      toast({
        tone: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy("");
    }
  }

  function pickSession(sid: string) {
    setActiveSessionId(sid);
    setConsoleEvents([]);
    setNetworkEvents([]);
    void refreshActive(sid);
  }

  // Console / Network refresh helpers --------------------------------------
  async function refreshConsole(clear: boolean = false) {
    if (!activeSessionId || inflightRef.current) return;
    inflightRef.current = true;
    try {
      const res = await clientApi.browserSessionCdpAction({
        session_id: activeSessionId,
        action: "get_console",
        payload: {
          limit: 200,
          kind: consoleFilter || undefined,
          clear,
        },
      });
      if (res.ok) {
        setConsoleEvents(res.console || []);
      } else {
        toast({
          tone: "error",
          message: res.error || "console fetch failed",
        });
      }
    } catch (e) {
      toast({
        tone: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      inflightRef.current = false;
    }
  }

  async function refreshNetwork(clear: boolean = false) {
    if (!activeSessionId || inflightRef.current) return;
    inflightRef.current = true;
    try {
      const res = await clientApi.browserSessionCdpAction({
        session_id: activeSessionId,
        action: networkApiOnly ? "get_api_requests" : "get_network",
        payload: { limit: 200, clear },
      });
      if (res.ok) {
        setNetworkEvents(res.events || []);
      } else {
        toast({
          tone: "error",
          message: res.error || "network fetch failed",
        });
      }
    } catch (e) {
      toast({
        tone: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      inflightRef.current = false;
    }
  }

  // Auto-pull console+network when the active session changes
  useEffect(() => {
    if (!activeSessionId) return;
    void refreshConsole(false);
    void refreshNetwork(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId]);

  // ------------------------------------------------------------------------
  const last = activeRecord?.last;
  const renderBody =
    last?.markdown ||
    last?.text ||
    (last?.error
      ? `[engine error] ${last.error}\n\n${last.detail || ""}`
      : "");

  return (
    <div className="space-y-4">
      {/* Sticky session-picker bar */}
      <Card
        featured
        title="Active session"
        description={
          activeSessionId
            ? `Driving session ${activeSessionId}. CDP mode is always on; every action below targets this session.`
            : "Pick a session from the left sidebar or open a new one to start driving the browser."
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={browsers?.selected ? "ok" : "warn"}>
              {browsers?.selected
                ? `Engine · ${browsers.selected}`
                : "No engine selected"}
            </Pill>
            {sessions ? (
              <Pill tone={sessions.count > 0 ? "brand" : "neutral"}>
                {sessions.count} session(s)
              </Pill>
            ) : null}
            <label className="flex items-center gap-1 text-[11px] text-ink-300">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
              />
              auto-refresh
            </label>
          </div>
        }
      >
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
          {/* Left: sessions sidebar */}
          <div className="space-y-2">
            <div className="text-[12px] text-ink-400 font-medium">
              Open sessions
            </div>
            {(sessions?.sessions || []).map((s) => {
              const active = s.session_id === activeSessionId;
              return (
                <button
                  type="button"
                  key={s.session_id}
                  className={`w-full text-left rounded-md border px-3 py-2 ${
                    active
                      ? "border-brand-400/50 bg-brand-500/10"
                      : "border-brand-500/10 bg-ink-900/40 hover:bg-ink-900/60"
                  }`}
                  onClick={() => pickSession(s.session_id)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[12px] text-ink-100">
                      {s.session_id}
                    </span>
                    <Pill tone={s.last_ok ? "ok" : "warn"}>
                      {s.engine || "?"}
                    </Pill>
                  </div>
                  <div className="mt-1 truncate text-[11px] text-ink-400">
                    {s.current_url || "–"}
                  </div>
                  <div className="mt-1 text-[10px] text-ink-500">
                    {s.last_fetch_method || "?"} ·{" "}
                    {summariseBytes(s.last_bytes)} · {s.last_elapsed_ms ?? 0}ms
                  </div>
                </button>
              );
            })}
            {(!sessions || !sessions.sessions || sessions.sessions.length === 0)
              ? (
                <div className="rounded-md border border-brand-500/10 bg-ink-950/30 px-3 py-2 text-[11px] text-ink-400">
                  No open sessions yet. Use the URL bar on the right to open
                  one.
                </div>
              )
              : null}
            <div className="pt-2">
              <div className="text-[12px] text-ink-400 font-medium">
                Engine for new sessions
              </div>
              <Select
                value={engineDraft}
                onChange={(value) => setEngineDraft(value)}
                options={[
                  {
                    value: "",
                    label: `Use selected (${browsers?.selected || "none"})`,
                  },
                  ...installedEngines.map((e) => ({
                    value: e.name,
                    label: `${e.title || e.name} (${e.kind})`,
                  })),
                ]}
                size="sm"
                ariaLabel="Engine for new sessions"
              />
              {noEnginesInstalled
                ? (
                  <div className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[10px] text-amber-100">
                    No engines installed yet. Switch to the Engines tab to install
                    one.
                  </div>
                )
                : null}
            </div>
          </div>

          {/* Right: URL bar + page body */}
          <div className="space-y-3">
            <div className="flex flex-col gap-2 md:flex-row">
              <input
                className="input-dark flex-1 font-mono text-xs"
                value={urlDraft}
                onChange={(e) => setUrlDraft(e.target.value)}
                placeholder="https://example.com"
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void (activeSessionId ? handleNavigate() : handleStart());
                  }
                }}
              />
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void handleStart()}
                  disabled={Boolean(busy) || !urlDraft.trim() ||
                    noEnginesInstalled}
                >
                  {busy === "start"
                    ? "Opening…"
                    : activeSessionId
                    ? "Open new"
                    : "Open"}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void handleNavigate()}
                  disabled={Boolean(busy) || !urlDraft.trim()}
                >
                  {busy === "navigate" ? "Loading…" : "Navigate"}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void runAction("reload", undefined, "reload")}
                  disabled={Boolean(busy) || !activeSessionId}
                >
                  Reload
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void runAction("go_back", undefined, "back")}
                  disabled={Boolean(busy) || !activeSessionId}
                >
                  ←
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() =>
                    void runAction("go_forward", undefined, "forward")}
                  disabled={Boolean(busy) || !activeSessionId}
                >
                  →
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void handleScreenshot()}
                  disabled={Boolean(busy) || !activeSessionId}
                >
                  {busy === "screenshot" ? "Snapping…" : "Screenshot"}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void handleClose()}
                  disabled={Boolean(busy) || !activeSessionId}
                >
                  Close
                </button>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-3 text-[11px] text-ink-400">
              <span>
                URL:{" "}
                <span className="font-mono text-ink-200 break-all">
                  {activeRecord?.current_url || "–"}
                </span>
              </span>
              <span>
                engine:{" "}
                <span className="font-mono text-ink-200">
                  {activeRecord?.engine || "–"}
                </span>
              </span>
              <span className="ml-auto">
                {last?.fetch_method || "–"} · {summariseBytes(last?.bytes)} ·{" "}
                {last?.elapsed_ms ?? 0}ms
              </span>
            </div>
          </div>
        </div>
      </Card>

      {/* Latest screenshot */}
      {activeSessionId
        ? (
          <Card
            title="Latest screenshot"
            description={activeRecord?.last_screenshot
              ? `${
                activeRecord.last_screenshot.fetch_method || "–"
              } · ${
                summariseBytes(activeRecord.last_screenshot.bytes)
              } · ${activeRecord.last_screenshot.elapsed_ms ?? 0}ms`
              : "No screenshot yet. Click the Screenshot button above."}
          >
            {activeRecord?.last_screenshot?.data_uri
              ? (
                <a
                  href={activeRecord.last_screenshot.data_uri}
                  target="_blank"
                  rel="noreferrer"
                  className="block"
                >
                  <img
                    src={activeRecord.last_screenshot.data_uri}
                    alt="Latest screenshot"
                    className="max-h-[55vh] w-full rounded-md border border-brand-500/10 bg-ink-950 object-contain"
                  />
                </a>
              )
              : (
                <div className="rounded-md border border-brand-500/10 bg-ink-950/30 px-3 py-2 text-[11px] text-ink-400">
                  Take a screenshot to see the live page rendering.
                </div>
              )}
          </Card>
        )
        : null}

      {/* Interaction toolbox */}
      <Card
        title="Interact"
        description="Move the mouse, send keystrokes, scroll. All actions target the active session."
      >
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[120px_120px_auto_auto]">
            <input
              className="input-dark text-xs"
              placeholder="x"
              value={clickX}
              onChange={(e) => setClickX(e.target.value)}
              disabled={!activeSessionId}
            />
            <input
              className="input-dark text-xs"
              placeholder="y"
              value={clickY}
              onChange={(e) => setClickY(e.target.value)}
              disabled={!activeSessionId}
            />
            <button
              type="button"
              className="btn btn-ghost"
              disabled={!activeSessionId || busy === "click_xy" ||
                !clickX.trim() || !clickY.trim()}
              onClick={() =>
                void runAction(
                  "click_xy",
                  { x: Number(clickX), y: Number(clickY) },
                  "click_xy",
                )}
            >
              {busy === "click_xy" ? "Clicking…" : "Click @ (x, y)"}
            </button>
            <div className="flex items-center gap-2">
              <input
                className="input-dark flex-1 text-xs font-mono"
                placeholder="dx"
                value={scrollDx}
                onChange={(e) => setScrollDx(e.target.value)}
                disabled={!activeSessionId}
              />
              <input
                className="input-dark flex-1 text-xs font-mono"
                placeholder="dy"
                value={scrollDy}
                onChange={(e) => setScrollDy(e.target.value)}
                disabled={!activeSessionId}
              />
              <button
                type="button"
                className="btn btn-ghost"
                disabled={!activeSessionId || busy === "scroll"}
                onClick={() =>
                  void runAction(
                    "scroll",
                    {
                      dx: Number(scrollDx) || 0,
                      dy: Number(scrollDy) || 0,
                    },
                    "scroll",
                  )}
              >
                {busy === "scroll" ? "Scrolling…" : "Scroll"}
              </button>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_auto_auto]">
            <input
              className="input-dark text-xs font-mono"
              placeholder="CSS selector, e.g. button.primary"
              value={selector}
              onChange={(e) => setSelector(e.target.value)}
              disabled={!activeSessionId}
            />
            <button
              type="button"
              className="btn btn-ghost"
              disabled={!activeSessionId || busy === "click_selector" ||
                !selector.trim()}
              onClick={() =>
                void runAction(
                  "click_selector",
                  { selector },
                  "click_selector",
                )}
            >
              {busy === "click_selector" ? "Clicking…" : "Click selector"}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={!activeSessionId || busy === "type" || !typeText}
              onClick={() =>
                void runAction(
                  "type",
                  {
                    text: typeText,
                    selector: selector || undefined,
                  },
                  "type",
                )}
              title="Types into the selector if provided, else focused element."
            >
              {busy === "type" ? "Typing…" : "Type"}
            </button>
          </div>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_140px_auto]">
            <input
              className="input-dark text-xs font-mono"
              placeholder="Text to type"
              value={typeText}
              onChange={(e) => setTypeText(e.target.value)}
              disabled={!activeSessionId}
            />
            <input
              className="input-dark text-xs font-mono"
              placeholder="Enter"
              value={pressKey}
              onChange={(e) => setPressKey(e.target.value)}
              disabled={!activeSessionId}
            />
            <button
              type="button"
              className="btn btn-ghost"
              disabled={!activeSessionId || busy === "press" || !pressKey.trim()}
              onClick={() =>
                void runAction(
                  "press",
                  { key: pressKey },
                  "press",
                )}
            >
              {busy === "press" ? "Pressing…" : "Press key"}
            </button>
          </div>
        </div>
      </Card>

      {/* Page body (text) */}
      <Card
        title="Page text"
        description="Latest text snapshot from the active session. Use Reload above to refresh."
      >
        <div
          className="max-h-[40vh] overflow-auto rounded-md border border-brand-500/10 bg-ink-950/40 p-3 font-mono text-[11px] text-ink-200 whitespace-pre-wrap"
          style={{ wordBreak: "break-word" }}
        >
          {renderBody ||
            (activeSessionId
              ? "No body captured yet. Hit Reload."
              : "Open a session to inspect the page text.")}
        </div>
      </Card>

      {/* Console viewer */}
      <Card
        title="Console"
        description="Live console output from the page (log/info/warn/error)."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Select
              value={consoleFilter}
              onChange={(value) => setConsoleFilter(value)}
              options={[
                { value: "", label: "All levels" },
                { value: "log", label: "log" },
                { value: "info", label: "info" },
                { value: "warn", label: "warn" },
                { value: "error", label: "error" },
                { value: "debug", label: "debug" },
              ]}
              size="sm"
              ariaLabel="Filter console events by level"
            />
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void refreshConsole(false)}
              disabled={!activeSessionId}
            >
              Refresh
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void refreshConsole(true)}
              disabled={!activeSessionId || consoleEvents.length === 0}
              title="Clear after fetching"
            >
              Clear
            </button>
          </div>
        }
      >
        {!activeSessionId
          ? (
            <div className="rounded-md border border-brand-500/10 bg-ink-950/30 px-3 py-2 text-[11px] text-ink-400">
              Open a session to capture console output.
            </div>
          )
          : consoleEvents.length === 0
          ? (
            <div className="rounded-md border border-brand-500/10 bg-ink-950/30 px-3 py-2 text-[11px] text-ink-400">
              No console events captured yet. Trigger an action on the page,
              then click Refresh.
            </div>
          )
          : (
            <div className="max-h-[40vh] space-y-1 overflow-auto">
              {consoleEvents.map((evt, i) => {
                const level = (evt.level || evt.kind || "log").toLowerCase();
                const tone: "ok" | "warn" | "neutral" =
                  level === "error" || level === "warn"
                    ? "warn"
                    : level === "log" || level === "info"
                    ? "ok"
                    : "neutral";
                return (
                  <div
                    key={`${evt.ts}_${i}`}
                    className="flex items-start gap-2 rounded-sm border border-brand-500/5 bg-ink-950/30 px-2 py-1 text-[11px]"
                  >
                    <span className="font-mono text-ink-500">
                      {fmtTs(evt.ts)}
                    </span>
                    <Pill tone={tone}>{level}</Pill>
                    <span className="font-mono text-ink-200 break-all">
                      {evt.text || "–"}
                    </span>
                    {evt.url
                      ? (
                        <span className="ml-auto truncate font-mono text-[10px] text-ink-500">
                          {evt.url}
                          {evt.line ? `:${evt.line}` : ""}
                        </span>
                      )
                      : null}
                  </div>
                );
              })}
            </div>
          )}
      </Card>

      {/* Network viewer */}
      <Card
        title="Network requests"
        description="Requests issued by the page (XHR/fetch/document/etc)."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1 text-[11px] text-ink-300">
              <input
                type="checkbox"
                checked={networkApiOnly}
                onChange={(e) => setNetworkApiOnly(e.target.checked)}
              />
              API only (XHR/fetch)
            </label>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void refreshNetwork(false)}
              disabled={!activeSessionId}
            >
              Refresh
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void refreshNetwork(true)}
              disabled={!activeSessionId || networkEvents.length === 0}
              title="Clear after fetching"
            >
              Clear
            </button>
          </div>
        }
      >
        {!activeSessionId
          ? (
            <div className="rounded-md border border-brand-500/10 bg-ink-950/30 px-3 py-2 text-[11px] text-ink-400">
              Open a session to capture network traffic.
            </div>
          )
          : networkEvents.length === 0
          ? (
            <div className="rounded-md border border-brand-500/10 bg-ink-950/30 px-3 py-2 text-[11px] text-ink-400">
              No requests captured yet. Trigger navigation or interactions on
              the page, then click Refresh.
            </div>
          )
          : (
            <div className="max-h-[40vh] overflow-auto">
              <table className="w-full table-fixed text-[11px]">
                <thead className="sticky top-0 bg-ink-950/80 text-left text-ink-400">
                  <tr>
                    <th className="w-[80px] px-2 py-1 font-medium">Time</th>
                    <th className="w-[60px] px-2 py-1 font-medium">Method</th>
                    <th className="w-[60px] px-2 py-1 font-medium">Status</th>
                    <th className="px-2 py-1 font-medium">URL</th>
                    <th className="w-[70px] px-2 py-1 font-medium text-right">
                      ms
                    </th>
                  </tr>
                </thead>
                <tbody className="font-mono text-ink-200">
                  {networkEvents.map((evt, i) => {
                    const status = Number(evt.status);
                    const tone: "ok" | "warn" | "neutral" =
                      Number.isFinite(status) && status > 0
                        ? status >= 400
                          ? "warn"
                          : status >= 200 && status < 300
                          ? "ok"
                          : "neutral"
                        : evt.failure
                        ? "warn"
                        : "neutral";
                    return (
                      <tr
                        key={`${evt.request_id || evt.ts}_${i}`}
                        className="border-t border-brand-500/5"
                      >
                        <td className="px-2 py-1 text-ink-500">
                          {fmtTs(evt.ts)}
                        </td>
                        <td className="px-2 py-1">{evt.method || "–"}</td>
                        <td className="px-2 py-1">
                          <Pill tone={tone}>
                            {evt.status ?? evt.failure?.slice(0, 4) ?? "–"}
                          </Pill>
                        </td>
                        <td className="px-2 py-1 truncate text-ink-200">
                          <a
                            href={evt.url}
                            target="_blank"
                            rel="noreferrer"
                            className="hover:underline"
                          >
                            {evt.url || "–"}
                          </a>
                        </td>
                        <td className="px-2 py-1 text-right text-ink-500">
                          {evt.elapsed_ms ?? "–"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
      </Card>
    </div>
  );
}
