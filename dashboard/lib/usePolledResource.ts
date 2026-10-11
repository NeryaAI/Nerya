"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useWorkspaceIdentity } from "./workspaceIdentity";

type ResourceState<T> = {
  key: string;
  data?: T;
  error: string;
  loading: boolean;
  updatedAt?: number;
};
type Options<T> = {
  intervalMs?: number | ((data: T | undefined) => number | false);
  timeoutMs?: number;
};

/** A single, scoped read lifecycle. Errors retain evidence, never fabricate emptiness.
 * The loader may incrementally extend its own successful snapshot (e.g. event pages).
 * Returning false from intervalMs stops polling, not explicit refresh/reconnect.
 */
export function usePolledResource<T>(
  resourceKey: string | null,
  load: (signal: AbortSignal, previous?: T) => Promise<T>,
  options: Options<T> = {},
) {
  const workspace = useWorkspaceIdentity();
  const key = JSON.stringify([workspace, resourceKey]);
  const loader = useRef(load);
  const settings = useRef(options);
  loader.current = load;
  settings.current = options;
  const [state, setState] = useState<ResourceState<T>>({ key, loading: !!resourceKey, error: "" });
  const wake = useRef<() => void>(() => {});
  const refresh = useCallback(() => wake.current(), []);

  useEffect(() => {
    if (resourceKey === null) return;
    let disposed = false;
    let active: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let previous: T | undefined;
    let failures = 0;
    let rerun = false;
    setState({ key, loading: true, error: "" });

    async function poll() {
      if (disposed) return;
      if (active) { rerun = true; return; }
      clearTimeout(timer);
      const controller = new AbortController();
      active = controller;
      let timedOut = false;
      const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, settings.current.timeoutMs ?? 20_000);
      setState(current => current.key === key ? { ...current, loading: true } : current);
      try {
        const data = await loader.current(controller.signal, previous);
        if (disposed) return;
        if (timedOut) throw new Error("Request timed out");
        if (controller.signal.aborted) return;
        previous = data;
        failures = 0;
        setState({ key, data, loading: false, error: "", updatedAt: Date.now() });
      } catch (reason) {
        if (disposed) return;
        failures += 1;
        const error = timedOut ? "Request timed out" : reason instanceof Error ? reason.message : String(reason);
        setState(current => current.key === key ? { ...current, error, loading: false } : current);
      } finally {
        clearTimeout(timeout);
        active = undefined;
        if (!disposed) {
          setState(current => current.key === key ? { ...current, loading: false } : current);
          const interval = settings.current.intervalMs ?? 30_000;
          const delay = typeof interval === "function" ? interval(previous) : interval;
          // 失败也必须重试；此前只在成功分支安排下一次轮询会把页面永久冻结。
          if (rerun || failures || delay !== false) {
            const retry = Math.min(30_000, 1000 * 2 ** Math.min(failures, 5));
            const wait = rerun ? 0 : failures ? retry : Math.max(100, Number(delay));
            rerun = false;
            timer = setTimeout(() => {
              if (document.hidden) timer = setTimeout(poll, 30_000);
              else void poll();
            }, wait);
          }
        }
      }
    }
    const refreshNow = () => { if (!disposed) { clearTimeout(timer); void poll(); } };
    const visible = () => { if (!document.hidden) refreshNow(); };
    wake.current = refreshNow;
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("online", refreshNow);
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      active?.abort();
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener("online", refreshNow);
      if (wake.current === refreshNow) wake.current = () => {};
    };
  }, [key, resourceKey]);

  const current = state.key === key ? state : { key, loading: !!resourceKey, error: "" };
  return { ...current, stale: !!current.error && current.data !== undefined, refresh };
}

export function requireSuccess<T extends { ok?: boolean; error?: unknown }>(value: T): T {
  if (value.ok === false) throw new Error(typeof value.error === "string" ? value.error : "Resource unavailable");
  return value;
}
