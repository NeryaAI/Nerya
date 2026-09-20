"use client";

import { useEffect, useRef, useState, type PointerEvent } from "react";

const WIDTH_KEY = "nerya.chat.workspace.width.v1";
const clamp = (value: number) => Math.min(62, Math.max(38, value));

/** Width follows the actual work area, not the window or sidebar assumptions. */
export function useCanvasLayout(ready: boolean) {
  const layoutRef = useRef<HTMLDivElement>(null);
  const drag = useRef(false);
  const [width, setWidth] = useState(48);
  const [compact, setCompact] = useState(true);
  useEffect(() => {
    if (!ready) return;
    try {
      const saved = Number(localStorage.getItem(WIDTH_KEY));
      if (Number.isFinite(saved) && saved >= 38 && saved <= 62) setWidth(saved);
    } catch { /* A blocked storage API must not block the workspace. */ }
    const el = layoutRef.current;
    if (!el) return;
    const measure = () => setCompact(el.clientWidth < 960);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ready]);
  function changeWidth(value: number) {
    const next = clamp(value);
    setWidth(next);
    try { localStorage.setItem(WIDTH_KEY, String(next)); } catch { /* Optional preference. */ }
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    if (!drag.current || !layoutRef.current) return;
    const rect = layoutRef.current.getBoundingClientRect();
    if (rect.width) changeWidth((rect.right - event.clientX) / rect.width * 100);
  }
  const separatorProps = {
    onPointerDown: (event: PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      drag.current = true;
      event.currentTarget.setPointerCapture(event.pointerId);
      event.currentTarget.focus();
    },
    onPointerMove: move,
    onPointerUp: (event: PointerEvent<HTMLDivElement>) => {
      drag.current = false;
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    },
    onPointerCancel: () => { drag.current = false; },
    onLostPointerCapture: () => { drag.current = false; },
  };
  return { layoutRef, width, compact, changeWidth, separatorProps };
}
