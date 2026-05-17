"use client";

/**
 * Lightweight portal-anchored dropdown.
 *
 * The May-2026 ``TopNav`` wraps its pill rail in ``overflow-x-auto``
 * so dense screens get a horizontal scroll instead of layout shift.
 * A side effect of that scroll viewport is that absolutely-positioned
 * dropdowns rendered inside the rail get clipped — even when their
 * trigger is fully visible.
 *
 * ``<PortalDropdown>`` solves it by rendering the panel through a
 * ``createPortal`` to ``document.body`` and computing its position
 * from the trigger element's bounding rect on every layout pass.
 *
 * Behaviour:
 *  • Click outside / Escape closes the panel.
 *  • Position is recomputed on window scroll/resize (``rAF``-debounced).
 *  • The portal panel adopts the airy ``glass-hi`` look so it feels
 *    like the rest of the airy redesign.
 *  • Right-aligned by default; pass ``align="left"`` to anchor on the
 *    trigger's left edge instead.
 */

import {
  CSSProperties,
  ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

type Align = "left" | "right";

export function useDropdown() {
  const [open, setOpen] = useState(false);
  const toggle = useCallback(() => setOpen((v) => !v), []);
  const close = useCallback(() => setOpen(false), []);
  return { open, setOpen, toggle, close };
}

interface PortalDropdownProps {
  open: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLElement | null>;
  align?: Align;
  width?: number | string;
  offset?: number;
  className?: string;
  children: ReactNode;
}

export function PortalDropdown({
  open,
  onClose,
  anchorRef,
  align = "right",
  width = 224,
  offset = 8,
  className,
  children,
}: PortalDropdownProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const viewportPadding = 8;
  const [style, setStyle] = useState<CSSProperties>({
    position: "fixed",
    top: -9999,
    left: -9999,
    opacity: 0,
    pointerEvents: "none",
  });
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  const reposition = useCallback(() => {
    const trigger = anchorRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const panel = panelRef.current;
    const panelWidth =
      typeof width === "number"
        ? width
        : panel
        ? panel.offsetWidth
        : 224;
    const panelHeight = panel?.offsetHeight ?? 0;
    const availableBelow = Math.max(
      96,
      window.innerHeight - rect.bottom - offset - viewportPadding,
    );
    const availableAbove = Math.max(96, rect.top - offset - viewportPadding);
    const placeAbove =
      panelHeight > availableBelow && availableAbove > availableBelow;
    const maxHeight = Math.max(
      96,
      Math.min(
        placeAbove ? availableAbove : availableBelow,
        window.innerHeight - viewportPadding * 2,
      ),
    );
    const unclampedTop = placeAbove
      ? rect.top - offset - panelHeight
      : rect.bottom + offset;
    const top = Math.min(
      window.innerHeight - viewportPadding - Math.min(panelHeight, maxHeight),
      Math.max(viewportPadding, unclampedTop),
    );
    const unclampedLeft =
      align === "right"
        ? rect.right - panelWidth
        : rect.left;
    const left = Math.max(
      viewportPadding,
      Math.min(window.innerWidth - panelWidth - viewportPadding, unclampedLeft),
    );
    setStyle({
      position: "fixed",
      top: `${top}px`,
      left: `${left}px`,
      width: typeof width === "number" ? `${width}px` : width,
      maxHeight: `${maxHeight}px`,
      overflowY: "auto",
      opacity: 1,
      pointerEvents: "auto",
      transformOrigin: placeAbove ? "bottom" : "top",
      zIndex: 1000,
    });
  }, [align, anchorRef, offset, viewportPadding, width]);

  useLayoutEffect(() => {
    if (!open) return;
    reposition();
    let frame = 0;
    const onScroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(reposition);
    };
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open, reposition]);

  useEffect(() => {
    if (!open) return;
    function onMouseDown(event: MouseEvent) {
      const target = event.target as Node | null;
      if (!target) return;
      if (panelRef.current?.contains(target)) return;
      if (anchorRef.current?.contains(target)) return;
      onClose();
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, onClose, anchorRef]);

  if (!mounted || !open) return null;

  return createPortal(
    <div
      ref={panelRef}
      role="menu"
      style={style}
      className={className ?? "glass-hi p-1.5 shadow-airy"}
    >
      {children}
    </div>,
    document.body,
  );
}
