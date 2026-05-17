"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { clientApi } from "../../../lib/clientApi";
import type {
  BrowserCdpAction,
  BrowserSessionRecord,
  BrowserSessionScreenshot,
} from "../../../lib/clientApi";

function latestScreenshot(record: BrowserSessionRecord | null): BrowserSessionScreenshot | null {
  if (!record) return null;
  if (record.last_screenshot?.data_uri) return record.last_screenshot;
  const shots = record.screenshots || [];
  for (let i = shots.length - 1; i >= 0; i -= 1) {
    if (shots[i]?.data_uri) return shots[i];
  }
  return null;
}

function keyForBrowser(key: string): string {
  const map: Record<string, string> = {
    ArrowDown: "ArrowDown",
    ArrowLeft: "ArrowLeft",
    ArrowRight: "ArrowRight",
    ArrowUp: "ArrowUp",
    Backspace: "Backspace",
    Delete: "Delete",
    End: "End",
    Enter: "Enter",
    Escape: "Escape",
    Home: "Home",
    PageDown: "PageDown",
    PageUp: "PageUp",
    Tab: "Tab",
  };
  return map[key] || "";
}

export default function BrowserSessionEmbedPage() {
  const t = useTranslations("browserEmbed");
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const lastWheelAtRef = useRef(0);
  const [sessionId, setSessionId] = useState("");
  const [initialUrl, setInitialUrl] = useState("");
  const [record, setRecord] = useState<BrowserSessionRecord | null>(null);
  const [frame, setFrame] = useState<BrowserSessionScreenshot | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [focused, setFocused] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setSessionId(params.get("session_id") || "");
    setInitialUrl(params.get("url") || "");
  }, []);

  const interactive = useMemo(() => {
    if (!record) return true;
    const engine = String(record?.engine || "").toLowerCase();
    return Boolean(record?.cdp) || engine === "cloakbrowser" || engine === "camofox";
  }, [record]);

  const refreshRecord = useCallback(async () => {
    if (!sessionId) return;
    try {
      const next = await clientApi.browserSessionGet(sessionId);
      const shot = latestScreenshot(next);
      setRecord(next);
      if (shot?.data_uri) setFrame(shot);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [sessionId]);

  const captureFrame = useCallback(async () => {
    if (!sessionId) return;
    try {
      const shot = interactive
        ? await clientApi.browserSessionCdpScreenshot({
            session_id: sessionId,
            full_page: false,
            timeout_s: 10,
          })
        : await clientApi.browserSessionScreenshot({
            session_id: sessionId,
            full_page: false,
            timeout_s: 10,
          });
      if (!shot.ok || !shot.data_uri) {
        if (!frame?.data_uri) setError(shot.error || shot.detail || t("frameUnavailable"));
        return;
      }
      setFrame({
        ts: new Date().toISOString(),
        url: shot.url || record?.current_url || initialUrl,
        ok: true,
        path: shot.path,
        bytes: shot.bytes,
        elapsed_ms: shot.elapsed_ms,
        fetch_method: shot.fetch_method,
        data_uri: shot.data_uri,
      });
      setError("");
    } catch (e) {
      if (!frame?.data_uri) setError(e instanceof Error ? e.message : String(e));
    }
  }, [frame?.data_uri, initialUrl, interactive, record?.current_url, sessionId, t]);

  useEffect(() => {
    if (!sessionId) return;
    void refreshRecord();
    const id = window.setInterval(() => {
      void refreshRecord();
    }, 2000);
    return () => window.clearInterval(id);
  }, [refreshRecord, sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    void captureFrame();
    const id = window.setInterval(() => {
      void captureFrame();
    }, 3500);
    return () => window.clearInterval(id);
  }, [captureFrame, sessionId]);

  function viewportPoint(event: React.MouseEvent | React.WheelEvent) {
    const img = imgRef.current;
    if (!img || !img.naturalWidth || !img.naturalHeight) return null;
    const rect = img.getBoundingClientRect();
    const imageRatio = img.naturalWidth / img.naturalHeight;
    const boxRatio = rect.width / rect.height;
    let drawW = rect.width;
    let drawH = rect.height;
    let offsetX = 0;
    let offsetY = 0;
    if (boxRatio > imageRatio) {
      drawH = rect.height;
      drawW = drawH * imageRatio;
      offsetX = (rect.width - drawW) / 2;
    } else {
      drawW = rect.width;
      drawH = drawW / imageRatio;
      offsetY = (rect.height - drawH) / 2;
    }
    const localX = event.clientX - rect.left - offsetX;
    const localY = event.clientY - rect.top - offsetY;
    if (localX < 0 || localY < 0 || localX > drawW || localY > drawH) return null;
    return {
      x: Math.round((localX / drawW) * img.naturalWidth),
      y: Math.round((localY / drawH) * img.naturalHeight),
    };
  }

  async function runAction(
    action: BrowserCdpAction,
    payload: Record<string, unknown> = {},
    busyKey: string = action,
  ) {
    if (!sessionId || !interactive) return;
    setBusy(busyKey);
    setError("");
    try {
      const res = await clientApi.browserSessionCdpAction({
        session_id: sessionId,
        action,
        payload,
      });
      if (!res.ok) {
        setError([res.error, res.detail, res.hint].filter(Boolean).join("\n"));
        return;
      }
      await refreshRecord();
      window.setTimeout(() => {
        void captureFrame();
      }, 250);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  function handleMouseDown(event: React.MouseEvent<HTMLDivElement>) {
    viewportRef.current?.focus();
    if (event.button !== 0) return;
    const point = viewportPoint(event);
    if (!point) return;
    event.preventDefault();
    void runAction("click_xy", point, "click");
  }

  function handleWheel(event: React.WheelEvent<HTMLDivElement>) {
    if (!interactive) return;
    event.preventDefault();
    const now = Date.now();
    if (now - lastWheelAtRef.current < 220) return;
    lastWheelAtRef.current = now;
    void runAction(
      "scroll",
      { dx: Math.round(event.deltaX), dy: Math.round(event.deltaY) },
      "scroll",
    );
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (!interactive) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const key = keyForBrowser(event.key);
    if (key) {
      event.preventDefault();
      void runAction("press", { key }, `key:${key}`);
      return;
    }
    if (event.key.length === 1) {
      event.preventDefault();
      void runAction("type", { text: event.key }, "type");
    }
  }

  const directUrl = frame?.url || record?.current_url || initialUrl;

  if (!sessionId) {
    return (
      <main className="flex h-screen items-center justify-center bg-[#050711] p-6 text-sm text-ink-400">
        {t("missingSession")}
      </main>
    );
  }

  if (!interactive && directUrl) {
    return (
      <iframe
        title={directUrl}
        src={directUrl}
        className="h-screen w-screen border-0 bg-white"
        sandbox="allow-same-origin allow-scripts allow-forms allow-popups"
      />
    );
  }

  return (
    <main className="h-screen w-screen overflow-hidden bg-[#050711] text-ink-100">
      <div
        ref={viewportRef}
        tabIndex={0}
        className={`relative h-full w-full outline-none ${focused ? "ring-1 ring-brand-400/60" : ""}`}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={handleKeyDown}
        onMouseDown={handleMouseDown}
        onWheel={handleWheel}
        role="application"
        aria-label={t("ariaLabel")}
      >
        {frame?.data_uri ? (
          <img
            ref={imgRef}
            src={frame.data_uri}
            alt={directUrl || sessionId}
            className="h-full w-full select-none object-contain"
            draggable={false}
          />
        ) : (
          <div className="flex h-full items-center justify-center px-6 text-center text-sm text-ink-400">
            {error || t("loadingFrame")}
          </div>
        )}

        <div className="pointer-events-none absolute left-3 top-3 max-w-[calc(100%-1.5rem)] rounded-md border border-black/40 bg-black/60 px-2 py-1 text-[11px] text-white/80 shadow-lg backdrop-blur">
          <div className="truncate font-mono">{directUrl || sessionId}</div>
        </div>

        <div className="pointer-events-none absolute bottom-3 left-3 flex max-w-[calc(100%-1.5rem)] flex-wrap gap-2">
          <span className="rounded-md border border-black/40 bg-black/60 px-2 py-1 text-[11px] text-white/75 shadow-lg backdrop-blur">
            {busy ? t("busy") : focused ? t("focused") : t("clickToFocus")}
          </span>
          {frame?.ts ? (
            <span className="rounded-md border border-black/40 bg-black/60 px-2 py-1 font-mono text-[11px] text-white/60 shadow-lg backdrop-blur">
              {new Date(frame.ts).toLocaleTimeString()}
            </span>
          ) : null}
          {error ? (
            <span className="rounded-md border border-[#ef4560]/40 bg-[#4b101b]/80 px-2 py-1 text-[11px] text-[#ffb8c2] shadow-lg backdrop-blur">
              {error}
            </span>
          ) : null}
        </div>
      </div>
    </main>
  );
}
